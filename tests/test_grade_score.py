"""Composite score, symbol-term redistribution, outcome handling and idempotent re-grading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from segbench.agent.run import RunCaps, RunCost, RunRecord
from segbench.config import Settings
from segbench.corpus.models import FixInfo, GroundTruth, Provenance
from segbench.grade.deterministic import DeterministicResult
from segbench.grade.judge import JudgeResult
from segbench.grade.score import (
    compose_localisation,
    compose_score,
    grade_run,
    read_grade_records,
    write_grade_records,
)


def _weights(settings: Settings):
    from segbench.grade.score import _weights_from_config

    return _weights_from_config(settings)


def test_compose_localisation_with_symbol_hit() -> None:
    weights = _weights(Settings())
    det = DeterministicResult(component_match=True, file_hit=True, file_f1=1.0, symbol_hit=True)
    assert compose_localisation(det, weights) == pytest.approx(1.0)


def test_compose_localisation_redistributes_when_symbol_null() -> None:
    weights = _weights(Settings())
    det = DeterministicResult(component_match=True, file_hit=True, file_f1=1.0, symbol_hit=None)
    # component_weight=0.5, file_f1_weight=0.3 -> both perfect -> redistributed to 1.0
    assert compose_localisation(det, weights) == pytest.approx(1.0)


def test_compose_localisation_redistribution_matches_manual_math() -> None:
    weights = _weights(Settings())
    det = DeterministicResult(component_match=True, file_hit=False, file_f1=0.0, symbol_hit=None)
    expected = weights.component_weight / (weights.component_weight + weights.file_f1_weight)
    assert compose_localisation(det, weights) == pytest.approx(expected)


def test_compose_score_matches_hand_computation() -> None:
    weights = _weights(Settings())
    det = DeterministicResult(component_match=True, file_hit=True, file_f1=0.5, symbol_hit=False)
    judge = JudgeResult(
        root_cause_score=3,
        root_cause_rationale="r",
        fix_score=3,
        fix_rationale="f",
        contradicts_ground_truth=False,
        unsupported_specifics=1,
    )
    diagnosis, localisation, remedy, penalty, final = compose_score(det, judge, weights)
    assert diagnosis == pytest.approx(1.0)
    assert remedy == pytest.approx(1.0)
    expected_loc = weights.component_weight * 1.0 + weights.file_f1_weight * 0.5
    assert localisation == pytest.approx(expected_loc)
    expected_penalty = min(weights.max_penalty, weights.unsupported_specific_penalty * 1)
    assert penalty == pytest.approx(expected_penalty)
    expected_score = (
        weights.diagnosis_weight * diagnosis
        + weights.localisation_weight * localisation
        + weights.remedy_weight * remedy
    )
    assert final == pytest.approx(max(0.0, expected_score - expected_penalty))


def _ground_truth() -> GroundTruth:
    return GroundTruth(
        root_cause="the real root cause",
        component="neutron",
        fix=FixInfo(
            commit="0123456789abcdef",
            files=["neutron/agent/ovn/metadata/agent.py"],
            symbols=["MetadataAgent.sync"],
        ),
        acceptable_components=[],
        also_acceptable_root_causes=[],
        provenance=Provenance.AUTHORED,
    )


def _run_record(results_dir: Path, *, outcome: str, run_id: str = "run-1") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        bug_id="lp-9000001",
        environment="E0",
        channel_set="full",
        model="some/model",
        backend="lxd",
        image="base",
        image_digest="deadbeef",
        corpus_revision=None,
        prompt_version="v1",
        prompt_hash="hash",
        caps=RunCaps(wall_clock_s=300, max_cost_usd=5.0),
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:01:00+00:00",
        duration_s=60.0,
        cost=RunCost(),
        leak_attempts=0,
        outcome=outcome,
        truncated=False,
        answer_outcome="ok" if outcome == "ok" else outcome,
        results_dir=str(results_dir),
    )


def _write_answer(results_dir: Path) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)
    answer = {
        "root_cause": "reconnect skips the datapath cache refresh",
        "component": "neutron",
        "suspect_files": ["neutron/agent/ovn/metadata/agent.py"],
        "suspect_symbols": ["MetadataAgent.sync"],
        "proposed_fix": "repopulate the cache on reconnect",
        "confidence": 0.7,
        "evidence": [],
        "uncertain_about": [],
    }
    (results_dir / "answer.json").write_text(json.dumps(answer), encoding="utf-8")


def test_grade_run_ok_calls_judge_and_composes_score(tmp_path, monkeypatch) -> None:
    results_dir = tmp_path / "run-1"
    _write_answer(results_dir)
    run = _run_record(results_dir, outcome="ok")
    settings = Settings()

    canned = JudgeResult(
        root_cause_score=3,
        root_cause_rationale="r",
        fix_score=3,
        fix_rationale="f",
        contradicts_ground_truth=False,
        unsupported_specifics=0,
    )
    monkeypatch.setattr("segbench.grade.score.call_judge", lambda *a, **k: canned)

    record = grade_run(settings, run, _ground_truth())
    assert record.component_match is True
    assert record.file_hit is True
    assert record.symbol_hit is True
    assert record.score == pytest.approx(1.0)


@pytest.mark.parametrize("outcome", ["no_answer", "invalid_answer", "timeout", "cost_exceeded"])
def test_grade_run_zero_scores_non_ok_outcomes_without_calling_judge(
    tmp_path, monkeypatch, outcome
) -> None:
    results_dir = tmp_path / "run-2"
    run = _run_record(results_dir, outcome=outcome, run_id="run-2")
    settings = Settings()

    def _fail_if_called(*a, **k):
        raise AssertionError("the judge must not be called for a non-ok outcome")

    monkeypatch.setattr("segbench.grade.score.call_judge", _fail_if_called)
    record = grade_run(settings, run, _ground_truth())
    assert record.score == 0.0
    assert record.outcome == outcome


def test_grade_run_harness_error_scores_zero_without_calling_judge(tmp_path, monkeypatch) -> None:
    run = _run_record(tmp_path / "run-3", outcome="harness_error", run_id="run-3")
    settings = Settings()
    monkeypatch.setattr(
        "segbench.grade.score.call_judge",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call judge")),
    )
    record = grade_run(settings, run, _ground_truth())
    assert record.score == 0.0
    assert record.outcome == "harness_error"


def test_changing_a_weight_changes_the_score_without_reinference(tmp_path, monkeypatch) -> None:
    """The acceptance check: a scoring-weight change plus re-grade must change scores with zero
    additional judge calls."""
    results_dir = tmp_path / "run-4"
    _write_answer(results_dir)
    run = _run_record(results_dir, outcome="ok", run_id="run-4")

    canned = JudgeResult(
        root_cause_score=2,
        root_cause_rationale="r",
        fix_score=1,
        fix_rationale="f",
        contradicts_ground_truth=False,
        unsupported_specifics=0,
    )
    call_count = {"n": 0}

    def _counting_judge(*a, **k):
        call_count["n"] += 1
        return canned

    monkeypatch.setattr("segbench.grade.score.call_judge", _counting_judge)

    default_settings = Settings()
    record_before = grade_run(default_settings, run, _ground_truth())

    heavier_remedy_settings = Settings(
        scoring={
            "diagnosis_weight": 0.20,
            "localisation_weight": 0.20,
            "remedy_weight": 0.60,
        }
    )
    record_after = grade_run(heavier_remedy_settings, run, _ground_truth())

    assert call_count["n"] == 2  # judge called once per grade_run invocation, not skipped
    assert record_before.score != record_after.score
    assert record_after.weights.remedy_weight == 0.60


def test_write_and_read_grade_records_round_trip(tmp_path) -> None:
    run = _run_record(tmp_path / "run-5", outcome="no_answer", run_id="run-5")
    settings = Settings()
    record = grade_run(settings, run, _ground_truth())

    path = tmp_path / "grades.jsonl"
    write_grade_records(path, {"run-5": record})
    reloaded = read_grade_records(path)
    assert reloaded["run-5"].run_id == "run-5"
    assert reloaded["run-5"].score == 0.0


def test_regrade_supersedes_rather_than_duplicates(tmp_path) -> None:
    run = _run_record(tmp_path / "run-6", outcome="no_answer", run_id="run-6")
    settings = Settings()
    first = grade_run(settings, run, _ground_truth())

    path = tmp_path / "grades.jsonl"
    write_grade_records(path, {"run-6": first})
    existing = read_grade_records(path)
    assert len(existing) == 1

    second = grade_run(settings, run, _ground_truth())
    existing["run-6"] = second
    write_grade_records(path, existing)

    reloaded = read_grade_records(path)
    assert len(reloaded) == 1
    assert path.read_text(encoding="utf-8").count("run-6") == 1
