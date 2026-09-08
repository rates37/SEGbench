"""Matrix building, filtering, resumption and the cost ceiling (plan.md sections 6, 13 phase 7).

Pure-logic tests only: no container, no inference. ``execute_run`` itself is exercised end to end
only via ``segbench run once``/``run campaign`` against a real backend, same limitation phase 5
and phase 6 have.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from segbench.agent.run import RunCaps, RunCost, RunRecord
from segbench.config import ModelConfig, Settings
from segbench.orchestrator import (
    Filters,
    OrchestratorError,
    build_matrix,
    estimate_cost,
)

MODELS = [
    ModelConfig(id="deepseek/deepseek-chat", label="DeepSeek"),
    ModelConfig(id="qwen/qwen-2.5-coder-32b-instruct", label="Qwen"),
]

# Both fixture bugs have 7 channels: 8 channel sets each (full + 7 loo:*).
CHANNEL_SETS_PER_BUG = 8
ENVIRONMENTS = 3
BUGS = 2


def _settings(good_corpus: Path, tmp_path: Path, **overrides) -> Settings:
    return Settings(
        paths={"corpus": good_corpus, "results": tmp_path / "results"},
        models=MODELS,
        **overrides,
    )


def _record(
    bug_id: str, environment: str, channel_set: str, model: str, *, outcome: str
) -> RunRecord:
    now = dt.datetime.now(dt.UTC).isoformat()
    return RunRecord(
        run_id=f"{bug_id}-{environment.lower()}-{model}-x",
        bug_id=bug_id,
        environment=environment,
        channel_set=channel_set,
        model=model,
        backend="lxd",
        image="segbench-base",
        image_digest="deadbeef",
        corpus_revision=None,
        prompt_version="v1",
        prompt_hash="abc123",
        caps=RunCaps(wall_clock_s=300, max_cost_usd=5.0),
        started_at=now,
        ended_at=now,
        duration_s=1.0,
        cost=RunCost(prompt_tokens=10, completion_tokens=10, usd=0.01),
        leak_attempts=0,
        outcome=outcome,
        truncated=False,
        answer_outcome="ok" if outcome == "ok" else "no_answer",
        results_dir="results/runs/x",
    )


def _append(settings: Settings, record: RunRecord) -> None:
    path = Path(settings.paths.results) / "runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(record.model_dump_json() + "\n")


def test_full_matrix_matches_hand_computed_count(good_corpus, tmp_path) -> None:
    settings = _settings(good_corpus, tmp_path)

    matrix = build_matrix(settings, Filters())

    expected = BUGS * ENVIRONMENTS * CHANNEL_SETS_PER_BUG * len(MODELS)
    assert matrix.cell_count == expected
    assert matrix.total_runs == expected  # repeats=1 by default
    assert matrix.pending_runs == expected
    assert matrix.existing_runs == 0


def test_filters_narrow_the_matrix(good_corpus, tmp_path) -> None:
    settings = _settings(good_corpus, tmp_path)

    matrix = build_matrix(
        settings,
        Filters(bugs=("lp-9000001",), environments=("E1",), models=("deepseek/deepseek-chat",)),
    )

    assert matrix.cell_count == CHANNEL_SETS_PER_BUG
    for cell in matrix.cells:
        assert cell.key.bug_id == "lp-9000001"
        assert cell.key.environment == "E1"
        assert cell.key.model == "deepseek/deepseek-chat"


def test_channel_sets_filter_matches_by_name(good_corpus, tmp_path) -> None:
    settings = _settings(good_corpus, tmp_path)

    matrix = build_matrix(settings, Filters(channel_sets=("full",)))

    assert matrix.cell_count == BUGS * ENVIRONMENTS * 1 * len(MODELS)
    assert all(c.key.channel_set == "full" for c in matrix.cells)


def test_unknown_model_filter_raises(good_corpus, tmp_path) -> None:
    settings = _settings(good_corpus, tmp_path)

    with pytest.raises(OrchestratorError):
        build_matrix(settings, Filters(models=("not-a-model",)))


def test_limit_caps_total_pending_runs(good_corpus, tmp_path) -> None:
    settings = _settings(good_corpus, tmp_path)

    matrix = build_matrix(settings, Filters(limit=5))

    assert matrix.pending_runs == 5


def test_resumption_skips_cells_with_a_non_harness_error_run(good_corpus, tmp_path) -> None:
    settings = _settings(good_corpus, tmp_path)
    _append(
        settings,
        _record("lp-9000001", "E0", "full", "deepseek/deepseek-chat", outcome="ok"),
    )

    matrix = build_matrix(settings, Filters())

    expected = BUGS * ENVIRONMENTS * CHANNEL_SETS_PER_BUG * len(MODELS)
    assert matrix.existing_runs == 1
    assert matrix.pending_runs == expected - 1
    resolved = [
        cell
        for cell in matrix.cells
        if cell.key.bug_id == "lp-9000001"
        and cell.key.environment == "E0"
        and cell.key.channel_set == "full"
        and cell.key.model == "deepseek/deepseek-chat"
    ]
    (cell,) = resolved
    assert cell.existing == 1
    assert cell.pending == 0


def test_harness_error_does_not_count_towards_resumption(good_corpus, tmp_path) -> None:
    settings = _settings(good_corpus, tmp_path)
    _append(
        settings,
        _record("lp-9000001", "E0", "full", "deepseek/deepseek-chat", outcome="harness_error"),
    )

    matrix = build_matrix(settings, Filters())

    expected = BUGS * ENVIRONMENTS * CHANNEL_SETS_PER_BUG * len(MODELS)
    assert matrix.existing_runs == 0
    assert matrix.pending_runs == expected


def test_force_ignores_existing_records(good_corpus, tmp_path) -> None:
    settings = _settings(good_corpus, tmp_path)
    _append(
        settings,
        _record("lp-9000001", "E0", "full", "deepseek/deepseek-chat", outcome="ok"),
    )

    matrix = build_matrix(settings, Filters(), force=True)

    expected = BUGS * ENVIRONMENTS * CHANNEL_SETS_PER_BUG * len(MODELS)
    assert matrix.pending_runs == expected


def test_repeats_multiply_pending_runs(good_corpus, tmp_path) -> None:
    settings = _settings(good_corpus, tmp_path, caps={"repeats": 3})

    matrix = build_matrix(settings, Filters())

    expected = BUGS * ENVIRONMENTS * CHANNEL_SETS_PER_BUG * len(MODELS) * 3
    assert matrix.pending_runs == expected


def test_estimate_cost_uses_the_price_table(good_corpus, tmp_path) -> None:
    settings = _settings(
        good_corpus,
        tmp_path,
        netpol={
            "pricing": {
                "deepseek/deepseek-chat": {
                    "input_per_million_usd": 1.0,
                    "output_per_million_usd": 2.0,
                },
                "qwen/qwen-2.5-coder-32b-instruct": {
                    "input_per_million_usd": 1.0,
                    "output_per_million_usd": 2.0,
                },
            }
        },
    )
    matrix = build_matrix(settings, Filters(limit=1))

    estimate = estimate_cost(settings, matrix)

    assert estimate.low_usd > 0
    assert estimate.high_usd >= estimate.low_usd
