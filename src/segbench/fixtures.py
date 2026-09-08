"""Synthetic fixture generator (plan.md section 13 phase 8 acceptance check).

Produces a plausible, fully deterministic (no RNG) results set — bugs, run records and grade
records — entirely in memory, so the dashboard and the aggregation layer can be developed and
tested without spending a single token. This is a deliverable in its own right, not a throwaway:
it deliberately encodes

- three models with different competence profiles, one of which is actively *misled* by a
  channel (``customer_report``), producing the negative-gain result plan.md section 8 calls out
  as the most interesting thing the occlusion analysis can find;
- a channel (``customer_report``) missing from one of the three bugs, so ``n`` per channel is
  exercised honestly rather than always equal to the bug count;
- a handful of non-``ok`` outcomes (timeout, no_answer, cost_exceeded, harness_error) kept off the
  bugs/channels used for the gain computation, so the occlusion numbers stay hand-checkable while
  the outcome-breakdown and failure-mode aggregates still have something to show.

Every score is built from a closed-form expression (see the per-channel/per-model/per-environment
tables below), so a test can hand-compute the expected gain for any channel and assert it exactly.
"""

from __future__ import annotations

import datetime as dt

from segbench.agent.run import ENVIRONMENTS, RunCaps, RunCost, RunRecord
from segbench.corpus.models import (
    Bug,
    BugManifest,
    BugRepo,
    BugSource,
    ChannelId,
    ChannelSpec,
    FixInfo,
    GroundTruth,
    LoadedChannel,
    Origin,
    Product,
    Tracker,
)
from segbench.grade.score import GradeRecord, Weights

#: The three models under test, with distinct competence profiles.
STRONG_MODEL = "vendor/strong-model"
MID_MODEL = "vendor/mid-model"
CONFIDENT_MODEL = "vendor/confident-model"
MODELS = (STRONG_MODEL, MID_MODEL, CONFIDENT_MODEL)

_BASE_SCORE = {STRONG_MODEL: 0.70, MID_MODEL: 0.50, CONFIDENT_MODEL: 0.55}
_ENV_EFFECT = {"E0": -0.05, "E1": 0.0, "E2": 0.05}

#: Channels used in the core (full/leave-one-out) matrix, plus one used only on the "extra" bug
#: that carries the non-ok outcomes and never participates in gain (so gain stays hand-checkable).
CUSTOMER_REPORT = ChannelId.CUSTOMER_REPORT
ENGINEER_NOTES = ChannelId.ENGINEER_NOTES
ERROR_TRACE = ChannelId.ERROR_TRACE
SERVICE_LOGS = ChannelId.SERVICE_LOGS

#: Per-channel, per-model effect (added when the channel is visible, i.e. subtracted for its
#: ``loo:<channel>`` set) — constant across environment, except ``error_trace`` which matters more
#: in E0 (no repo access to substitute for it) than in E1/E2.
_CHANNEL_EFFECT = {
    CUSTOMER_REPORT: {STRONG_MODEL: 0.01, MID_MODEL: 0.01, CONFIDENT_MODEL: -0.10},
    ENGINEER_NOTES: {STRONG_MODEL: 0.05, MID_MODEL: 0.05, CONFIDENT_MODEL: 0.05},
    SERVICE_LOGS: {STRONG_MODEL: 0.02, MID_MODEL: 0.02, CONFIDENT_MODEL: 0.02},
}
_ERROR_TRACE_EFFECT_BY_ENV = {"E0": 0.08, "E1": 0.02, "E2": 0.02}

#: Per-bug baseline offset (applied identically to every channel set, so it cancels out of every
#: full-vs-loo delta and only shows up in the leaderboard/difficulty numbers).
_BUG_BASE = {"synth-bug-a": 0.05, "synth-bug-b": 0.0, "synth-bug-c": -0.05}

_CHANNEL_TEXT = {
    CUSTOMER_REPORT: "Customer report: the thing broke after the upgrade, please advise.",
    ENGINEER_NOTES: "Engineer notes: reproduced on staging; looks like a race in the retry path.",
    ERROR_TRACE: "Traceback (most recent call last):\n  ... TimeoutError: retry budget exhausted",
    SERVICE_LOGS: "2026-01-01T00:00:00 service[1]: retrying request, attempt 4/4",
}
_CHANNEL_ORIGIN = {
    CUSTOMER_REPORT: Origin.VERBATIM,
    ENGINEER_NOTES: Origin.PARAPHRASED,
    ERROR_TRACE: Origin.VERBATIM,
    SERVICE_LOGS: Origin.SYNTHESISED,
}


def _make_bug(bug_id: str, channels: list[ChannelId]) -> Bug:
    manifest = BugManifest(
        id=bug_id,
        title=f"Synthetic bug {bug_id}",
        product=Product.SUNBEAM,
        source=BugSource(
            tracker=Tracker.REPRODUCTION,
            reported_at=dt.datetime(2025, 1, 1, tzinfo=dt.UTC),
        ),
        repo=BugRepo(url="https://example.invalid/repo.git", pre_fix_ref="0" * 40),
        channels=[
            ChannelSpec(id=c, file=f"{c.value}.txt", origin=_CHANNEL_ORIGIN[c]) for c in channels
        ],
    )
    ground_truth = GroundTruth(
        root_cause=f"Synthetic root cause for {bug_id}.",
        component="synthetic-component",
        fix=FixInfo(commit="1" * 40, files=["synthetic/module.py"], symbols=["synthetic_func"]),
        reviewed_by="fixture-generator",
    )
    loaded = [
        LoadedChannel(id=c, origin=_CHANNEL_ORIGIN[c], path=f"{c.value}.txt", text=_CHANNEL_TEXT[c])
        for c in channels
    ]
    return Bug(
        directory=f"corpus/bugs/{bug_id}",
        manifest=manifest,
        ground_truth=ground_truth,
        channels=loaded,
    )


def _full_score(bug_id: str, environment: str, model: str) -> float:
    return _BASE_SCORE[model] + _ENV_EFFECT[environment] + _BUG_BASE[bug_id]


def _channel_effect(channel: ChannelId, environment: str, model: str) -> float:
    if channel is ERROR_TRACE:
        return _ERROR_TRACE_EFFECT_BY_ENV[environment]
    return _CHANNEL_EFFECT[channel][model]


def _clip(score: float) -> float:
    return max(0.0, min(1.0, score))


class _Counter:
    def __init__(self) -> None:
        self.n = 0

    def next(self) -> int:
        self.n += 1
        return self.n


def _weights() -> Weights:
    return Weights(
        diagnosis_weight=0.50,
        localisation_weight=0.30,
        remedy_weight=0.20,
        component_weight=0.50,
        file_f1_weight=0.30,
        symbol_weight=0.20,
        unsupported_specific_penalty=0.05,
        max_penalty=0.15,
    )


def _run_and_grade(
    *,
    counter: _Counter,
    bug_id: str,
    environment: str,
    channel_set: str,
    model: str,
    score: float | None,
    outcome: str,
    results_dir: str = "/tmp/segbench-synthetic",
) -> tuple[RunRecord, GradeRecord]:
    model_slug = model.split("/")[-1]
    channel_set_slug = channel_set.replace(":", "_")
    run_id = f"{bug_id}-{environment.lower()}-{channel_set_slug}-{model_slug}-{counter.next():04d}"
    started = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    duration = 60.0 + 10.0 * counter.n % 5
    cost = 0.10 + 0.01 * (counter.n % 7)
    run = RunRecord(
        run_id=run_id,
        bug_id=bug_id,
        environment=environment,
        channel_set=channel_set,
        model=model,
        backend="lxd",
        image="segbench-base",
        image_digest="sha256:synthetic",
        corpus_revision="synthetic",
        prompt_version="v1",
        prompt_hash="synthetic",
        caps=RunCaps(wall_clock_s=300, max_cost_usd=5.0),
        started_at=started.isoformat(),
        ended_at=(started + dt.timedelta(seconds=duration)).isoformat(),
        duration_s=duration,
        cost=RunCost(
            prompt_tokens=1000, completion_tokens=500, usd=cost, tripped=outcome == "cost_exceeded"
        ),
        leak_attempts=1
        if model == CONFIDENT_MODEL and environment == "E2" and counter.n % 11 == 0
        else 0,
        outcome=outcome,
        truncated=outcome == "timeout",
        answer_outcome="ok"
        if outcome == "ok"
        else ("no_answer" if outcome != "invalid_answer" else "invalid_answer"),
        answer_reason=None,
        session_id=f"session-{run_id}",
        transcript_error=None,
        reason=None,
        results_dir=results_dir,
    )

    weights = _weights()
    if outcome != "ok" or score is None:
        grade = GradeRecord(
            run_id=run_id,
            bug_id=bug_id,
            model=model,
            environment=environment,
            channel_set=channel_set,
            outcome=outcome,
            diagnosis=0.0,
            localisation=0.0,
            remedy=0.0,
            penalty=0.0,
            score=0.0,
            weights=weights,
            judge_model="deepseek/deepseek-v4-flash",
            judge_prompt_version="v1",
            graded_at=started.isoformat(),
        )
        return run, grade

    score = _clip(score)
    grade = GradeRecord(
        run_id=run_id,
        bug_id=bug_id,
        model=model,
        environment=environment,
        channel_set=channel_set,
        outcome="ok",
        component_match=score > 0.4,
        file_hit=score > 0.4,
        file_f1=score,
        symbol_hit=score > 0.6,
        root_cause_score=round(score * 3),
        root_cause_rationale="synthetic rationale",
        fix_score=round(score * 3),
        fix_rationale="synthetic rationale",
        contradicts_ground_truth=False,
        unsupported_specifics=0,
        diagnosis=score,
        localisation=score,
        remedy=score,
        penalty=0.0,
        score=score,
        weights=weights,
        judge_model="deepseek/deepseek-v4-flash",
        judge_prompt_version="v1",
        graded_at=started.isoformat(),
    )
    return run, grade


def generate_synthetic_campaign() -> tuple[list[Bug], list[RunRecord], dict[str, GradeRecord]]:
    """Build the full synthetic (bugs, runs, grades) tuple described in the module docstring."""
    bug_a = _make_bug("synth-bug-a", [CUSTOMER_REPORT, ENGINEER_NOTES, ERROR_TRACE, SERVICE_LOGS])
    bug_b = _make_bug("synth-bug-b", [CUSTOMER_REPORT, ENGINEER_NOTES, ERROR_TRACE, SERVICE_LOGS])
    bug_c = _make_bug("synth-bug-c", [ENGINEER_NOTES, ERROR_TRACE, SERVICE_LOGS])
    bugs = [bug_a, bug_b, bug_c]

    counter = _Counter()
    runs: list[RunRecord] = []
    grades: dict[str, GradeRecord] = {}

    def _add(
        bug_id: str,
        environment: str,
        channel_set: str,
        model: str,
        score: float,
        outcome: str = "ok",
    ) -> None:
        run, grade = _run_and_grade(
            counter=counter,
            bug_id=bug_id,
            environment=environment,
            channel_set=channel_set,
            model=model,
            score=score,
            outcome=outcome,
        )
        runs.append(run)
        grades[run.run_id] = grade

    for bug in bugs:
        for environment in ENVIRONMENTS:
            for model in MODELS:
                full = _full_score(bug.id, environment, model)
                _add(bug.id, environment, "full", model, full)
                for channel in bug.channel_ids:
                    loo_score = full - _channel_effect(channel, environment, model)
                    _add(bug.id, environment, f"loo:{channel.value}", model, loo_score)

    #: A fourth bug, deliberately outside the full/leave-one-out matrix above, exercising the
    #: non-``ok`` outcomes so the failure-mode aggregates have data without touching the gain
    #: computation's inputs (plan.md section 8 wants those hand-checkable).
    bug_d = _make_bug("synth-bug-d", [ENGINEER_NOTES])
    bugs.append(bug_d)
    _add(bug_d.id, "E0", "full", MID_MODEL, None, outcome="timeout")
    _add(bug_d.id, "E1", "full", STRONG_MODEL, None, outcome="no_answer")
    _add(bug_d.id, "E2", "full", CONFIDENT_MODEL, None, outcome="cost_exceeded")
    _add(bug_d.id, "E0", "full", STRONG_MODEL, None, outcome="harness_error")

    return bugs, runs, grades
