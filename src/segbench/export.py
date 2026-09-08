"""Results export (plan.md sections 8, 11 and 13 phase 8).

``segbench export`` joins ``results/runs.jsonl`` with ``results/grades.jsonl``, loads the corpus
for its channel possession/origin metadata, computes every aggregate in :mod:`segbench.aggregate`,
and writes the single versioned ``data.json`` the static dashboard reads. No transcripts: the
per-run table carries only the on-disk transcript *path*, never its contents.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from pydantic import BaseModel

from segbench.agent.run import RunRecord
from segbench.aggregate import (
    Aggregates,
    Joined,
    compute_aggregates,
    join_runs_and_grades,
)
from segbench.config import Settings
from segbench.corpus.loader import load_corpus
from segbench.corpus.models import Bug
from segbench.grade.score import GradeRecord, read_grade_records, read_run_records

SCHEMA_VERSION = 1


class ExportError(Exception):
    """The export could not be built — a bad corpus, unreadable results files, and so on."""


class NetlogSummary(BaseModel):
    total_requests: int
    allowed: int
    denied: int
    leak_attempts: int


def _netlog_summary(run: RunRecord) -> NetlogSummary:
    netlog_path = Path(run.results_dir) / "netlog.jsonl"
    total = allowed = denied = leaks = 0
    if netlog_path.is_file():
        for line in netlog_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += 1
            if entry.get("allowed"):
                allowed += 1
            else:
                denied += 1
            if entry.get("leak_attempt"):
                leaks += 1
    return NetlogSummary(total_requests=total, allowed=allowed, denied=denied, leak_attempts=leaks)


def _answer_fields(run: RunRecord) -> tuple[str | None, str | None]:
    """``(root_cause, proposed_fix)`` from the on-disk answer file, or ``(None, None)`` if the run
    has none (no_answer/invalid_answer/timeout/harness_error) — read loosely (not re-validated
    against the schema), since a malformed answer must still be exportable for the drilldown to
    show what went wrong."""
    answer_path = Path(run.results_dir) / "answer.json"
    if not answer_path.is_file():
        return None, None
    try:
        raw = json.loads(answer_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, None
    if not isinstance(raw, dict):
        return None, None
    return raw.get("root_cause"), raw.get("proposed_fix")


class RunRow(BaseModel):
    """One row of the per-run drilldown table (plan.md section 11: "Run drilldown")."""

    run_id: str
    bug_id: str
    environment: str
    channel_set: str
    model: str
    outcome: str
    score: float
    diagnosis: float
    localisation: float
    remedy: float
    component_match: bool | None
    file_hit: bool | None
    file_f1: float | None
    symbol_hit: bool | None
    root_cause: str | None
    proposed_fix: str | None
    root_cause_rationale: str | None
    fix_rationale: str | None
    ground_truth_root_cause: str
    unsupported_specifics: int | None
    contradicts_ground_truth: bool | None
    cost_usd: float
    duration_s: float
    leak_attempts: int
    netlog: NetlogSummary
    transcript_path: str


def _run_row(j: Joined, ground_truth_by_bug: dict[str, str]) -> RunRow:
    run, grade = j.run, j.grade
    root_cause, proposed_fix = _answer_fields(run)
    return RunRow(
        run_id=run.run_id,
        bug_id=run.bug_id,
        environment=run.environment,
        channel_set=run.channel_set,
        model=run.model,
        outcome=grade.outcome,
        score=grade.score,
        diagnosis=grade.diagnosis,
        localisation=grade.localisation,
        remedy=grade.remedy,
        component_match=grade.component_match,
        file_hit=grade.file_hit,
        file_f1=grade.file_f1,
        symbol_hit=grade.symbol_hit,
        root_cause=root_cause,
        proposed_fix=proposed_fix,
        root_cause_rationale=grade.root_cause_rationale,
        fix_rationale=grade.fix_rationale,
        ground_truth_root_cause=ground_truth_by_bug.get(run.bug_id, ""),
        unsupported_specifics=grade.unsupported_specifics,
        contradicts_ground_truth=grade.contradicts_ground_truth,
        cost_usd=run.cost.usd,
        duration_s=run.duration_s,
        leak_attempts=run.leak_attempts,
        netlog=_netlog_summary(run),
        transcript_path=str(Path(run.results_dir) / "transcript.jsonl"),
    )


class CampaignMetadata(BaseModel):
    generated_at: str
    repeats: int
    models: list[str]
    environments: list[str]
    judge_model: str
    judge_prompt_version: str
    weights: dict[str, float]
    corpus_revision: str | None


def _campaign_metadata(settings: Settings, runs: list[RunRecord]) -> CampaignMetadata:
    corpus_revision = next((r.corpus_revision for r in runs if r.corpus_revision), None)
    scoring = settings.scoring
    return CampaignMetadata(
        generated_at=dt.datetime.now(dt.UTC).isoformat(),
        repeats=settings.caps.repeats,
        models=[m.id for m in settings.models] or sorted({r.model for r in runs}),
        environments=sorted({r.environment for r in runs}),
        judge_model=settings.judge.model,
        judge_prompt_version=settings.judge.prompt_version,
        weights={
            "diagnosis_weight": scoring.diagnosis_weight,
            "localisation_weight": scoring.localisation_weight,
            "remedy_weight": scoring.remedy_weight,
            "component_weight": scoring.component_weight,
            "file_f1_weight": scoring.file_f1_weight,
            "symbol_weight": scoring.symbol_weight,
            "unsupported_specific_penalty": scoring.unsupported_specific_penalty,
            "max_penalty": scoring.max_penalty,
        },
        corpus_revision=corpus_revision,
    )


class ExportDocument(BaseModel):
    """The single JSON document ``segbench export`` writes (plan.md section 11)."""

    schema_version: int
    campaign: CampaignMetadata
    aggregates: Aggregates
    runs: list[RunRow]


def build_export(
    settings: Settings,
    *,
    bugs: list[Bug] | None = None,
    runs: list[RunRecord] | None = None,
    grades: dict[str, GradeRecord] | None = None,
    low_confidence_n: int = 10,
) -> ExportDocument:
    """Build the export document from disk (or from the given in-memory records — the synthetic
    fixture generator and tests pass these in directly rather than round-tripping through files).
    """
    if bugs is None:
        bugs, errors = load_corpus(settings.paths.corpus)
        if errors:
            raise ExportError(
                "corpus failed to load; run `segbench corpus validate` first:\n"
                + "\n".join(str(e) for e in errors)
            )
    if runs is None:
        runs = read_run_records(Path(settings.paths.results) / "runs.jsonl")
    if grades is None:
        grades = read_grade_records(Path(settings.paths.results) / "grades.jsonl")

    joined = join_runs_and_grades(runs, grades)
    aggregates = compute_aggregates(settings, bugs, joined, low_confidence_n=low_confidence_n)
    ground_truth_by_bug = {bug.id: bug.ground_truth.root_cause for bug in bugs}
    run_rows = [_run_row(j, ground_truth_by_bug) for j in joined]

    return ExportDocument(
        schema_version=SCHEMA_VERSION,
        campaign=_campaign_metadata(settings, runs),
        aggregates=aggregates,
        runs=run_rows,
    )


def write_export(document: ExportDocument, out: Path) -> None:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(document.model_dump_json(indent=2), encoding="utf-8")
