"""Image definitions, the builder, and the image cache metadata.

Plan.md section 9 asks for one base image, built once, cached, and digest-stamped onto every run
record. This module implements that with an intentionally dull pipeline:

1. read a YAML definition from ``images/``;
2. hash the definition and its provisioning script into a **definition hash**;
3. if the cache already records a successful build of that hash and the alias still resolves to
   the recorded fingerprint, do nothing (this is what makes the command idempotent);
4. otherwise launch a container from the source image (or from the parent overlay's alias), run
   ``apt-get install`` and the provisioning script in it, stop it, and ``lxc publish`` it under
   the definition's alias;
5. record alias, fingerprint, definition hash and build time in the cache.

Note the direction of the freshness check. The cache is a record of what was built, never the
authority on what exists: the fingerprint is re-read from LXD on every ``status`` and every build
decision, so an image deleted behind the harness's back is detected rather than assumed.

A cloud-init profile was considered, per plan.md section 9's wording, and rejected: cloud-init's
failure mode is a container that boots, reports success and is silently missing half its packages,
which then shows up as a mysterious agent failure days later. Running the provisioning steps as
foreground execs means a build that fails, fails at the step that broke, with that step's stderr.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from segbench.logging import get_logger
from segbench.runtime.base import UNPOLICED, ContainerSpec, Handle, RuntimeFailure
from segbench.runtime.lxd import LXDRuntime, sanitise_name

log = get_logger(__name__)

#: Where definitions live, relative to the repository root.
DEFINITIONS_DIR = Path("images")

#: Filename of the cache metadata inside ``paths.image_cache``.
CACHE_FILE = "images.json"


class ImageDefinitionError(RuntimeFailure):
    """A definition file is missing, malformed, or references a script that does not exist."""


class ImageSource(BaseModel):
    """Where a base definition starts from: a remote and an image on it."""

    model_config = ConfigDict(extra="forbid")

    remote: str = "ubuntu"
    image: str = "24.04"

    @property
    def reference(self) -> str:
        return f"{self.remote}:{self.image}"


class ImageDefinition(BaseModel):
    """One image definition: the base, or a product overlay layered on it."""

    model_config = ConfigDict(extra="forbid")

    name: str
    alias: str
    description: str = ""
    #: Exactly one of ``source`` (a base) or ``parent`` (an overlay) must be set.
    source: ImageSource | None = None
    parent: str | None = None
    #: ``bug.product`` values this overlay serves. Empty on the base, which serves everything.
    products: list[str] = Field(default_factory=list)
    packages: list[str] = Field(default_factory=list)
    provision: Path | None = None
    agent_user: str = "agent"

    #: Set by the loader; not part of the file.
    path: Path = Path()
    #: The definitions root. Provisioning scripts resolve against it rather than against the
    #: definition's own directory, so overlays in ``images/overlays/`` share ``images/provision/``.
    root: Path = Path()

    def provision_script(self) -> Path | None:
        return (self.root / self.provision) if self.provision else None

    def definition_hash(self) -> str:
        """Hash the definition and its provisioning script.

        This is what makes ``--force`` rarely necessary: edit either file and the next build sees
        a different hash and rebuilds. It deliberately does *not* hash the upstream Ubuntu image,
        which moves under the harness; ``--force`` is the way to pick up an upstream refresh, and
        the run record carries the resulting fingerprint either way.
        """
        digest = hashlib.sha256()
        digest.update(self.model_dump_json(exclude={"path", "root"}).encode())
        script = self.provision_script()
        if script and script.is_file():
            digest.update(script.read_bytes())
        return digest.hexdigest()


def load_definition(path: Path, *, root: Path | None = None) -> ImageDefinition:
    """Load and validate one definition file.

    ``root`` is the definitions directory that provisioning scripts resolve against. It defaults
    to the definition's own directory, which is right for a standalone base definition and wrong
    for an overlay, so :func:`load_definitions` always passes it explicitly.
    """
    if not path.is_file():
        raise ImageDefinitionError(f"image definition not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ImageDefinitionError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ImageDefinitionError(f"{path}: expected a mapping at the top level")
    raw["path"] = path
    raw["root"] = root if root is not None else path.parent
    try:
        definition = ImageDefinition.model_validate(raw)
    except Exception as exc:
        raise ImageDefinitionError(f"{path}: {exc}") from exc

    if bool(definition.source) == bool(definition.parent):
        raise ImageDefinitionError(
            f"{path}: set exactly one of `source` (a base image) or `parent` (an overlay)"
        )
    script = definition.provision_script()
    if script is not None and not script.is_file():
        raise ImageDefinitionError(f"{path}: provision script not found: {script}")
    return definition


def load_definitions(root: Path = DEFINITIONS_DIR) -> dict[str, ImageDefinition]:
    """Load ``images/base.yaml`` and every overlay in ``images/overlays/``, keyed by name."""
    if not root.is_dir():
        raise ImageDefinitionError(f"image definitions directory not found: {root}")
    paths = [root / "base.yaml", *sorted((root / "overlays").glob("*.yaml"))]
    definitions: dict[str, ImageDefinition] = {}
    for path in paths:
        definition = load_definition(path, root=root)
        if definition.name in definitions:
            raise ImageDefinitionError(f"duplicate image definition name {definition.name!r}")
        definitions[definition.name] = definition
    for definition in definitions.values():
        if definition.parent and definition.parent not in definitions:
            raise ImageDefinitionError(
                f"{definition.path}: parent {definition.parent!r} is not a known definition"
            )
    return definitions


class CacheEntry(BaseModel):
    """What the harness knows about one built image."""

    model_config = ConfigDict(extra="forbid")

    name: str
    alias: str
    fingerprint: str
    definition_hash: str
    built_at: dt.datetime
    parent_fingerprint: str | None = None


class ImageCache(BaseModel):
    """The on-disk record of built images, at ``paths.image_cache/images.json``."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    images: dict[str, CacheEntry] = Field(default_factory=dict)

    @classmethod
    def load(cls, directory: Path) -> ImageCache:
        path = directory / CACHE_FILE
        if not path.is_file():
            return cls()
        try:
            return cls.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeFailure(
                f"image cache at {path} is unreadable: {exc}. Delete it and rebuild."
            ) from exc

    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / CACHE_FILE).write_text(
            json.dumps(self.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
        )


@dataclass
class BuildResult:
    """One definition's outcome from a build pass."""

    name: str
    alias: str
    fingerprint: str
    rebuilt: bool
    duration_s: float = 0.0


@dataclass
class ImageStatus:
    """One definition's state as reported by ``segbench image status``."""

    name: str
    alias: str
    #: Fingerprint currently in LXD, or ``None`` when the alias does not resolve.
    fingerprint: str | None
    cached_fingerprint: str | None
    definition_hash: str
    cached_definition_hash: str | None
    built_at: dt.datetime | None

    @property
    def built(self) -> bool:
        return self.fingerprint is not None

    @property
    def stale(self) -> bool:
        """True when the image needs rebuilding before it can back a campaign.

        Three ways to be stale: never built, built from a different definition, or present in LXD
        under a fingerprint the cache does not recognise (someone rebuilt it by hand).
        """
        return (
            not self.built
            or self.cached_definition_hash != self.definition_hash
            or self.cached_fingerprint != self.fingerprint
        )

    @property
    def summary(self) -> str:
        if not self.built:
            return "missing"
        if self.cached_definition_hash != self.definition_hash:
            return "stale (definition changed)"
        if self.cached_fingerprint != self.fingerprint:
            return "stale (rebuilt outside segbench)"
        return "ok"


@dataclass
class ImageBuilder:
    """Builds definitions into local LXD images and maintains the cache metadata."""

    runtime: LXDRuntime
    cache_dir: Path
    definitions_dir: Path = DEFINITIONS_DIR
    #: Seconds allowed for one provisioning exec. Package installation over a slow archive
    #: mirror is genuinely minutes, so this is generous on purpose.
    provision_timeout_s: float = 1800.0
    _definitions: dict[str, ImageDefinition] = field(default_factory=dict, init=False)

    def definitions(self) -> dict[str, ImageDefinition]:
        if not self._definitions:
            self._definitions = load_definitions(self.definitions_dir)
        return self._definitions

    def status(self, names: list[str] | None = None) -> list[ImageStatus]:
        """Report each definition's state, re-reading fingerprints from LXD."""
        cache = ImageCache.load(self.cache_dir)
        out: list[ImageStatus] = []
        for name, definition in self.definitions().items():
            if names and name not in names:
                continue
            entry = cache.images.get(name)
            fingerprint: str | None
            try:
                fingerprint = self.runtime.image_digest(definition.alias)
            except RuntimeFailure:
                fingerprint = None
            out.append(
                ImageStatus(
                    name=name,
                    alias=definition.alias,
                    fingerprint=fingerprint,
                    cached_fingerprint=entry.fingerprint if entry else None,
                    definition_hash=definition.definition_hash(),
                    cached_definition_hash=entry.definition_hash if entry else None,
                    built_at=entry.built_at if entry else None,
                )
            )
        return out

    def build(self, name: str = "base", *, force: bool = False) -> list[BuildResult]:
        """Build ``name``, building its parent chain first. Idempotent unless ``force``.

        Returns one result per definition touched, parents first, each saying whether it was
        actually rebuilt.
        """
        definitions = self.definitions()
        if name not in definitions:
            raise ImageDefinitionError(
                f"unknown image {name!r}; known: {', '.join(sorted(definitions))}"
            )
        chain: list[ImageDefinition] = []
        current: ImageDefinition | None = definitions[name]
        while current is not None:
            chain.append(current)
            current = definitions[current.parent] if current.parent else None
        chain.reverse()

        results: list[BuildResult] = []
        parent_rebuilt = False
        for definition in chain:
            result = self._build_one(
                definition, force=force or parent_rebuilt, parent_rebuilt=parent_rebuilt
            )
            parent_rebuilt = parent_rebuilt or result.rebuilt
            results.append(result)
        return results

    def _build_one(
        self, definition: ImageDefinition, *, force: bool, parent_rebuilt: bool
    ) -> BuildResult:
        cache = ImageCache.load(self.cache_dir)
        entry = cache.images.get(definition.name)
        definition_hash = definition.definition_hash()

        if not force and entry is not None and entry.definition_hash == definition_hash:
            try:
                fingerprint = self.runtime.image_digest(definition.alias)
            except RuntimeFailure:
                fingerprint = None
            if fingerprint is not None and fingerprint == entry.fingerprint:
                log.info(
                    "image up to date",
                    extra={"image": definition.name, "fingerprint": fingerprint},
                )
                return BuildResult(
                    name=definition.name,
                    alias=definition.alias,
                    fingerprint=fingerprint,
                    rebuilt=False,
                )

        if parent_rebuilt:
            log.info("parent image was rebuilt, rebuilding", extra={"image": definition.name})

        started = dt.datetime.now(tz=dt.UTC)
        fingerprint = self._provision_and_publish(definition)
        duration = (dt.datetime.now(tz=dt.UTC) - started).total_seconds()

        parent_fingerprint = None
        if definition.parent:
            parent_entry = ImageCache.load(self.cache_dir).images.get(definition.parent)
            parent_fingerprint = parent_entry.fingerprint if parent_entry else None

        cache = ImageCache.load(self.cache_dir)
        cache.images[definition.name] = CacheEntry(
            name=definition.name,
            alias=definition.alias,
            fingerprint=fingerprint,
            definition_hash=definition_hash,
            built_at=started,
            parent_fingerprint=parent_fingerprint,
        )
        cache.save(self.cache_dir)
        log.info(
            "image built",
            extra={
                "image": definition.name,
                "alias": definition.alias,
                "fingerprint": fingerprint,
                "duration_s": round(duration, 1),
            },
        )
        return BuildResult(
            name=definition.name,
            alias=definition.alias,
            fingerprint=fingerprint,
            rebuilt=True,
            duration_s=duration,
        )

    def _source_image(self, definition: ImageDefinition) -> str:
        if definition.source is not None:
            return definition.source.reference
        parent = self.definitions()[definition.parent or ""]
        if not self.runtime.image_exists(parent.alias):
            raise ImageDefinitionError(
                f"overlay {definition.name!r} needs image {parent.alias!r}, which does not exist; "
                f"build it first with `segbench image build --overlay {parent.name}`"
            )
        return parent.alias

    def _provision_and_publish(self, definition: ImageDefinition) -> str:
        """Launch a build container, provision it, stop it, and publish it under the alias.

        The build container gets the default (unpoliced) network because it must reach the Ubuntu
        archive and the opencode installer. This is the only place in the harness where a
        container has real egress, and it never runs agent code.
        """
        source = self._source_image(definition)
        spec = ContainerSpec(
            image=source,
            name=sanitise_name(f"segbench-build-{definition.name}"),
            network=UNPOLICED,
            env={"DEBIAN_FRONTEND": "noninteractive"},
            wait_for_network=True,
            ready_timeout_s=180.0,
        )
        log.info(
            "building image",
            extra={"image": definition.name, "source": source, "container": spec.name},
        )
        handle = self.runtime.create(spec)
        try:
            self._wait_for_cloud_init(handle)
            if definition.packages:
                # DPkg::Lock::Timeout makes apt wait for the lock itself rather than failing
                # instantly if a first-boot job is still holding it.
                lock = "-o=DPkg::Lock::Timeout=300"
                self.runtime.exec(
                    handle,
                    ["apt-get", lock, "update", "-qq"],
                    timeout=self.provision_timeout_s,
                    check=True,
                )
                self.runtime.exec(
                    handle,
                    [
                        "apt-get",
                        lock,
                        "install",
                        "-y",
                        "--no-install-recommends",
                        *definition.packages,
                    ],
                    env={"DEBIAN_FRONTEND": "noninteractive"},
                    timeout=self.provision_timeout_s,
                    check=True,
                )
            script = definition.provision_script()
            if script is not None:
                self.runtime.push(handle, script, "/tmp/provision.sh", mode="0755")
                self.runtime.exec(
                    handle,
                    ["/bin/bash", "/tmp/provision.sh"],
                    env={
                        "DEBIAN_FRONTEND": "noninteractive",
                        "SEGBENCH_IMAGE_NAME": definition.name,
                        "SEGBENCH_AGENT_USER": definition.agent_user,
                    },
                    timeout=self.provision_timeout_s,
                    check=True,
                )
                self.runtime.exec(handle, ["rm", "-f", "/tmp/provision.sh"], timeout=60.0)

            # Publishing requires a stopped instance, and the existing alias must go first: LXD
            # refuses to publish onto an alias that is already taken.
            self.runtime.stop(handle)
            self.runtime.run_lxc("image", "delete", definition.alias, check=False, timeout=120.0)
            self.runtime.run_lxc(
                "publish",
                handle.name,
                "--alias",
                definition.alias,
                "--reuse",
                f"description={definition.description or definition.name}",
                timeout=1800.0,
            )
        finally:
            self.runtime.destroy(handle)
        return self.runtime.image_digest(definition.alias)

    def _wait_for_cloud_init(self, handle: Handle) -> None:
        """Wait for the cloud image to finish its own first-boot work before touching apt.

        Ubuntu cloud images run cloud-init, ``apt-daily`` and ``unattended-upgrades`` on first
        boot. Racing them produces an intermittent "could not get lock
        /var/lib/dpkg/lock-frontend" that looks like a harness bug and is not.

        ``cloud-init status --wait`` exits non-zero when cloud-init finished in a degraded state,
        which on a container is common and harmless (datasource probing fails). The apt calls
        themselves carry ``DPkg::Lock::Timeout``, so a degraded or absent cloud-init is logged and
        stepped over rather than treated as fatal.
        """
        result = self.runtime.exec(
            handle, ["cloud-init", "status", "--wait"], timeout=self.provision_timeout_s
        )
        if not result.ok:
            log.debug(
                "cloud-init wait returned non-zero; continuing",
                extra={"returncode": result.returncode, "stdout": result.stdout.strip()[:200]},
            )
