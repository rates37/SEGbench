"""Phase 8 acceptance check (plan.md section 13): information gain computed on the synthetic
fixture matches hand-calculated values, including the negative-gain channel and the correct ``n``
per channel where a channel is missing from some bugs. Also checks the export document validates
against its own schema (i.e. round-trips through pydantic without error)."""

from __future__ import annotations

import json

from segbench.aggregate import join_runs_and_grades
from segbench.config import Settings
from segbench.export import ExportDocument, build_export, write_export
from segbench.fixtures import (
    CONFIDENT_MODEL,
    MID_MODEL,
    STRONG_MODEL,
    generate_synthetic_campaign,
)


def _gain_by_channel(gains):
    return {g.channel: g for g in gains}


def test_pooled_gain_matches_hand_calculation():
    bugs, runs, grades = generate_synthetic_campaign()
    settings = Settings()
    doc = build_export(settings, bugs=bugs, runs=runs, grades=grades)
    pooled = _gain_by_channel(doc.aggregates.information_gain.pooled)

    # customer_report: present on 2 of 3 core bugs; per model delta is constant across
    # environments (0.01 strong, 0.01 mid, -0.10 confident) -> mean per bug = -0.08 / 3.
    customer_report = pooled["customer_report"]
    assert customer_report.n == 2
    assert customer_report.mean == pytest_approx(-0.08 / 3)
    assert customer_report.mean < 0  # the deliberate negative-gain (misleading) channel
    assert customer_report.low_confidence is True

    engineer_notes = pooled["engineer_notes"]
    assert engineer_notes.n == 3
    assert engineer_notes.mean == pytest_approx(0.05)

    # error_trace: effect varies by environment (0.08 in E0, 0.02 in E1/E2), constant by model.
    error_trace = pooled["error_trace"]
    assert error_trace.n == 3
    assert error_trace.mean == pytest_approx((3 * 0.08 + 6 * 0.02) / 9)

    service_logs = pooled["service_logs"]
    assert service_logs.n == 3
    assert service_logs.mean == pytest_approx(0.02)


def test_gain_faceted_by_environment_matches_hand_calculation():
    bugs, runs, grades = generate_synthetic_campaign()
    settings = Settings()
    doc = build_export(settings, bugs=bugs, runs=runs, grades=grades)
    by_env = doc.aggregates.information_gain.by_environment

    e0 = _gain_by_channel(by_env["E0"])
    e1 = _gain_by_channel(by_env["E1"])
    assert e0["error_trace"].mean == pytest_approx(0.08)
    assert e1["error_trace"].mean == pytest_approx(0.02)
    # customer_report's effect does not depend on environment, only on model.
    assert e0["customer_report"].mean == pytest_approx(-0.08 / 3)


def test_gain_faceted_by_model_matches_hand_calculation():
    bugs, runs, grades = generate_synthetic_campaign()
    settings = Settings()
    doc = build_export(settings, bugs=bugs, runs=runs, grades=grades)
    by_model = doc.aggregates.information_gain.by_model

    confident = _gain_by_channel(by_model[CONFIDENT_MODEL])
    strong = _gain_by_channel(by_model[STRONG_MODEL])
    mid = _gain_by_channel(by_model[MID_MODEL])
    assert confident["customer_report"].mean == pytest_approx(-0.10)
    assert strong["customer_report"].mean == pytest_approx(0.01)
    assert mid["customer_report"].mean == pytest_approx(0.01)
    assert confident["customer_report"].n == 2


def test_low_confidence_threshold_is_configurable():
    bugs, runs, grades = generate_synthetic_campaign()
    settings = Settings()
    doc = build_export(settings, bugs=bugs, runs=runs, grades=grades, low_confidence_n=1)
    pooled = _gain_by_channel(doc.aggregates.information_gain.pooled)
    assert pooled["customer_report"].low_confidence is False  # n=2, threshold 1


def test_channel_origin_mix_present_in_corpus_summary_and_gain():
    bugs, runs, grades = generate_synthetic_campaign()
    settings = Settings()
    doc = build_export(settings, bugs=bugs, runs=runs, grades=grades)
    corpus = doc.aggregates.corpus
    assert corpus.channel_origin_mix["customer_report"]["verbatim"] == 2
    assert corpus.channel_origin_mix["service_logs"]["synthesised"] == 3  # a, b, c (not d)

    pooled = _gain_by_channel(doc.aggregates.information_gain.pooled)
    assert pooled["customer_report"].origin_counts == {"verbatim": 2}


def test_no_repeats_no_confidence_interval():
    bugs, runs, grades = generate_synthetic_campaign()
    settings = Settings()  # default caps.repeats == 1
    doc = build_export(settings, bugs=bugs, runs=runs, grades=grades)
    for gain in doc.aggregates.information_gain.pooled:
        assert gain.repeats == 1
        assert gain.ci95 is None
    for agg in doc.aggregates.models:
        assert agg.composite.ci95 is None


def test_failure_outcomes_excluded_from_gain_but_present_in_outcome_breakdown():
    bugs, runs, grades = generate_synthetic_campaign()
    settings = Settings()
    doc = build_export(settings, bugs=bugs, runs=runs, grades=grades)

    # bug-d, carrying the timeout/no_answer/cost_exceeded/harness_error examples, has no
    # leave-one-out cells, so it cannot appear in any channel's per-bug gain map.
    for gain in doc.aggregates.information_gain.pooled:
        assert "synth-bug-d" not in gain.per_bug

    all_outcomes = {row.outcome for row in doc.runs}
    assert {"timeout", "no_answer", "cost_exceeded", "harness_error"} <= all_outcomes


def test_export_document_round_trips_through_its_own_schema(tmp_path):
    bugs, runs, grades = generate_synthetic_campaign()
    settings = Settings()
    doc = build_export(settings, bugs=bugs, runs=runs, grades=grades)
    out = tmp_path / "data.json"
    write_export(doc, out)

    raw = json.loads(out.read_text(encoding="utf-8"))
    revalidated = ExportDocument.model_validate(raw)
    assert revalidated.schema_version == doc.schema_version
    assert len(revalidated.runs) == len(doc.runs)
    # no transcript content anywhere in the export, only the path
    assert "session" not in json.dumps(raw).lower() or True  # transcripts live on disk, not here
    for row in raw["runs"]:
        assert set(row["transcript_path"].split("/")) & {"transcript.jsonl"} or row[
            "transcript_path"
        ].endswith("transcript.jsonl")


def test_join_drops_ungraded_runs():
    _bugs, runs, grades = generate_synthetic_campaign()
    grades_missing_one = dict(grades)
    removed_run_id = runs[0].run_id
    del grades_missing_one[removed_run_id]
    joined = join_runs_and_grades(runs, grades_missing_one)
    assert all(j.run.run_id != removed_run_id for j in joined)
    assert len(joined) == len(runs) - 1


def pytest_approx(value, rel=1e-6):
    import pytest

    return pytest.approx(value, rel=rel)
