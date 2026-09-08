"""Run orchestration: the campaign matrix, subsetting, resumption, concurrency and the cost
ceiling (plan.md sections 6 and 13 phase 7).

The matrix is ``bugs x environments x channel_sets(bug) x models``, expanded to *runs* by
``repeats``; see :mod:`segbench.occlusion` for how ``channel_sets(bug)`` is generated. This module
turns that abstract matrix into concrete work, in order:

1. **filter** the axes per the CLI's ``--bugs``/``--products``/``--tags``/``--envs``/
   ``--channel-sets``/``--models``/``--limit``;
2. **subtract** what ``results/runs.jsonl`` already has — a cell needs no more work once it has
   ``repeats`` runs whose outcome is not ``harness_error`` (a harness failure never "counts" as
   having tried the cell; only agent-attributable outcomes do, per plan.md section 7.3's
   ``outcome`` vocabulary) — unless ``--force`` is passed, which schedules every repeat again;
3. **execute** the remaining work with two independent concurrency bounds (overall in-flight runs,
   and container provisioning) and a campaign-wide cost ceiling that halts cleanly: no new cell is
   *started* once the ceiling is at or past the ceiling, and in-flight runs are allowed to finish.

Orphan safety on ``SIGINT`` is not reimplemented here: every container created by
:func:`segbench.agent.run.execute_run` is already registered with
:data:`segbench.runtime.base.REGISTRY`, which destroys every live container on ``SIGINT``/
``SIGTERM``/interpreter exit before re-raising. This module's job is only to stop *submitting* new
work when that happens, drain whatever is in flight (their own ``execute_run`` calls simply finish
with ``outcome: "harness_error"`` once their container is gone from under them, which is correct:
that repeat still needs retrying, and it will not be double-counted because ``harness_error`` never
counts towards ``repeats`` above), and leave the campaign manifest and ``runs.jsonl`` in a state
from which resumption reproduces exactly the missing work.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

from segbench.agent.run import execute_run
from segbench.config import ModelConfig, Settings
from segbench.corpus.loader import load_corpus
from segbench.corpus.models import Bug
from segbench.grade.score import read_run_records
from segbench.logging import get_logger
from segbench.netpol.gitmirror import GitMirror
from segbench.occlusion import channel_sets_for
from segbench.runtime.images import ImageBuilder

log = get_logger(__name__)

HARNESS_ERROR = "harness_error"


class OrchestratorError(Exception):
    """The matrix or its filters could not be built."""


@dataclass(frozen=True)
class CellKey:
    """Identifies one (bug, environment, channel set, model) cell — the unit resumption tracks."""

    bug_id: str
    environment: str
    channel_set: str
    model: str

    def as_tuple(self) -> tuple[str, str, str, str]:
        return (self.bug_id, self.environment, self.channel_set, self.model)


@dataclass
class CellPlan:
    """One cell's place in the matrix: how many repeats it needs, and how many it already has."""

    key: CellKey
    product: str
    repeats_total: int
    existing: int  # non-harness_error runs already on record for this cell
    pending: int  # repeats still to execute this campaign


@dataclass
class Filters:
    bugs: tuple[str, ...] | None = None
    products: tuple[str, ...] | None = None
    tags: tuple[str, ...] | None = None
    environments: tuple[str, ...] | None = None
    channel_sets: tuple[str, ...] | None = None
    models: tuple[str, ...] | None = None
    limit: int | None = None

    @staticmethod
    def _split(value: str | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        return tuple(part.strip() for part in value.split(",") if part.strip())

    @classmethod
    def from_cli(
        cls,
        *,
        bugs: str | None = None,
        products: str | None = None,
        tags: str | None = None,
        environments: str | None = None,
        channel_sets: str | None = None,
        models: str | None = None,
        limit: int | None = None,
    ) -> Filters:
        return cls(
            bugs=cls._split(bugs),
            products=cls._split(products),
            tags=cls._split(tags),
            environments=cls._split(environments),
            channel_sets=cls._split(channel_sets),
            models=cls._split(models),
            limit=limit,
        )


DEFAULT_ENVIRONMENTS = ("E0", "E1", "E2")


@dataclass
class CampaignMatrix:
    cells: list[CellPlan] = field(default_factory=list)

    @property
    def cell_count(self) -> int:
        return len(self.cells)

    @property
    def total_runs(self) -> int:
        """Repeats across every cell in the (filtered) matrix, ignoring what already exists."""
        return sum(c.repeats_total for c in self.cells)

    @property
    def pending_runs(self) -> int:
        return sum(c.pending for c in self.cells)

    @property
    def existing_runs(self) -> int:
        return sum(c.existing for c in self.cells)

    def breakdown(self, axis: str) -> dict[str, int]:
        """Pending-run counts grouped by one axis: ``bug``, ``environment``, ``channel_set`` or
        ``model``."""
        index = {"bug": 0, "environment": 1, "channel_set": 2, "model": 3}[axis]
        counts: dict[str, int] = {}
        for cell in self.cells:
            value = cell.key.as_tuple()[index]
            counts[value] = counts.get(value, 0) + cell.pending
        return counts


def _model_matrix(settings: Settings, wanted: tuple[str, ...] | None) -> list[ModelConfig]:
    if not settings.models:
        raise OrchestratorError("no models configured; add at least one [[models]] entry")
    if wanted is None:
        return list(settings.models)
    by_id = {m.id: m for m in settings.models}
    unknown = [m for m in wanted if m not in by_id]
    if unknown:
        raise OrchestratorError(
            f"--models names id(s) not in the model matrix: {', '.join(unknown)}"
        )
    return [by_id[m] for m in wanted]


def _load_bugs(settings: Settings, filters: Filters) -> list[Bug]:
    bugs, errors = load_corpus(
        settings.paths.corpus,
        bug_ids=list(filters.bugs) if filters.bugs else None,
        include_unreviewed=False,
    )
    if errors:
        raise OrchestratorError("; ".join(str(e) for e in errors))
    if filters.products:
        wanted = set(filters.products)
        bugs = [b for b in bugs if b.manifest.product.value in wanted]
    if filters.tags:
        wanted_tags = set(filters.tags)
        bugs = [b for b in bugs if wanted_tags & set(b.manifest.tags)]
    return bugs


def _existing_counts(settings: Settings) -> dict[tuple[str, str, str, str], int]:
    """Non-``harness_error`` run counts, by cell, from ``results/runs.jsonl``."""
    runs_path = Path(settings.paths.results) / "runs.jsonl"
    counts: dict[tuple[str, str, str, str], int] = {}
    for run in read_run_records(runs_path):
        if run.outcome == HARNESS_ERROR:
            continue
        key = (run.bug_id, run.environment, run.channel_set, run.model)
        counts[key] = counts.get(key, 0) + 1
    return counts


def build_matrix(
    settings: Settings,
    filters: Filters,
    *,
    force: bool = False,
    repeats: int | None = None,
) -> CampaignMatrix:
    """Build the (filtered, resumption-aware) matrix. Raises :class:`OrchestratorError` on a bad
    filter (unknown bug id, unknown model id, ...)."""
    bugs = _load_bugs(settings, filters)
    models = _model_matrix(settings, filters.models)
    environments = filters.environments or DEFAULT_ENVIRONMENTS
    unknown_envs = [e for e in environments if e not in DEFAULT_ENVIRONMENTS]
    if unknown_envs:
        raise OrchestratorError(f"--envs names unknown environment(s): {', '.join(unknown_envs)}")

    repeats_total = repeats if repeats is not None else settings.caps.repeats
    existing = {} if force else _existing_counts(settings)

    cells: list[CellPlan] = []
    for bug in bugs:
        channel_sets = channel_sets_for(bug)
        if filters.channel_sets:
            wanted_sets = set(filters.channel_sets)
            channel_sets = [cs for cs in channel_sets if cs.name in wanted_sets]
        for environment in environments:
            for spec in channel_sets:
                for model in models:
                    key = CellKey(bug.id, environment, spec.name, model.id)
                    have = existing.get(key.as_tuple(), 0)
                    pending = repeats_total if force else max(0, repeats_total - have)
                    cells.append(
                        CellPlan(
                            key=key,
                            product=bug.manifest.product.value,
                            repeats_total=repeats_total,
                            existing=0 if force else have,
                            pending=pending,
                        )
                    )

    if filters.limit is not None:
        cells = _apply_limit(cells, filters.limit)

    return CampaignMatrix(cells=cells)


def _apply_limit(cells: list[CellPlan], limit: int) -> list[CellPlan]:
    """Cap total *pending* runs at ``limit``, trimming (and dropping) cells in matrix order."""
    if limit < 0:
        raise OrchestratorError("--limit must be >= 0")
    kept: list[CellPlan] = []
    remaining = limit
    for cell in cells:
        if remaining <= 0:
            kept.append(
                CellPlan(cell.key, cell.product, cell.repeats_total, cell.existing, pending=0)
            )
            continue
        take = min(cell.pending, remaining)
        remaining -= take
        kept.append(CellPlan(cell.key, cell.product, cell.repeats_total, cell.existing, take))
    return kept


@dataclass
class CostEstimate:
    low_usd: float
    high_usd: float


def estimate_cost(
    settings: Settings,
    matrix: CampaignMatrix,
    *,
    tokens_per_run: tuple[int, int] = (4_000, 60_000),
) -> CostEstimate:
    """A cost range from the configured price table and a configurable per-run token estimate.

    ``tokens_per_run`` is ``(low_total_tokens, high_total_tokens)`` for one run, split 1:4
    prompt:completion (agentic transcripts are completion-heavy) when no better information is
    available. Models absent from ``netpol.pricing`` fall back to the mean of whatever prices are
    configured, or a conservative $3/$15-per-million guess if the table is empty.
    """
    pricing = settings.netpol.pricing
    if pricing:
        mean_in = sum(p.input_per_million_usd for p in pricing.values()) / len(pricing)
        mean_out = sum(p.output_per_million_usd for p in pricing.values()) / len(pricing)
    else:
        mean_in, mean_out = 3.0, 15.0

    low_usd = high_usd = 0.0
    per_model_runs: dict[str, int] = {}
    for cell in matrix.cells:
        per_model_runs[cell.key.model] = per_model_runs.get(cell.key.model, 0) + cell.pending

    for model_id, n_runs in per_model_runs.items():
        price = pricing.get(model_id)
        in_price = price.input_per_million_usd if price else mean_in
        out_price = price.output_per_million_usd if price else mean_out
        for total_tokens in tokens_per_run:
            prompt_tokens = total_tokens // 5
            completion_tokens = total_tokens - prompt_tokens
            cost = (prompt_tokens / 1_000_000) * in_price + (
                completion_tokens / 1_000_000
            ) * out_price
            if total_tokens == tokens_per_run[0]:
                low_usd += cost * n_runs
            else:
                high_usd += cost * n_runs
    return CostEstimate(low_usd=low_usd, high_usd=high_usd)


@dataclass
class CampaignState:
    """Live counters for a running (or resumed) campaign, used for the progress table and the
    manifest's final summary."""

    completed: int = 0
    failed: int = 0  # harness_error
    running: int = 0
    cost_usd: float = 0.0
    stopping: bool = False


def _campaigns_dir(settings: Settings) -> Path:
    path = Path(settings.paths.results) / "campaigns"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _image_digests(settings: Settings) -> dict[str, str]:
    """Best-effort image digests for the manifest. Empty (never fatal) if the runtime backend is
    not reachable right now, e.g. during a ``--dry-run`` with no LXD available."""
    if settings.runtime.backend != "lxd":
        return {}
    try:
        from segbench.runtime.lxd import LXDRuntime

        runtime = LXDRuntime(project=settings.runtime.lxd_project)
        runtime.preflight()
        builder = ImageBuilder(
            runtime=runtime,
            cache_dir=settings.paths.image_cache,
            definitions_dir=settings.runtime.image_definitions,
        )
        return {s.name: s.fingerprint for s in builder.status() if s.fingerprint}
    except Exception as exc:  # a manifest must still be written without a live runtime
        log.debug(
            "could not read image digests for the campaign manifest", extra={"error": str(exc)}
        )
        return {}


def write_manifest(
    settings: Settings,
    campaign_id: str,
    matrix: CampaignMatrix,
    filters: Filters,
    *,
    dry_run: bool,
) -> Path:
    """Write ``results/campaigns/<id>.json``: everything needed to reconstruct this campaign."""
    from segbench.agent.run import _corpus_revision  # local import: avoid a cycle at module load

    manifest = {
        "campaign_id": campaign_id,
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "dry_run": dry_run,
        "config": settings.model_dump(mode="json"),
        "filters": {
            "bugs": filters.bugs,
            "products": filters.products,
            "tags": filters.tags,
            "environments": filters.environments,
            "channel_sets": filters.channel_sets,
            "models": filters.models,
            "limit": filters.limit,
        },
        "corpus_revision": _corpus_revision(settings.paths.corpus),
        "image_digests": _image_digests(settings),
        "matrix": [
            {
                "bug_id": c.key.bug_id,
                "product": c.product,
                "environment": c.key.environment,
                "channel_set": c.key.channel_set,
                "model": c.key.model,
                "repeats_total": c.repeats_total,
                "existing": c.existing,
                "pending": c.pending,
            }
            for c in matrix.cells
        ],
    }
    path = _campaigns_dir(settings) / f"{campaign_id}.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=False), encoding="utf-8")
    return path


def read_manifest(settings: Settings, campaign_id: str) -> dict:
    path = _campaigns_dir(settings) / f"{campaign_id}.json"
    if not path.is_file():
        raise OrchestratorError(f"no campaign manifest at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def latest_campaign_id(settings: Settings) -> str | None:
    manifests = sorted(_campaigns_dir(settings).glob("*.json"))
    return manifests[-1].stem if manifests else None


def new_campaign_id() -> str:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def run_campaign(
    settings: Settings,
    filters: Filters,
    *,
    force: bool = False,
    concurrency: int | None = None,
    provision_concurrency: int | None = None,
    campaign_id: str | None = None,
    on_update: Callable[[CampaignState], None] | None = None,
) -> tuple[str, CampaignState]:
    """Execute a campaign to completion, interruption, or the cost ceiling — whichever comes
    first. Returns the campaign id and the final :class:`CampaignState`.

    In order:

    1. bound overall in-flight runs at ``concurrency`` (default ``settings.concurrency``);
    2. bound concurrent container *provisioning* independently at ``provision_concurrency``
       (default: same as ``concurrency``) via a semaphore threaded into
       :func:`segbench.agent.run.execute_run`, since a slow container backend is a different
       bottleneck than slow inference;
    3. stop submitting new cells once ``caps.max_campaign_cost_usd`` is met or exceeded by the
       running total of completed runs' metered cost, or on ``SIGINT`` (surfaced here as
       ``KeyboardInterrupt`` once :data:`segbench.runtime.base.REGISTRY` has already destroyed
       every live container);
    4. let whatever is already in flight finish (its container is gone, so it finishes fast with
       ``outcome: "harness_error"``, which correctly leaves its repeat pending for next time).
    """
    matrix = build_matrix(settings, filters, force=force)
    campaign_id = campaign_id or new_campaign_id()
    write_manifest(settings, campaign_id, matrix, filters, dry_run=False)

    concurrency = concurrency or settings.concurrency
    provision_concurrency = provision_concurrency or concurrency
    provision_semaphore = threading.Semaphore(provision_concurrency)
    ceiling = settings.caps.max_campaign_cost_usd

    bugs_by_id = {b.id: b for b in _load_bugs(settings, filters)}
    models_by_id = {m.id: m for m in _model_matrix(settings, filters.models)}

    state = CampaignState()
    lock = threading.Lock()

    # One mirror listener for the whole campaign (plan.md sec 5.3), not one per E2 run: several E2
    # runs can be in flight at once under --concurrency, and each has its own GitMirror trying to
    # bind the same configured port otherwise.
    needs_mirror = any(cell.pending > 0 and cell.key.environment == "E2" for cell in matrix.cells)
    mirror = GitMirror(settings) if needs_mirror else None
    if mirror is not None:
        mirror.start()

    def work_items() -> Iterator[CellKey]:
        for cell in matrix.cells:
            for _ in range(cell.pending):
                yield cell.key

    def run_one(key: CellKey) -> None:
        record = execute_run(
            settings,
            bug=bugs_by_id[key.bug_id],
            environment=key.environment,
            channel_set=key.channel_set,
            model=models_by_id[key.model],
            provision_semaphore=provision_semaphore,
            mirror=mirror,
        )
        with lock:
            state.running -= 1
            if record.outcome == HARNESS_ERROR:
                state.failed += 1
            else:
                state.completed += 1
            state.cost_usd += record.cost.usd
            if on_update:
                on_update(state)

    items = work_items()
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            in_flight: set[Future] = set()
            try:
                while True:
                    if state.stopping:
                        break
                    if ceiling is not None and state.cost_usd >= ceiling:
                        log.warning(
                            "campaign cost ceiling reached; not starting further cells",
                            extra={"cost_usd": state.cost_usd, "ceiling": ceiling},
                        )
                        state.stopping = True
                        break
                    while len(in_flight) < concurrency:
                        key = next(items, None)
                        if key is None:
                            break
                        with lock:
                            state.running += 1
                            if on_update:
                                on_update(state)
                        in_flight.add(pool.submit(run_one, key))
                    if not in_flight:
                        break
                    done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
                    for future in done:
                        future.result()  # re-raise a bug in run_one; run_one swallows the rest
            except KeyboardInterrupt:
                log.warning("SIGINT: draining in-flight runs, submitting nothing further")
                state.stopping = True
                wait(in_flight)
    finally:
        if mirror is not None:
            mirror.stop()

    return campaign_id, state
