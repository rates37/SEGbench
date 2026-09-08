"""Orchestration for ``segbench grade`` (plan.md section 7, section 13 phase 6).

Reads ``results/runs.jsonl``, grades whichever subset is asked for, and rewrites
``results/grades.jsonl`` from the merged map of latest-per-``run_id`` records — this is what makes
``--regrade`` idempotent (plan.md section 7.3 acceptance check): the file is never appended to
with a stale duplicate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from segbench.config import Settings
from segbench.corpus.loader import CorpusError, load_bug
from segbench.corpus.models import GroundTruth
from segbench.grade.score import (
    GradeError,
    GradeRecord,
    grade_run,
    read_grade_records,
    read_run_records,
    write_grade_records,
)
from segbench.logging import get_logger

log = get_logger(__name__)


@dataclass
class GradingSummary:
    graded: list[GradeRecord] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # run_ids left alone (already graded)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (run_id, error)


def _ground_truth_for(
    settings: Settings, bug_id: str, cache: dict[str, GroundTruth]
) -> GroundTruth:
    if bug_id in cache:
        return cache[bug_id]
    loaded = load_bug(settings.paths.corpus / "bugs" / bug_id)
    cache[bug_id] = loaded.ground_truth
    return cache[bug_id]


def run_grading(
    settings: Settings,
    *,
    pending: bool = False,
    regrade: bool = False,
    run_id: str | None = None,
) -> GradingSummary:
    """Grade runs from ``results/runs.jsonl`` into ``results/grades.jsonl``.

    - default (no flags): grade every run without an existing grade record.
    - ``pending``: same as the default; kept as an explicit, self-documenting flag.
    - ``regrade``: also re-grade runs that already have a grade record.
    - ``run_id``: restrict to one run, combinable with ``regrade``.
    """
    runs_path = Path(settings.paths.results) / "runs.jsonl"
    grades_path = Path(settings.paths.results) / "grades.jsonl"

    runs = read_run_records(runs_path)
    if run_id is not None:
        runs = [r for r in runs if r.run_id == run_id]

    existing = read_grade_records(grades_path)
    summary = GradingSummary()
    gt_cache: dict[str, GroundTruth] = {}

    for run in runs:
        already_graded = run.run_id in existing
        if already_graded and not regrade:
            summary.skipped.append(run.run_id)
            continue
        try:
            ground_truth = _ground_truth_for(settings, run.bug_id, gt_cache)
            record = grade_run(settings, run, ground_truth, models=settings.models)
        except (GradeError, CorpusError) as exc:
            summary.failed.append((run.run_id, str(exc)))
            log.error("grading failed", extra={"run_id": run.run_id, "error": str(exc)})
            continue
        existing[run.run_id] = record
        summary.graded.append(record)

    write_grade_records(grades_path, existing)
    return summary
