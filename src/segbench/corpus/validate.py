"""Compose the three check families into one corpus validation pass.

``segbench corpus validate`` is the gate that stands between the maintainer and an invalid
benchmark, so it runs everything in one pass and reports everything it finds: schema conformance
via the loader, the scrubber over channels *and* attachments, and the leak checks over channels.

Exit rule: any :attr:`Severity.ERROR` finding, or any bug that failed to load, fails the run.
Warnings — a similarity flag, an unscrubbable binary attachment, an unreviewed ground truth — are
printed and do not.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from segbench.corpus import leakage, scrub
from segbench.corpus.findings import Finding, Severity
from segbench.corpus.loader import CorpusError, load_corpus
from segbench.corpus.models import Bug
from segbench.corpus.scrub import ScrubPolicy


class BugReport(BaseModel):
    """The outcome for one bug."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    bug_id: str
    ready: bool
    channel_count: int
    findings: list[Finding]

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors


class CorpusReport(BaseModel):
    """The outcome for the whole pass."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    bugs: list[BugReport]
    load_errors: list[str]

    @property
    def ok(self) -> bool:
        """False if anything failed to load or produced an error finding."""
        return not self.load_errors and all(b.ok for b in self.bugs)

    @property
    def error_count(self) -> int:
        return sum(len(b.errors) for b in self.bugs) + len(self.load_errors)

    @property
    def warning_count(self) -> int:
        return sum(len(b.warnings) for b in self.bugs)


def attachment_paths(bug: Bug) -> list[tuple[Path, Path]]:
    """Every file under the bug's ``attachments/`` directory, as (actual, display) pairs."""
    root = bug.directory / "attachments"
    if not root.is_dir():
        return []
    return [(path, path) for path in sorted(root.rglob("*")) if path.is_file()]


def validate_bug(
    bug: Bug,
    *,
    policy: ScrubPolicy = scrub.DEFAULT_POLICY,
    similarity_threshold: float = leakage.DEFAULT_SIMILARITY_THRESHOLD,
) -> BugReport:
    """Run the scrubber and the leak checks over one already-loaded bug."""
    findings: list[Finding] = []

    findings.extend(
        scrub.scan_paths([(c.path, c.path) for c in bug.channels], bug_id=bug.id, policy=policy)
    )
    findings.extend(scrub.scan_paths(attachment_paths(bug), bug_id=bug.id, policy=policy))
    findings.extend(leakage.check_bug(bug, similarity_threshold=similarity_threshold))

    if not bug.ready:
        findings.append(
            Finding(
                bug_id=bug.id,
                detector="not_ready",
                severity=Severity.WARNING,
                file=bug.directory / "ground_truth.yaml",
                message=(
                    f"{bug.not_ready_reason}; this bug is excluded from campaigns unless "
                    f"--include-unreviewed is passed"
                ),
            )
        )

    return BugReport(
        bug_id=bug.id,
        ready=bug.ready,
        channel_count=len(bug.channels),
        findings=findings,
    )


def validate_corpus(
    corpus_root: Path,
    *,
    bug_ids: list[str] | None = None,
    policy: ScrubPolicy = scrub.DEFAULT_POLICY,
    similarity_threshold: float = leakage.DEFAULT_SIMILARITY_THRESHOLD,
) -> CorpusReport:
    """Load and check every bug (or a named subset), collecting all problems in one pass."""
    bugs, errors = load_corpus(corpus_root, bug_ids=bug_ids, include_unreviewed=True)
    reports = [
        validate_bug(bug, policy=policy, similarity_threshold=similarity_threshold) for bug in bugs
    ]
    return CorpusReport(
        bugs=reports,
        load_errors=[_render(exc) for exc in errors],
    )


def _render(exc: CorpusError) -> str:
    return str(exc)
