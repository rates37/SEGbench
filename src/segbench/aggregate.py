"""Aggregation over graded runs (plan.md sections 8 and 11).

Joins ``results/runs.jsonl`` and ``results/grades.jsonl`` on ``run_id`` and computes every
aggregate the dashboard needs: per-model leaderboard figures, per-model-per-environment figures
(and the E1-E0 / E2-E1 deltas), per-bug-per-model scores and difficulty, information gain per
channel (pooled, and faceted by environment and by model), and a corpus summary.

Every aggregate carries its ``n``. With ``repeats == 1`` (the default, plan.md section 8) no
confidence interval is emitted — only the raw per-bug/per-run spread and a ``repeats`` field the
UI reads to decide what caveat to show. Bootstrap CIs are added only when ``repeats > 1``.
"""

from __future__ import annotations

import random
import statistics
from collections import defaultdict

from pydantic import BaseModel

from segbench.agent.run import RunRecord
from segbench.config import Settings
from segbench.corpus.models import Bug, ChannelId, Origin
from segbench.grade.score import GradeRecord

#: Below this many bugs, a gain figure is flagged ``low_confidence`` in the UI (plan.md sec. 8).
DEFAULT_LOW_CONFIDENCE_N = 10

#: Outcomes never counted in a score aggregate (CLAUDE.md / plan.md section 7.3).
EXCLUDED_OUTCOMES = frozenset({"harness_error"})

_BOOTSTRAP_ITERS = 2000


class Joined(BaseModel):
    """One run joined to its grade. Ungraded runs (grading failed, or not yet run) are dropped by
    :func:`join_runs_and_grades` rather than represented here with null scores."""

    model_config = {"arbitrary_types_allowed": True}

    run: RunRecord
    grade: GradeRecord


def join_runs_and_grades(runs: list[RunRecord], grades: dict[str, GradeRecord]) -> list[Joined]:
    """Pair each run with its grade record. A run with no grade record yet (not graded) is
    silently skipped — it is not part of any aggregate until it is graded."""
    joined = []
    for run in runs:
        grade = grades.get(run.run_id)
        if grade is None:
            continue
        joined.append(Joined(run=run, grade=grade))
    return joined


def _scored(joined: list[Joined]) -> list[Joined]:
    """Runs eligible for score aggregates: everything but ``harness_error`` (plan.md sec. 7.3)."""
    return [j for j in joined if j.grade.outcome not in EXCLUDED_OUTCOMES]


class Spread(BaseModel):
    """A point estimate with its raw spread — no CI unless ``repeats > 1`` (plan.md sec. 8)."""

    n: int
    mean: float
    values: list[float]
    repeats: int
    ci95: tuple[float, float] | None = None


def _bootstrap_ci(
    values: list[float], *, repeats: int, rng: random.Random
) -> tuple[float, float] | None:
    if repeats <= 1 or len(values) < 2:
        return None
    means = []
    n = len(values)
    for _ in range(_BOOTSTRAP_ITERS):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(statistics.fmean(sample))
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[min(len(means) - 1, int(0.975 * len(means)))]
    return (lo, hi)


def _spread(values: list[float], *, repeats: int, rng: random.Random) -> Spread:
    if not values:
        return Spread(n=0, mean=0.0, values=[], repeats=repeats)
    return Spread(
        n=len(values),
        mean=statistics.fmean(values),
        values=values,
        repeats=repeats,
        ci95=_bootstrap_ci(values, repeats=repeats, rng=rng),
    )


class OutcomeBreakdown(BaseModel):
    counts: dict[str, int]
    rates: dict[str, float]
    n_total: int


def _outcome_breakdown(joined: list[Joined]) -> OutcomeBreakdown:
    counts: dict[str, int] = defaultdict(int)
    for j in joined:
        counts[j.grade.outcome] += 1
    n_total = sum(counts.values())
    rates = {k: (v / n_total if n_total else 0.0) for k, v in counts.items()}
    return OutcomeBreakdown(counts=dict(counts), rates=rates, n_total=n_total)


class ModelAggregate(BaseModel):
    model: str
    n_runs: int
    composite: Spread
    diagnosis: Spread
    localisation: Spread
    remedy: Spread
    mean_cost_usd: float
    mean_latency_s: float
    outcomes: OutcomeBreakdown
    unsupported_specifics_rate: float
    leak_attempt_rate: float


def _model_aggregate(
    model: str, joined: list[Joined], *, repeats: int, rng: random.Random
) -> ModelAggregate:
    scored = _scored(joined)
    composite = _spread([j.grade.score for j in scored], repeats=repeats, rng=rng)
    diagnosis = _spread([j.grade.diagnosis for j in scored], repeats=repeats, rng=rng)
    localisation = _spread([j.grade.localisation for j in scored], repeats=repeats, rng=rng)
    remedy = _spread([j.grade.remedy for j in scored], repeats=repeats, rng=rng)
    costs = [j.run.cost.usd for j in joined]
    latencies = [j.run.duration_s for j in joined]
    unsupported = [
        j.grade.unsupported_specifics for j in scored if j.grade.unsupported_specifics is not None
    ]
    unsupported_rate = (
        sum(1 for u in unsupported if u > 0) / len(unsupported) if unsupported else 0.0
    )
    leak_rate = sum(1 for j in joined if j.run.leak_attempts > 0) / len(joined) if joined else 0.0
    return ModelAggregate(
        model=model,
        n_runs=len(joined),
        composite=composite,
        diagnosis=diagnosis,
        localisation=localisation,
        remedy=remedy,
        mean_cost_usd=statistics.fmean(costs) if costs else 0.0,
        mean_latency_s=statistics.fmean(latencies) if latencies else 0.0,
        outcomes=_outcome_breakdown(joined),
        unsupported_specifics_rate=unsupported_rate,
        leak_attempt_rate=leak_rate,
    )


class ModelEnvironmentAggregate(BaseModel):
    model: str
    environment: str
    composite: Spread
    n_runs: int


class ModelDeltas(BaseModel):
    model: str
    e1_minus_e0: float | None
    e2_minus_e1: float | None


def _leaderboard_subset(joined: list[Joined]) -> list[Joined]:
    """The subset used for the leaderboard and the model x environment heatmap: the ``full``
    channel set only, so a model's headline number is never diluted by leave-one-out cells that
    exist purely for the occlusion analysis (plan.md sections 6 and 11)."""
    return [j for j in joined if j.run.channel_set == "full"]


def model_aggregates(joined: list[Joined], *, repeats: int, seed: int = 0) -> list[ModelAggregate]:
    subset = _leaderboard_subset(joined)
    by_model: dict[str, list[Joined]] = defaultdict(list)
    for j in subset:
        by_model[j.run.model].append(j)
    rng = random.Random(seed)
    return [
        _model_aggregate(model, items, repeats=repeats, rng=rng)
        for model, items in sorted(by_model.items())
    ]


def model_environment_aggregates(
    joined: list[Joined], *, repeats: int, seed: int = 0
) -> tuple[list[ModelEnvironmentAggregate], list[ModelDeltas]]:
    subset = _leaderboard_subset(joined)
    rng = random.Random(seed)
    by_model_env: dict[tuple[str, str], list[Joined]] = defaultdict(list)
    for j in subset:
        by_model_env[(j.run.model, j.run.environment)].append(j)

    aggregates = []
    means: dict[tuple[str, str], float] = {}
    for (model, env), items in sorted(by_model_env.items()):
        scored = _scored(items)
        values = [j.grade.score for j in scored]
        spread = _spread(values, repeats=repeats, rng=rng)
        means[(model, env)] = spread.mean if values else None  # type: ignore[assignment]
        aggregates.append(
            ModelEnvironmentAggregate(
                model=model, environment=env, composite=spread, n_runs=len(items)
            )
        )

    deltas = []
    for model in sorted({m for m, _ in by_model_env}):
        e0 = means.get((model, "E0"))
        e1 = means.get((model, "E1"))
        e2 = means.get((model, "E2"))
        deltas.append(
            ModelDeltas(
                model=model,
                e1_minus_e0=(e1 - e0) if e0 is not None and e1 is not None else None,
                e2_minus_e1=(e2 - e1) if e1 is not None and e2 is not None else None,
            )
        )
    return aggregates, deltas


class BugModelScore(BaseModel):
    bug_id: str
    model: str
    score: Spread


class BugDifficulty(BaseModel):
    bug_id: str
    mean_score: float
    n_models: int


def bug_model_scores(
    joined: list[Joined], *, repeats: int, seed: int = 0
) -> tuple[list[BugModelScore], list[BugDifficulty]]:
    subset = _scored(_leaderboard_subset(joined))
    rng = random.Random(seed)
    by_bug_model: dict[tuple[str, str], list[float]] = defaultdict(list)
    for j in subset:
        by_bug_model[(j.run.bug_id, j.run.model)].append(j.grade.score)

    scores = [
        BugModelScore(bug_id=bug_id, model=model, score=_spread(values, repeats=repeats, rng=rng))
        for (bug_id, model), values in sorted(by_bug_model.items())
    ]

    by_bug: dict[str, list[float]] = defaultdict(list)
    for (bug_id, _model), values in by_bug_model.items():
        by_bug[bug_id].append(statistics.fmean(values))
    difficulty = [
        BugDifficulty(bug_id=bug_id, mean_score=statistics.fmean(means), n_models=len(means))
        for bug_id, means in sorted(by_bug.items())
    ]
    return scores, difficulty


def _loo_name(channel: ChannelId) -> str:
    return f"loo:{channel.value}"


def _score_index(joined: list[Joined]) -> dict[tuple[str, str, str, str], list[float]]:
    """``(bug_id, channel_set, environment, model) -> [score, ...]`` over repeats, restricted to
    scored (non-``harness_error``) runs — the raw material every gain facet is built from."""
    index: dict[tuple[str, str, str, str], list[float]] = defaultdict(list)
    for j in _scored(joined):
        key = (j.run.bug_id, j.run.channel_set, j.run.environment, j.run.model)
        index[key].append(j.grade.score)
    return index


class ChannelGain(BaseModel):
    channel: str
    n: int
    mean: float
    per_bug: dict[str, float]
    low_confidence: bool
    origin_counts: dict[str, int]
    repeats: int
    ci95: tuple[float, float] | None = None


def _gain_from_deltas(
    channel: ChannelId,
    per_bug_deltas: dict[str, list[float]],
    origin_counts_by_bug: dict[str, str],
    *,
    repeats: int,
    low_confidence_n: int,
    rng: random.Random,
) -> ChannelGain:
    per_bug_mean = {
        bug_id: statistics.fmean(deltas) for bug_id, deltas in per_bug_deltas.items() if deltas
    }
    values = list(per_bug_mean.values())
    n = len(values)
    origin_counts: dict[str, int] = defaultdict(int)
    for bug_id in per_bug_mean:
        origin = origin_counts_by_bug.get(bug_id)
        if origin:
            origin_counts[origin] += 1
    return ChannelGain(
        channel=channel.value,
        n=n,
        mean=statistics.fmean(values) if values else 0.0,
        per_bug=per_bug_mean,
        low_confidence=n < low_confidence_n,
        origin_counts=dict(origin_counts),
        repeats=repeats,
        ci95=_bootstrap_ci(values, repeats=repeats, rng=rng) if values else None,
    )


class InformationGain(BaseModel):
    pooled: list[ChannelGain]
    by_environment: dict[str, list[ChannelGain]]
    by_model: dict[str, list[ChannelGain]]


def information_gain(
    bugs: list[Bug],
    joined: list[Joined],
    *,
    repeats: int,
    low_confidence_n: int = DEFAULT_LOW_CONFIDENCE_N,
    seed: int = 0,
) -> InformationGain:
    """Information gain per channel (plan.md section 8): ``score(full) - score(loo:c)``, meaned
    per bug first (one point per bug, over whichever (environment, model) pairs have both a
    ``full`` and a ``loo:c`` score), then meaned over bugs. Pooled, and faceted by environment and
    by model, each carrying its own ``n`` — a bug missing usable data for a facet does not count
    toward that facet's ``n`` even if it counts toward the pooled figure.
    """
    rng = random.Random(seed)
    index = _score_index(joined)
    origin_by_channel_bug: dict[ChannelId, dict[str, str]] = defaultdict(dict)
    for bug in bugs:
        for channel in bug.channels:
            origin_by_channel_bug[channel.id][bug.id] = channel.origin.value

    all_environments = sorted({j.run.environment for j in joined})
    all_models = sorted({j.run.model for j in joined})
    all_channels = sorted({c for bug in bugs for c in bug.channel_ids}, key=lambda c: c.value)

    pooled: list[ChannelGain] = []
    by_environment: dict[str, list[ChannelGain]] = {env: [] for env in all_environments}
    by_model: dict[str, list[ChannelGain]] = {model: [] for model in all_models}

    for channel in all_channels:
        loo = _loo_name(channel)
        bugs_with_channel = [b for b in bugs if channel in b.channel_ids]

        pooled_deltas: dict[str, list[float]] = defaultdict(list)
        env_deltas: dict[str, dict[str, list[float]]] = {
            env: defaultdict(list) for env in all_environments
        }
        model_deltas: dict[str, dict[str, list[float]]] = {m: defaultdict(list) for m in all_models}

        for bug in bugs_with_channel:
            for env in all_environments:
                for model in all_models:
                    full_scores = index.get((bug.id, "full", env, model))
                    loo_scores = index.get((bug.id, loo, env, model))
                    if not full_scores or not loo_scores:
                        continue
                    delta = statistics.fmean(full_scores) - statistics.fmean(loo_scores)
                    pooled_deltas[bug.id].append(delta)
                    env_deltas[env][bug.id].append(delta)
                    model_deltas[model][bug.id].append(delta)

        origin_counts_by_bug = origin_by_channel_bug[channel]
        pooled.append(
            _gain_from_deltas(
                channel,
                pooled_deltas,
                origin_counts_by_bug,
                repeats=repeats,
                low_confidence_n=low_confidence_n,
                rng=rng,
            )
        )
        for env in all_environments:
            by_environment[env].append(
                _gain_from_deltas(
                    channel,
                    env_deltas[env],
                    origin_counts_by_bug,
                    repeats=repeats,
                    low_confidence_n=low_confidence_n,
                    rng=rng,
                )
            )
        for model in all_models:
            by_model[model].append(
                _gain_from_deltas(
                    channel,
                    model_deltas[model],
                    origin_counts_by_bug,
                    repeats=repeats,
                    low_confidence_n=low_confidence_n,
                    rng=rng,
                )
            )

    return InformationGain(pooled=pooled, by_environment=by_environment, by_model=by_model)


class CorpusSummary(BaseModel):
    n_bugs: int
    n_ready: int
    products: dict[str, int]
    channel_origin_mix: dict[str, dict[str, int]]


def corpus_summary(bugs: list[Bug]) -> CorpusSummary:
    """Bug counts, product mix, and the per-channel origin mix (verbatim/paraphrased/synthesised)
    the dashboard must display alongside gain figures (plan.md section 8)."""
    products: dict[str, int] = defaultdict(int)
    origin_mix: dict[str, dict[str, int]] = defaultdict(
        lambda: dict.fromkeys([o.value for o in Origin], 0)
    )
    for bug in bugs:
        products[bug.manifest.product.value] += 1
        for channel in bug.channels:
            origin_mix[channel.id.value][channel.origin.value] += 1
    return CorpusSummary(
        n_bugs=len(bugs),
        n_ready=sum(1 for b in bugs if b.ready),
        products=dict(products),
        channel_origin_mix={k: dict(v) for k, v in origin_mix.items()},
    )


class Aggregates(BaseModel):
    models: list[ModelAggregate]
    model_environment: list[ModelEnvironmentAggregate]
    model_deltas: list[ModelDeltas]
    bug_model_scores: list[BugModelScore]
    bug_difficulty: list[BugDifficulty]
    information_gain: InformationGain
    corpus: CorpusSummary


def compute_aggregates(
    settings: Settings,
    bugs: list[Bug],
    joined: list[Joined],
    *,
    low_confidence_n: int = DEFAULT_LOW_CONFIDENCE_N,
    seed: int = 0,
) -> Aggregates:
    repeats = settings.caps.repeats
    model_agg = model_aggregates(joined, repeats=repeats, seed=seed)
    model_env_agg, deltas = model_environment_aggregates(joined, repeats=repeats, seed=seed)
    bug_model, difficulty = bug_model_scores(joined, repeats=repeats, seed=seed)
    gain = information_gain(
        bugs, joined, repeats=repeats, low_confidence_n=low_confidence_n, seed=seed
    )
    return Aggregates(
        models=model_agg,
        model_environment=model_env_agg,
        model_deltas=deltas,
        bug_model_scores=bug_model,
        bug_difficulty=difficulty,
        information_gain=gain,
        corpus=corpus_summary(bugs),
    )
