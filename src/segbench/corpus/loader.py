"""Load and validate bug directories.

The contract, from CLAUDE.md: *fail loudly on ambiguity in the corpus*. A malformed manifest, a
missing ground-truth field or a channel pointing at a file that is not there must raise here, at
load time, naming the file and the field — never produce a degraded task that quietly runs and
contributes a meaningless number to an aggregate.

The one deliberate exception is review status. A bug whose ``ground_truth.reviewed_by`` is unset
loads fine and is simply marked not-ready: the maintainer has to be able to iterate on a bug before
it is finished. Campaign selection excludes it unless ``--include-unreviewed`` is passed.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from segbench.corpus.models import Bug, BugManifest, GroundTruth, LoadedChannel

BUG_FILE = "bug.yaml"
GROUND_TRUTH_FILE = "ground_truth.yaml"


class CorpusError(Exception):
    """A bug directory cannot be loaded. The message always names the offending file."""


def _read_yaml(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise CorpusError(f"{path}: required file is missing")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CorpusError(f"{path}: not valid YAML: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise CorpusError(f"{path}: not valid UTF-8 text: {exc}") from exc
    if raw is None:
        raise CorpusError(f"{path}: file is empty")
    if not isinstance(raw, dict):
        raise CorpusError(
            f"{path}: expected a YAML mapping at the top level, found {type(raw).__name__}"
        )
    return raw


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    lines = [f"{path}: {exc.error_count()} schema error(s):"]
    for error in exc.errors():
        field = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  - {field}: {error['msg']}")
    return "\n".join(lines)


def load_bug(directory: Path) -> Bug:
    """Load one bug directory into a :class:`Bug`, or raise :class:`CorpusError`."""
    directory = Path(directory)
    if not directory.is_dir():
        raise CorpusError(f"{directory}: not a directory")

    manifest_path = directory / BUG_FILE
    truth_path = directory / GROUND_TRUTH_FILE

    try:
        manifest = BugManifest.model_validate(_read_yaml(manifest_path))
    except ValidationError as exc:
        raise CorpusError(_format_validation_error(manifest_path, exc)) from exc

    if manifest.id != directory.name:
        raise CorpusError(
            f"{manifest_path}: id is {manifest.id!r} but the directory is named "
            f"{directory.name!r}; the two must match so a bug id addresses exactly one directory"
        )

    try:
        ground_truth = GroundTruth.model_validate(_read_yaml(truth_path))
    except ValidationError as exc:
        raise CorpusError(_format_validation_error(truth_path, exc)) from exc

    channels: list[LoadedChannel] = []
    for spec in manifest.channels:
        if spec.file.is_absolute():
            raise CorpusError(
                f"{manifest_path}: channel {spec.id.value!r} file {spec.file} is absolute; "
                f"channel paths must be relative to the bug directory"
            )
        resolved = (directory / spec.file).resolve()
        if not resolved.is_relative_to(directory.resolve()):
            raise CorpusError(
                f"{manifest_path}: channel {spec.id.value!r} file {spec.file} escapes the bug "
                f"directory"
            )
        if not resolved.is_file():
            raise CorpusError(
                f"{manifest_path}: channel {spec.id.value!r} references {spec.file}, which does "
                f"not exist under {directory}"
            )
        try:
            text = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise CorpusError(f"{directory / spec.file}: not valid UTF-8 text: {exc}") from exc
        if not text.strip():
            raise CorpusError(
                f"{directory / spec.file}: channel {spec.id.value!r} is empty; a channel the bug "
                f"does not have must be omitted from bug.yaml, not left blank, or leave-one-out "
                f"will generate a meaningless cell for it"
            )
        channels.append(
            LoadedChannel(id=spec.id, origin=spec.origin, path=directory / spec.file, text=text)
        )

    return Bug(
        directory=directory,
        manifest=manifest,
        ground_truth=ground_truth,
        channels=channels,
    )


def bug_directories(corpus_root: Path) -> list[Path]:
    """Every bug directory under ``<corpus_root>/bugs``, sorted by id."""
    bugs_root = Path(corpus_root) / "bugs"
    if not bugs_root.is_dir():
        return []
    return sorted(
        path
        for path in bugs_root.iterdir()
        if path.is_dir() and not path.name.startswith(".") and (path / BUG_FILE).is_file()
    )


def load_corpus(
    corpus_root: Path,
    *,
    bug_ids: list[str] | None = None,
    include_unreviewed: bool = True,
) -> tuple[list[Bug], list[CorpusError]]:
    """Load every bug (or a named subset).

    Returns loaded bugs and the errors for the ones that failed, rather than raising on the first
    problem: ``corpus validate`` needs to report every broken bug in one pass, not make the
    maintainer fix them one at a time.
    """
    bugs: list[Bug] = []
    errors: list[CorpusError] = []

    directories = bug_directories(corpus_root)
    if bug_ids is not None:
        wanted = set(bug_ids)
        found = {d.name for d in directories}
        for missing in sorted(wanted - found):
            errors.append(
                CorpusError(f"no bug directory named {missing!r} under {corpus_root}/bugs")
            )
        directories = [d for d in directories if d.name in wanted]

    for directory in directories:
        try:
            bug = load_bug(directory)
        except CorpusError as exc:
            errors.append(exc)
            continue
        if not include_unreviewed and not bug.ready:
            continue
        bugs.append(bug)

    return bugs, errors
