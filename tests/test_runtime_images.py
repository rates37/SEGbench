"""Image definitions, the cache, and the builder's idempotence.

The builder is exercised against a fake :class:`LXDRuntime` rather than real LXD: a build takes
minutes and needs network, and none of the logic under test here is about LXD's behaviour. The
one thing the fake models faithfully is that an image only exists once it has been published.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from segbench.runtime.base import ContainerSpec, ExecResult, Handle, RuntimeFailure
from segbench.runtime.images import (
    CacheEntry,
    ImageBuilder,
    ImageCache,
    ImageDefinitionError,
    load_definition,
    load_definitions,
)

DEFINITIONS = Path(__file__).resolve().parents[1] / "images"


class FakeLXD:
    """Enough of :class:`LXDRuntime` for the builder: launch, exec, push, publish, delete."""

    name = "lxd"

    def __init__(self) -> None:
        self.images: dict[str, str] = {"ubuntu:24.04": "f" * 64}
        self.live: set[str] = set()
        self.execs: list[list[str]] = []
        self.pushes: list[tuple[str, str]] = []
        self.publishes: list[str] = []
        self._counter = 0

    # lifecycle -------------------------------------------------------------

    def image_digest(self, image: str) -> str:
        if image not in self.images:
            raise RuntimeFailure(f"no such image: {image!r}")
        return self.images[image]

    def image_exists(self, alias: str) -> bool:
        return alias in self.images

    def create(self, spec: ContainerSpec) -> Handle:
        self.live.add(spec.name)
        return Handle(
            name=spec.name,
            backend=self.name,
            image=spec.image,
            image_digest=self.image_digest(spec.image),
        )

    def stop(self, handle: Handle) -> None:
        pass

    def destroy(self, handle: Handle) -> None:
        self.live.discard(handle.name)

    # operations ------------------------------------------------------------

    def exec(self, handle: Handle, argv: list[str], **kwargs: object) -> ExecResult:
        self.execs.append(list(argv))
        return ExecResult(argv=tuple(argv), returncode=0, stdout="", stderr="", duration_s=0.0)

    def push(self, handle: Handle, local: Path, remote: str, *, mode: str | None = None) -> None:
        self.pushes.append((str(local), remote))

    def run_lxc(self, *args: str, **kwargs: object) -> object:
        if args[:2] == ("image", "delete"):
            self.images.pop(args[2], None)
        elif args[0] == "publish":
            alias = args[args.index("--alias") + 1]
            self._counter += 1
            self.images[alias] = f"{self._counter:064x}"
            self.publishes.append(alias)
        return None


@pytest.fixture
def builder(tmp_path: Path) -> ImageBuilder:
    return ImageBuilder(
        runtime=FakeLXD(),  # type: ignore[arg-type]
        cache_dir=tmp_path / "cache",
        definitions_dir=DEFINITIONS,
    )


class TestDefinitions:
    def test_the_shipped_definitions_load(self) -> None:
        definitions = load_definitions(DEFINITIONS)

        assert set(definitions) == {"base", "kernel"}
        assert definitions["base"].alias == "segbench-base"
        assert definitions["kernel"].parent == "base"

    def test_the_base_image_carries_what_plan_section_9_requires(self) -> None:
        packages = set(load_definitions(DEFINITIONS)["base"].packages)

        assert {"git", "ripgrep", "fd-find", "jq", "build-essential"} <= packages
        assert any(p.startswith("python3") for p in packages)

    def test_the_base_installs_opencode(self) -> None:
        base = load_definitions(DEFINITIONS)["base"]
        script = base.provision_script()

        assert script is not None
        assert "opencode" in script.read_text(encoding="utf-8")

    def test_the_kernel_overlay_is_keyed_on_product(self) -> None:
        kernel = load_definitions(DEFINITIONS)["kernel"]

        assert kernel.products == ["kernel"]
        assert "crash" in kernel.packages

    def test_a_definition_cannot_ask_for_privilege(self, tmp_path: Path) -> None:
        """No image needs it today, and granting it must be a deliberate schema change.

        The build container is where a privilege request would be easiest to slip in unnoticed,
        so the definition schema simply has no key for it.
        """
        path = tmp_path / "greedy.yaml"
        path.write_text(
            "name: x\nalias: x\nsource:\n  image: '24.04'\nprivileged: true\n", encoding="utf-8"
        )

        with pytest.raises(ImageDefinitionError):
            load_definition(path)

    def test_a_definition_with_neither_source_nor_parent_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("name: x\nalias: x\n", encoding="utf-8")

        with pytest.raises(ImageDefinitionError, match="exactly one"):
            load_definition(path)

    def test_a_definition_with_both_source_and_parent_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("name: x\nalias: x\nparent: base\nsource:\n  image: '24.04'\n", "utf-8")

        with pytest.raises(ImageDefinitionError, match="exactly one"):
            load_definition(path)

    def test_a_missing_provision_script_fails_at_load(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text(
            "name: x\nalias: x\nsource:\n  image: '24.04'\nprovision: nope.sh\n", encoding="utf-8"
        )

        with pytest.raises(ImageDefinitionError, match="provision script not found"):
            load_definition(path)

    def test_an_unknown_key_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("name: x\nalias: x\nsource:\n  image: '24.04'\ntypo: 1\n", encoding="utf-8")

        with pytest.raises(ImageDefinitionError):
            load_definition(path)

    def test_an_overlay_naming_an_unknown_parent_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "overlays").mkdir()
        (tmp_path / "base.yaml").write_text(
            "name: base\nalias: b\nsource:\n  image: '24.04'\n", encoding="utf-8"
        )
        (tmp_path / "overlays" / "o.yaml").write_text(
            "name: o\nalias: o\nparent: nope\n", encoding="utf-8"
        )

        with pytest.raises(ImageDefinitionError, match="not a known definition"):
            load_definitions(tmp_path)

    def test_definition_hash_tracks_the_provision_script(self, tmp_path: Path) -> None:
        (tmp_path / "base.yaml").write_text(
            "name: base\nalias: b\nsource:\n  image: '24.04'\nprovision: p.sh\n", encoding="utf-8"
        )
        script = tmp_path / "p.sh"
        script.write_text("echo one\n", encoding="utf-8")
        before = load_definition(tmp_path / "base.yaml").definition_hash()

        script.write_text("echo two\n", encoding="utf-8")
        after = load_definition(tmp_path / "base.yaml").definition_hash()

        assert before != after


class TestCache:
    def test_round_trips(self, tmp_path: Path) -> None:
        cache = ImageCache()
        cache.images["base"] = CacheEntry(
            name="base",
            alias="segbench-base",
            fingerprint="a" * 64,
            definition_hash="b" * 64,
            built_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        )
        cache.save(tmp_path)

        assert ImageCache.load(tmp_path).images["base"].fingerprint == "a" * 64

    def test_a_missing_cache_is_empty_not_an_error(self, tmp_path: Path) -> None:
        assert ImageCache.load(tmp_path).images == {}

    def test_a_corrupt_cache_fails_loudly(self, tmp_path: Path) -> None:
        (tmp_path / "images.json").write_text("{ not json", encoding="utf-8")

        with pytest.raises(RuntimeFailure, match="unreadable"):
            ImageCache.load(tmp_path)


class TestBuild:
    def test_builds_the_base_and_records_the_digest(self, builder: ImageBuilder) -> None:
        (result,) = builder.build("base")

        assert result.rebuilt
        assert result.alias == "segbench-base"
        entry = ImageCache.load(builder.cache_dir).images["base"]
        assert entry.fingerprint == result.fingerprint

    def test_installs_packages_and_runs_the_provision_script(self, builder: ImageBuilder) -> None:
        builder.build("base")
        fake: FakeLXD = builder.runtime  # type: ignore[assignment]

        installs = [argv for argv in fake.execs if argv[:1] == ["apt-get"] and "install" in argv]
        assert installs and "ripgrep" in installs[0]
        assert any(remote == "/tmp/provision.sh" for _, remote in fake.pushes)
        assert ["/bin/bash", "/tmp/provision.sh"] in fake.execs

    def test_is_idempotent(self, builder: ImageBuilder) -> None:
        builder.build("base")
        fake: FakeLXD = builder.runtime  # type: ignore[assignment]
        publishes = len(fake.publishes)

        (again,) = builder.build("base")

        assert not again.rebuilt
        assert len(fake.publishes) == publishes

    def test_force_rebuilds(self, builder: ImageBuilder) -> None:
        first = builder.build("base")[0]

        second = builder.build("base", force=True)[0]

        assert second.rebuilt
        assert second.fingerprint != first.fingerprint

    def test_rebuilds_when_the_image_vanished_behind_our_back(self, builder: ImageBuilder) -> None:
        builder.build("base")
        fake: FakeLXD = builder.runtime  # type: ignore[assignment]
        fake.images.pop("segbench-base")

        assert builder.build("base")[0].rebuilt

    def test_an_overlay_builds_its_parent_first(self, builder: ImageBuilder) -> None:
        results = builder.build("kernel")

        assert [r.name for r in results] == ["base", "kernel"]
        fake: FakeLXD = builder.runtime  # type: ignore[assignment]
        assert fake.publishes == ["segbench-base", "segbench-kernel"]

    def test_rebuilding_the_base_rebuilds_the_overlay(self, builder: ImageBuilder) -> None:
        builder.build("kernel")

        results = builder.build("kernel", force=True)

        assert all(r.rebuilt for r in results)

    def test_an_up_to_date_overlay_rebuilds_nothing(self, builder: ImageBuilder) -> None:
        builder.build("kernel")

        results = builder.build("kernel")

        assert not any(r.rebuilt for r in results)

    def test_the_build_container_is_always_destroyed(self, builder: ImageBuilder) -> None:
        builder.build("kernel")
        fake: FakeLXD = builder.runtime  # type: ignore[assignment]

        assert fake.live == set()

    def test_a_failed_build_destroys_its_container(self, builder: ImageBuilder) -> None:
        fake: FakeLXD = builder.runtime  # type: ignore[assignment]

        def explode(*args: object, **kwargs: object) -> ExecResult:
            raise RuntimeFailure("apt-get exploded")

        fake.exec = explode  # type: ignore[method-assign]

        with pytest.raises(RuntimeFailure, match="exploded"):
            builder.build("base")

        assert fake.live == set()

    def test_an_unknown_image_name_is_rejected(self, builder: ImageBuilder) -> None:
        with pytest.raises(ImageDefinitionError, match="unknown image"):
            builder.build("nope")


class TestStatus:
    def test_reports_missing_before_a_build(self, builder: ImageBuilder) -> None:
        statuses = {s.name: s for s in builder.status()}

        assert statuses["base"].summary == "missing"
        assert statuses["base"].stale

    def test_reports_ok_after_a_build(self, builder: ImageBuilder) -> None:
        builder.build("base")

        status = next(s for s in builder.status() if s.name == "base")

        assert status.summary == "ok"
        assert not status.stale
        assert status.fingerprint == status.cached_fingerprint

    def test_detects_an_image_rebuilt_outside_segbench(self, builder: ImageBuilder) -> None:
        builder.build("base")
        fake: FakeLXD = builder.runtime  # type: ignore[assignment]
        fake.images["segbench-base"] = "9" * 64

        status = next(s for s in builder.status() if s.name == "base")

        assert status.stale
        assert "outside segbench" in status.summary

    def test_detects_a_changed_definition(self, builder: ImageBuilder) -> None:
        builder.build("base")
        cache = ImageCache.load(builder.cache_dir)
        cache.images["base"].definition_hash = "0" * 64
        cache.save(builder.cache_dir)

        status = next(s for s in builder.status() if s.name == "base")

        assert status.stale
        assert "definition changed" in status.summary
