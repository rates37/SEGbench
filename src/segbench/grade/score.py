"""Composite score (plan.md section 7.3) and the ``grade`` command's orchestration.

A :class:`GradeRecord` is deliberately self-contained: the full weight vector, the judge's prompt
version and the judge's model string are stamped onto every record, so a dashboard reading
``results/grades.jsonl`` can never mix scoring regimes without noticing (plan.md section 7.3).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from pydantic import BaseModel

from segbench.agent.run import RunRecord
from segbench.agent.schema import Answer, validate_answer_file
from segbench.config import ModelConfig, Settings
from segbench.corpus.models import GroundTruth
from segbench.grade.deterministic import DeterministicResult, score_deterministic
from segbench.grade.judge import JudgeError, JudgeInput, JudgeResult, call_judge
from segbench.logging import get_logger

log = get_logger(__name__)

#: Outcomes for which the graded score is unconditionally zero — there is no valid answer to
#: judge or check deterministically (plan.md section 7.3).
ZERO_SCORE_OUTCOMES = frozenset({"no_answer", "invalid_answer", "timeout", "cost_exceeded"})


class GradeError(Exception):
    """A run could not be graded at all — distinct from a run that legitimately scores zero."""


class Weights(BaseModel):
    """The weight vector stamped onto every grade record, read from :class:`ScoringConfig`."""

    diagnosis_weight: float
    localisation_weight: float
    remedy_weight: float
    component_weight: float
    file_f1_weight: float
    symbol_weight: float
    unsupported_specific_penalty: float
    max_penalty: float


class GradeRecord(BaseModel):
    """One line of ``results/grades.jsonl``, joined to ``results/runs.jsonl`` on ``run_id``."""

    run_id: str
    bug_id: str
    model: str
    environment: str
    channel_set: str
    outcome: str

    component_match: bool | None = None
    file_hit: bool | None = None
    file_f1: float | None = None
    symbol_hit: bool | None = None

    root_cause_score: int | None = None
    root_cause_rationale: str | None = None
    fix_score: int | None = None
    fix_rationale: str | None = None
    contradicts_ground_truth: bool | None = None
    unsupported_specifics: int | None = None

    diagnosis: float
    localisation: float
    remedy: float
    penalty: float
    score: float

    weights: Weights
    judge_model: str
    judge_prompt_version: str
    graded_at: str


def _weights_from_config(settings: Settings) -> Weights:
    scoring = settings.scoring
    return Weights(
        diagnosis_weight=scoring.diagnosis_weight,
        localisation_weight=scoring.localisation_weight,
        remedy_weight=scoring.remedy_weight,
        component_weight=scoring.component_weight,
        file_f1_weight=scoring.file_f1_weight,
        symbol_weight=scoring.symbol_weight,
        unsupported_specific_penalty=scoring.unsupported_specific_penalty,
        max_penalty=scoring.max_penalty,
    )


def compose_localisation(det: DeterministicResult, weights: Weights) -> float:
    """``localisation`` per plan.md section 7.3, redistributing the symbol term's weight over the
    other two when ``symbol_hit`` is ``None`` (the bug's language has no symbol extractor)."""
    component_term = weights.component_weight * (1.0 if det.component_match else 0.0)
    file_term = weights.file_f1_weight * det.file_f1
    if det.symbol_hit is None:
        denom = weights.component_weight + weights.file_f1_weight
        if denom <= 0:
            return 0.0
        return (component_term + file_term) / denom
    symbol_term = weights.symbol_weight * (1.0 if det.symbol_hit else 0.0)
    return component_term + file_term + symbol_term


def compose_score(
    det: DeterministicResult, judge: JudgeResult, weights: Weights
) -> tuple[float, float, float, float, float]:
    """Return ``(diagnosis, localisation, remedy, penalty, final)`` per plan.md section 7.3."""
    diagnosis = judge.root_cause_score / 3
    remedy = judge.fix_score / 3
    localisation = compose_localisation(det, weights)
    score = (
        weights.diagnosis_weight * diagnosis
        + weights.localisation_weight * localisation
        + weights.remedy_weight * remedy
    )
    penalty = min(
        weights.max_penalty, weights.unsupported_specific_penalty * judge.unsupported_specifics
    )
    final = max(0.0, score - penalty)
    return diagnosis, localisation, remedy, penalty, final


def _zero_record(run: RunRecord, weights: Weights, settings: Settings) -> GradeRecord:
    """A grade record for an outcome with no answer to score (plan.md section 7.3: everything but
    ``harness_error`` scores zero rather than being dropped)."""
    return GradeRecord(
        run_id=run.run_id,
        bug_id=run.bug_id,
        model=run.model,
        environment=run.environment,
        channel_set=run.channel_set,
        outcome=run.outcome,
        diagnosis=0.0,
        localisation=0.0,
        remedy=0.0,
        penalty=0.0,
        score=0.0,
        weights=weights,
        judge_model=settings.judge.model,
        judge_prompt_version=settings.judge.prompt_version,
        graded_at=dt.datetime.now(dt.UTC).isoformat(),
    )


def grade_run(
    settings: Settings,
    run: RunRecord,
    ground_truth: GroundTruth,
    *,
    models: list[ModelConfig] | None = None,
    judge_client=None,
) -> GradeRecord:
    """Grade one run record. Reads the answer from ``run.results_dir``, never re-runs inference
    against the model under test — the judge call is the only inference this makes, and it is
    against the pinned judge model, never ``run.model``."""
    weights = _weights_from_config(settings)

    if run.outcome == "harness_error":
        return _zero_record(run, weights, settings)
    if run.outcome in ZERO_SCORE_OUTCOMES:
        return _zero_record(run, weights, settings)
    if run.outcome != "ok":
        raise GradeError(f"run {run.run_id!r} has unrecognised outcome {run.outcome!r}")

    answer_result = validate_answer_file(Path(run.results_dir) / "answer.json")
    if not answer_result.ok or answer_result.answer is None:
        # The run claimed "ok" but the answer file is gone/corrupt on disk since it ran — treat
        # it the same as a harness-side grading gap, not a silent zero.
        raise GradeError(
            f"run {run.run_id!r} outcome is 'ok' but its answer file did not validate: "
            f"{answer_result.reason}"
        )
    answer: Answer = answer_result.answer

    det = score_deterministic(answer, ground_truth)
    judge_input = JudgeInput(
        ground_truth_root_cause=ground_truth.root_cause,
        also_acceptable_root_causes=ground_truth.also_acceptable_root_causes,
        agent_root_cause=answer.root_cause,
        agent_proposed_fix=answer.proposed_fix,
    )
    try:
        judge = call_judge(settings, judge_input, models=models, client=judge_client)
    except JudgeError as exc:
        raise GradeError(f"run {run.run_id!r}: {exc}") from exc

    diagnosis, localisation, remedy, penalty, final = compose_score(det, judge, weights)

    return GradeRecord(
        run_id=run.run_id,
        bug_id=run.bug_id,
        model=run.model,
        environment=run.environment,
        channel_set=run.channel_set,
        outcome=run.outcome,
        component_match=det.component_match,
        file_hit=det.file_hit,
        file_f1=det.file_f1,
        symbol_hit=det.symbol_hit,
        root_cause_score=judge.root_cause_score,
        root_cause_rationale=judge.root_cause_rationale,
        fix_score=judge.fix_score,
        fix_rationale=judge.fix_rationale,
        contradicts_ground_truth=judge.contradicts_ground_truth,
        unsupported_specifics=judge.unsupported_specifics,
        diagnosis=diagnosis,
        localisation=localisation,
        remedy=remedy,
        penalty=penalty,
        score=final,
        weights=weights,
        judge_model=settings.judge.model,
        judge_prompt_version=settings.judge.prompt_version,
        graded_at=dt.datetime.now(dt.UTC).isoformat(),
    )


def read_run_records(path: Path) -> list[RunRecord]:
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(RunRecord.model_validate_json(line))
    return records


def read_grade_records(path: Path) -> dict[str, GradeRecord]:
    """Latest grade per ``run_id``. Later lines in the file win, so appending a re-grade and
    rewriting via :func:`write_grade_records` is how "supersede rather than duplicate" works."""
    if not path.is_file():
        return {}
    by_run_id: dict[str, GradeRecord] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = GradeRecord.model_validate_json(line)
            by_run_id[record.run_id] = record
    return by_run_id


def write_grade_records(path: Path, by_run_id: dict[str, GradeRecord]) -> None:
    """Rewrite the whole file from the in-memory map — the mechanism behind idempotent
    re-grading: a regraded run's old line is gone, not appended after."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [record.model_dump_json() for record in by_run_id.values()]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
