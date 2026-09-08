"""Executes a single run: one (bug, environment, channel set, model) cell (plan.md sections 2,
5, 10 and 13 phase 5).

This is the bottom of the stack the orchestrator (phase 7) will eventually drive many of in
parallel; today it is also directly reachable as ``segbench run once`` for single-run debugging.
Responsibility, in order: provision the container and its network policy, seed the environment
(E0/E1/E2), render and deliver the prompt, invoke the agent while snapshotting its answer file,
collect the answer/transcript/netlog/meta, tear the container down — guaranteed, even on
exception — and append one run record to ``results/runs.jsonl``.

Everything the reproducibility invariant (CLAUDE.md) asks for is on :class:`RunRecord`: image
digest, corpus revision, channel set, environment, model, prompt hash and version, caps, timings,
cost, and outcome.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from segbench.agent.answers import AnswerSnapshotter
from segbench.agent.opencode import AgentRunResult, export_session, invoke_agent
from segbench.agent.prompt import PROMPT_VERSION, render_prompt
from segbench.agent.schema import ANSWER_FILENAME, AnswerOutcome, AnswerResult, validate_answer_file
from segbench.agent.transcript import TranscriptError, extract_transcript, write_transcript
from segbench.config import ModelConfig, Settings
from segbench.corpus.models import Bug, ChannelId
from segbench.logging import get_logger
from segbench.netpol import enforce
from segbench.netpol.gitmirror import GitMirror, MirrorError, ensure_target_mirror, seed_e1
from segbench.netpol.policy import EgressPolicy, build_policies
from segbench.netpol.proxy import EgressProxy
from segbench.runtime.base import ContainerSpec, Handle, Runtime, RuntimeFailure
from segbench.runtime.base import NetworkPolicy as RuntimeNetworkPolicy
from segbench.runtime.images import ImageDefinition, load_definitions
from segbench.runtime.lxd import LXDRuntime, sanitise_name
from segbench.runtime.podman import PodmanRuntime

log = get_logger(__name__)

ENVIRONMENTS = ("E0", "E1", "E2")

#: How often the agent's answer file is polled while it works (plan.md section 10).
ANSWER_POLL_INTERVAL_S = 5.0


class RunError(Exception):
    """A run could not be set up or executed at all — a harness failure, not an agent failure."""


@dataclass(frozen=True)
class ChannelSet:
    """A resolved channel set: its label (``full`` or ``loo:<channel>``) and visible channels."""

    label: str
    visible: tuple[ChannelId, ...]


def resolve_channel_set(bug: Bug, channel_set: str) -> ChannelSet:
    """Parse ``"full"`` or ``"loo:<channel_id>"`` against one bug's actual channels (plan.md
    section 6: leave-one-out only generates a cell for channels the bug actually has)."""
    if channel_set == "full":
        return ChannelSet(label="full", visible=tuple(bug.channel_ids))

    prefix = "loo:"
    if not channel_set.startswith(prefix):
        raise RunError(f"unknown channel set {channel_set!r}; expected 'full' or 'loo:<channel>'")
    excluded_raw = channel_set[len(prefix) :]
    try:
        excluded = ChannelId(excluded_raw)
    except ValueError as exc:
        raise RunError(
            f"unknown channel id {excluded_raw!r} in channel set {channel_set!r}"
        ) from exc
    if excluded not in bug.channel_ids:
        raise RunError(
            f"bug {bug.id!r} has no channel {excluded.value!r} to leave out "
            f"(it has: {', '.join(c.value for c in bug.channel_ids)})"
        )
    visible = tuple(c for c in bug.channel_ids if c != excluded)
    return ChannelSet(label=channel_set, visible=visible)


class RunCaps(BaseModel):
    wall_clock_s: int
    max_cost_usd: float


class RunCost(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usd: float = 0.0
    estimated: bool = False
    tripped: bool = False


class RunRecord(BaseModel):
    """One line of ``results/runs.jsonl``. See the module docstring for the invariant it serves."""

    run_id: str
    bug_id: str
    environment: str
    channel_set: str
    model: str
    backend: str
    image: str
    image_digest: str
    corpus_revision: str | None
    prompt_version: str
    prompt_hash: str
    caps: RunCaps
    started_at: str
    ended_at: str
    duration_s: float
    cost: RunCost
    leak_attempts: int
    outcome: str
    truncated: bool
    answer_outcome: str
    answer_reason: str | None = None
    session_id: str | None = None
    transcript_error: str | None = None
    reason: str | None = None
    results_dir: str


def _corpus_revision(corpus_root: Path) -> str | None:
    """The git revision of the corpus directory, or ``None`` if it is not (or not fully) a git
    checkout. Best-effort: a run must still be executable against a corpus that is not version
    controlled, it just cannot claim the same reproducibility guarantee."""
    proc = subprocess.run(
        ["git", "-C", str(corpus_root), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _select_image(settings: Settings, bug: Bug) -> ImageDefinition:
    """The overlay whose ``products`` includes this bug's product, or the base image."""
    definitions = load_definitions(settings.runtime.image_definitions)
    for definition in definitions.values():
        if bug.manifest.product.value in definition.products:
            return definition
    return definitions["base"]


def _build_runtime(settings: Settings) -> Runtime:
    runtime: Runtime
    if settings.runtime.backend == "lxd":
        runtime = LXDRuntime(project=settings.runtime.lxd_project)
    else:
        runtime = PodmanRuntime()
    runtime.preflight()
    return runtime


def _lxd_gateway_ip(lxd: LXDRuntime, network: str) -> str:
    """The dedicated network's own gateway address — where the per-run proxy listener binds
    (``bind_host="0.0.0.0"``) and therefore what the container must be told to reach."""
    proc = lxd.run_lxc("network", "get", network, "ipv4.address", check=False)
    if proc.returncode != 0:
        raise RunError(f"could not read the gateway address of LXD network {network!r}")
    cidr = proc.stdout.strip()
    if not cidr:
        raise RunError(f"LXD network {network!r} has no ipv4.address set")
    return cidr.split("/", 1)[0]


def _push_channels(
    runtime: Runtime, handle: Handle, bug: Bug, channels: ChannelSet, *, remote_dir: str
) -> None:
    """Materialise the visible channels as files, in addition to their appearing in the prompt
    text — plan.md section 5, E0: "``/workspace`` contains only the channel files"."""
    with tempfile.TemporaryDirectory(prefix="segbench-channels-") as tmp:
        local_dir = Path(tmp)
        for channel in bug.channels:
            if channel.id not in channels.visible:
                continue
            suffix = channel.path.suffix or ".txt"
            (local_dir / f"{channel.id.value}{suffix}").write_text(channel.text, encoding="utf-8")
        runtime.exec(handle, ["mkdir", "-p", remote_dir], check=True)
        runtime.push(handle, local_dir, remote_dir)


def _seed_environment(
    runtime: Runtime,
    handle: Handle,
    bug: Bug,
    environment: str,
    settings: Settings,
    *,
    run_id: str,
    bridge_ip: str,
    mirror: GitMirror | None,
) -> None:
    if environment == "E0":
        return
    if environment == "E1":
        bare_repo = ensure_target_mirror(settings.paths.mirror_cache, bug)
        with tempfile.TemporaryDirectory(prefix="segbench-e1-seed-") as tmp:
            try:
                seed_e1(runtime, handle, bare_repo, host_workdir=Path(tmp) / "repo")
            except MirrorError as exc:
                raise RunError(f"E1 seeding failed: {exc}") from exc
        return
    if environment == "E2":
        assert mirror is not None
        netlog_path = Path(settings.paths.results) / "runs" / run_id / "netlog.jsonl"
        mirror.start_run(run_id, bug, netlog_path=netlog_path)
        mirror.apply_e2_rewrite(runtime, handle, run_id, bug, advertise_host=bridge_ip)
        return
    raise RunError(f"unknown environment {environment!r}; expected one of {ENVIRONMENTS}")


def execute_run(
    settings: Settings,
    *,
    bug: Bug,
    environment: str,
    channel_set: str,
    model: ModelConfig,
) -> RunRecord:
    """Execute one run end to end and return its record. Also appends the record to
    ``results/runs.jsonl``."""
    if environment not in ENVIRONMENTS:
        raise RunError(f"unknown environment {environment!r}; expected one of {ENVIRONMENTS}")
    channels = resolve_channel_set(bug, channel_set)

    run_id = sanitise_name(f"{bug.id}-{environment.lower()}-{model.id}", suffix_bytes=4)
    results_dir = Path(settings.paths.results) / "runs" / run_id
    results_dir.mkdir(parents=True, exist_ok=True)

    prompt_text = render_prompt(
        bug,
        environment_name=environment,
        wall_clock_s=settings.caps.wall_clock_s,
        visible_channel_ids=list(channels.visible),
    )
    (results_dir / "prompt.md").write_text(prompt_text, encoding="utf-8")
    prompt_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()

    definition = _select_image(settings, bug)
    policy: EgressPolicy = build_policies(settings)[environment]
    runtime = _build_runtime(settings)
    image_digest = runtime.image_digest(definition.alias)

    proxy = EgressProxy(settings)
    netlog_path = results_dir / "netlog.jsonl"
    proxy_run = proxy.start_run(
        run_id, policy, netlog_path=netlog_path, max_cost_usd=settings.caps.max_cost_usd
    )
    mirror: GitMirror | None = None
    if environment == "E2":
        mirror = GitMirror(settings)
        mirror.start()

    handle: Handle | None = None
    started_at = dt.datetime.now(dt.UTC)
    outcome = "harness_error"
    reason: str | None = None
    truncated = False
    answer_result = AnswerResult(
        outcome=AnswerOutcome.NO_ANSWER, reason="run did not reach answer validation"
    )
    agent_result: AgentRunResult | None = None
    transcript_error: str | None = None

    try:
        is_lxd = isinstance(runtime, LXDRuntime)
        network = enforce.ensure_network(runtime, policy) if is_lxd else None
        bridge_ip = _lxd_gateway_ip(runtime, network) if network else "127.0.0.1"

        spec = ContainerSpec(
            image=definition.alias,
            name=sanitise_name(f"segbench-{bug.id}-{environment.lower()}"),
            network=RuntimeNetworkPolicy(
                name=policy.name, network=network, env=proxy_run.env(advertise_host=bridge_ip)
            ),
            wait_for_network=True,
            ready_timeout_s=settings.runtime.ready_timeout_s,
        )
        handle = runtime.create(spec)
        enforce.apply(
            runtime,
            handle,
            policy,
            proxy_host=bridge_ip,
            proxy_port=proxy_run.port,
            ca_cert_pem=proxy.ca_cert_pem,
        )

        _seed_environment(
            runtime,
            handle,
            bug,
            environment,
            settings,
            run_id=run_id,
            bridge_ip=bridge_ip,
            mirror=mirror,
        )
        _push_channels(runtime, handle, bug, channels, remote_dir="/workspace/channels")

        with AnswerSnapshotter(
            runtime,
            handle,
            remote_path=f"/workspace/{ANSWER_FILENAME}",
            out_dir=results_dir / "answers",
            interval_s=min(ANSWER_POLL_INTERVAL_S, max(1.0, settings.caps.wall_clock_s / 20)),
        ):
            agent_result = invoke_agent(
                runtime,
                handle,
                settings=settings,
                model=model,
                prompt=prompt_text,
                wall_clock_s=settings.caps.wall_clock_s,
                agent_user=definition.agent_user,
            )
        truncated = agent_result.timed_out

        (results_dir / "agent_stdout.txt").write_text(
            agent_result.exec_result.stdout, encoding="utf-8"
        )
        (results_dir / "agent_stderr.txt").write_text(
            agent_result.exec_result.stderr, encoding="utf-8"
        )

        local_answer = results_dir / ANSWER_FILENAME
        with contextlib.suppress(RuntimeFailure):
            runtime.pull(handle, f"/workspace/{ANSWER_FILENAME}", local_answer)
        answer_result = validate_answer_file(local_answer)

        if agent_result.session_id:
            try:
                raw_session = export_session(
                    runtime,
                    handle,
                    agent_result.session_id,
                    settings=settings,
                    model=model,
                    agent_user=definition.agent_user,
                )
                (results_dir / "session.json").write_text(raw_session, encoding="utf-8")
                messages = extract_transcript(raw_session)
                write_transcript(messages, results_dir / "transcript.jsonl")
            except (RuntimeFailure, TranscriptError) as exc:
                transcript_error = str(exc)
                log.error(
                    "transcript extraction failed; the raw session (if captured) is on disk, "
                    "transcript.jsonl was not written",
                    extra={"run_id": run_id, "error": str(exc)},
                )

        if proxy_run.meter.tripped:
            outcome = "cost_exceeded"
        elif truncated and not answer_result.ok:
            outcome = "timeout"
        elif answer_result.ok:
            outcome = "ok"
        else:
            outcome = answer_result.outcome.value
        reason = answer_result.reason

    except Exception as exc:  # a harness failure must still tear down and be recorded, not raise
        outcome = "harness_error"
        reason = str(exc)
        log.error("run failed", extra={"run_id": run_id, "error": str(exc)}, exc_info=True)
    finally:
        if handle is not None:
            with _suppress_teardown_errors(run_id):
                runtime.destroy(handle)
        proxy.stop_run(run_id)
        proxy.shutdown()
        if mirror is not None:
            mirror.stop_run(run_id)
            mirror.stop()

    ended_at = dt.datetime.now(dt.UTC)

    leak_attempts = _count_leak_attempts(netlog_path)
    record = RunRecord(
        run_id=run_id,
        bug_id=bug.id,
        environment=environment,
        channel_set=channels.label,
        model=model.id,
        backend=runtime.name,
        image=definition.alias,
        image_digest=image_digest,
        corpus_revision=_corpus_revision(settings.paths.corpus),
        prompt_version=PROMPT_VERSION,
        prompt_hash=prompt_hash,
        caps=RunCaps(
            wall_clock_s=settings.caps.wall_clock_s, max_cost_usd=settings.caps.max_cost_usd
        ),
        started_at=started_at.isoformat(),
        ended_at=ended_at.isoformat(),
        duration_s=(ended_at - started_at).total_seconds(),
        cost=RunCost(
            prompt_tokens=proxy_run.meter.prompt_tokens,
            completion_tokens=proxy_run.meter.completion_tokens,
            usd=proxy_run.meter.usd,
            tripped=proxy_run.meter.tripped,
        ),
        leak_attempts=leak_attempts,
        outcome=outcome,
        truncated=truncated,
        answer_outcome=answer_result.outcome.value,
        answer_reason=answer_result.reason,
        session_id=agent_result.session_id if agent_result else None,
        transcript_error=transcript_error,
        reason=reason,
        results_dir=str(results_dir),
    )
    _append_record(settings, record)
    return record


class _suppress_teardown_errors:
    """Log, never raise, from the ``finally`` block's container teardown.

    A destroy failure must not shadow whatever exception (or lack of one) the run itself produced,
    and must not stop the netlog/cost bookkeeping below it from running.
    """

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: BaseException | None, tb: object) -> bool:
        if exc is not None:
            log.error(
                "container teardown failed", extra={"run_id": self._run_id, "error": str(exc)}
            )
        return True


def _count_leak_attempts(netlog_path: Path) -> int:
    if not netlog_path.is_file():
        return 0
    count = 0
    for line in netlog_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("leak_attempt"):
            count += 1
    return count


def _append_record(settings: Settings, record: RunRecord) -> None:
    path = Path(settings.paths.results) / "runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(record.model_dump_json() + "\n")
