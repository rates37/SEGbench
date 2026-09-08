"""Hand-grading workflow for judge validation (plan.md section 7.4).

``segbench grade sample`` emits a *neutral* form per selected run: the agent's answer and the
ground truth, and nothing that could bias a human grader — no model identity, no environment, no
channel set, and no score the judge already assigned. ``segbench grade agreement`` reads the
filled-in forms back and compares the human's scores against the judge's, on the same run, via a
manifest file kept out of the neutral form itself.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from segbench.config import Settings
from segbench.corpus.loader import CorpusError, load_bug
from segbench.grade.score import GradeRecord, read_grade_records, read_run_records
from segbench.logging import get_logger

log = get_logger(__name__)

MANIFEST_FILENAME = "manifest.json"


class ValidationError(Exception):
    """The sample or agreement workflow could not proceed."""


class NeutralForm(BaseModel):
    """One hand-gradable form. Deliberately carries no model identity or prior score."""

    sample_id: str
    ground_truth_root_cause: str
    also_acceptable_root_causes: list[str]
    agent_root_cause: str
    agent_proposed_fix: str
    human_root_cause_score: int | None = None
    human_fix_score: int | None = None
    human_notes: str = ""


def _stratified_sample(records: list[GradeRecord], n: int, *, seed: int) -> list[GradeRecord]:
    """Spread the sample across the judge's score range (plan.md section 7.4) rather than taking
    an unstratified random slice, which would under-represent the tails."""
    graded = sorted((r for r in records if r.outcome == "ok"), key=lambda r: r.score)
    if len(graded) <= n:
        return graded
    rng = random.Random(seed)
    bucket_count = min(10, n)
    buckets: list[list[GradeRecord]] = [[] for _ in range(bucket_count)]
    for i, record in enumerate(graded):
        buckets[min(i * bucket_count // len(graded), bucket_count - 1)].append(record)
    per_bucket = max(1, n // bucket_count)
    selected: list[GradeRecord] = []
    for bucket in buckets:
        rng.shuffle(bucket)
        selected.extend(bucket[:per_bucket])
    rng.shuffle(selected)
    return selected[:n]


def sample_for_hand_grading(
    settings: Settings, *, n: int, out_dir: Path, seed: int = 0
) -> list[Path]:
    """Write ``n`` neutral forms plus a manifest into ``out_dir``. Returns the form paths."""
    grades_path = Path(settings.paths.results) / "grades.jsonl"
    grades = list(read_grade_records(grades_path).values())
    if not grades:
        raise ValidationError(f"no grade records in {grades_path}; run `segbench grade` first")

    runs_path = Path(settings.paths.results) / "runs.jsonl"
    runs_by_id = {r.run_id: r for r in read_run_records(runs_path)}
    selected = _stratified_sample(grades, n, seed=seed)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, str] = {}
    form_paths: list[Path] = []
    for i, grade in enumerate(selected):
        run = runs_by_id.get(grade.run_id)
        if run is None:
            continue
        try:
            ground_truth = load_bug(settings.paths.corpus / "bugs" / grade.bug_id).ground_truth
        except CorpusError as exc:
            log.warning(
                "skipping run for hand-grading sample: bug did not load",
                extra={"run_id": grade.run_id, "error": str(exc)},
            )
            continue
        answer_path = Path(run.results_dir) / "answer.json"
        if not answer_path.is_file():
            continue
        answer = json.loads(answer_path.read_text(encoding="utf-8"))

        sample_id = f"sample-{i:03d}"
        form = NeutralForm(
            sample_id=sample_id,
            ground_truth_root_cause=ground_truth.root_cause,
            also_acceptable_root_causes=ground_truth.also_acceptable_root_causes,
            agent_root_cause=answer["root_cause"],
            agent_proposed_fix=answer["proposed_fix"],
        )
        form_path = out_dir / f"{sample_id}.json"
        form_path.write_text(form.model_dump_json(indent=2) + "\n", encoding="utf-8")
        form_paths.append(form_path)
        manifest[sample_id] = grade.run_id

    manifest_text = json.dumps(manifest, indent=2) + "\n"
    (out_dir / MANIFEST_FILENAME).write_text(manifest_text, encoding="utf-8")
    return form_paths


def _cohens_kappa(a: list[int], b: list[int]) -> float:
    """Unweighted Cohen's kappa over paired categorical scores."""
    n = len(a)
    if n == 0:
        return float("nan")
    labels = sorted(set(a) | set(b))
    observed = sum(1 for x, y in zip(a, b, strict=True) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    expected = sum((ca.get(label, 0) / n) * (cb.get(label, 0) / n) for label in labels)
    if expected >= 1.0:
        return 1.0
    return (observed - expected) / (1 - expected)


@dataclass(frozen=True)
class AgreementStats:
    n: int
    exact_match: float
    within_one: float
    kappa: float


def _agreement_stats(human: list[int], judge: list[int]) -> AgreementStats:
    n = len(human)
    if n == 0:
        nan = float("nan")
        return AgreementStats(n=0, exact_match=nan, within_one=nan, kappa=nan)
    exact = sum(1 for h, j in zip(human, judge, strict=True) if h == j) / n
    within_one = sum(1 for h, j in zip(human, judge, strict=True) if abs(h - j) <= 1) / n
    kappa = _cohens_kappa(human, judge)
    return AgreementStats(n=n, exact_match=exact, within_one=within_one, kappa=kappa)


@dataclass(frozen=True)
class Agreement:
    root_cause: AgreementStats
    fix: AgreementStats


def compute_agreement(settings: Settings, sample_dir: Path) -> Agreement:
    """Compare filled-in hand-graded forms in ``sample_dir`` against the judge's scores."""
    sample_dir = Path(sample_dir)
    manifest_path = sample_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise ValidationError(f"no manifest at {manifest_path}; run `segbench grade sample` first")
    manifest: dict[str, str] = json.loads(manifest_path.read_text(encoding="utf-8"))

    grades = read_grade_records(Path(settings.paths.results) / "grades.jsonl")

    human_rc, judge_rc, human_fix, judge_fix = [], [], [], []
    for sample_id, run_id in manifest.items():
        form_path = sample_dir / f"{sample_id}.json"
        if not form_path.is_file():
            continue
        form = NeutralForm.model_validate_json(form_path.read_text(encoding="utf-8"))
        grade = grades.get(run_id)
        if grade is None or grade.root_cause_score is None or grade.fix_score is None:
            continue
        if form.human_root_cause_score is None or form.human_fix_score is None:
            continue  # not yet hand-graded
        human_rc.append(form.human_root_cause_score)
        judge_rc.append(grade.root_cause_score)
        human_fix.append(form.human_fix_score)
        judge_fix.append(grade.fix_score)

    return Agreement(
        root_cause=_agreement_stats(human_rc, judge_rc),
        fix=_agreement_stats(human_fix, judge_fix),
    )
