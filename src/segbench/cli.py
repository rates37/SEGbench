"""The ``segbench`` command-line entry point.

The command groups, their names and their global options are fixed here so every phase has an
obvious home. The ``corpus`` group is implemented (phase 1); the remaining commands are stubs that
raise :class:`NotImplementedError` naming the phase that will implement them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from segbench import __version__
from segbench.agent.run import RunError, execute_run
from segbench.config import ModelConfig, Settings, load_settings
from segbench.corpus.add import AddError, fetch_github, fetch_launchpad, next_steps, scaffold
from segbench.corpus.derive import DeriveError, derive_fix, write_derived_fix
from segbench.corpus.findings import Severity
from segbench.corpus.loader import CorpusError, bug_directories, load_bug
from segbench.corpus.scrub import ScrubPolicy
from segbench.corpus.validate import CorpusReport, validate_corpus
from segbench.grade.runner import GradingSummary, run_grading
from segbench.grade.validation import ValidationError, compute_agreement, sample_for_hand_grading
from segbench.logging import configure_logging, get_logger
from segbench.netpol.gitmirror import MirrorError, mirror_status_for, sync_bug
from segbench.netpol.policy import build_policies
from segbench.netpol.verify import VerifyReport, verify_policy
from segbench.runtime.base import Runtime, RuntimeFailure
from segbench.runtime.images import ImageBuilder, load_definitions
from segbench.runtime.lxd import LXDRuntime, sanitise_name
from segbench.runtime.podman import PodmanRuntime
from segbench.runtime.smoke import run_smoke

log = get_logger(__name__)

#: Command *output* goes to stdout so it can be piped; log lines go to stderr via `logging`.
console = Console()

app = typer.Typer(
    name="segbench",
    help="Benchmark harness for LLM diagnosis of real defects in Canonical products.",
    no_args_is_help=True,
    add_completion=False,
)

corpus_app = typer.Typer(name="corpus", help="Load, validate and scaffold bug corpus entries.")
image_app = typer.Typer(name="image", help="Build and inspect the base container image.")
runtime_app = typer.Typer(name="runtime", help="Debug the container runtime backend.")
mirror_app = typer.Typer(name="mirror", help="Manage the truncating git mirror.")
netpol_app = typer.Typer(name="netpol", help="Egress proxy, allowlist and cost metering.")
run_app = typer.Typer(name="run", help="Execute the run matrix.")
grade_app = typer.Typer(name="grade", help="Grade completed runs.")
export_app = typer.Typer(name="export", help="Export graded results for the dashboard.")

_subcommands = (
    corpus_app,
    image_app,
    runtime_app,
    mirror_app,
    netpol_app,
    run_app,
    grade_app,
    export_app,
)
for sub in _subcommands:
    app.add_typer(sub)


def _todo(phase: int, what: str) -> NotImplementedError:
    """Build the uniform stub error, naming the plan.md phase that will implement the command."""
    return NotImplementedError(
        f"{what} is not implemented yet; it lands in phase {phase} (plan.md section 13)."
    )


@app.callback()
def main_callback(
    ctx: typer.Context,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Enable debug-level logging.")
    ] = False,
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Config file to load instead of segbench.toml."),
    ] = None,
) -> None:
    """Load configuration and install logging before any subcommand runs."""
    settings = load_settings(config)
    if verbose:
        settings = settings.model_copy(update={"verbose": True})
    configure_logging(log_file=settings.paths.log_file, verbose=settings.verbose)
    ctx.obj = settings
    log.debug("configuration loaded", extra={"config_file": str(config) if config else None})


@app.command()
def version() -> None:
    """Print the segbench version."""
    typer.echo(__version__)


def _settings(ctx: typer.Context) -> Settings:
    return ctx.obj if isinstance(ctx.obj, Settings) else load_settings(None)


def _scrub_policy(settings: Settings) -> ScrubPolicy:
    """Build the scrubber's policy from the ``[corpus]`` config section."""
    return ScrubPolicy(
        customer_host_patterns=tuple(settings.corpus.customer_host_patterns),
        allow_values=tuple(settings.corpus.allow_values),
        allow_email_domains=tuple(settings.corpus.allow_email_domains),
        allow_mac_prefixes=tuple(settings.corpus.allow_mac_prefixes),
    )


def _print_corpus_report(report: CorpusReport) -> None:
    """Render the validation pass: a per-bug summary table, then the findings themselves."""
    for message in report.load_errors:
        console.print(f"[red]load error:[/red] {message}")

    table = Table(title="corpus validation", title_justify="left")
    table.add_column("bug")
    table.add_column("channels", justify="right")
    table.add_column("ready")
    table.add_column("errors", justify="right")
    table.add_column("warnings", justify="right")
    table.add_column("status")

    for bug in report.bugs:
        table.add_row(
            bug.bug_id,
            str(bug.channel_count),
            "yes" if bug.ready else "no",
            str(len(bug.errors)),
            str(len(bug.warnings)),
            "[green]pass[/green]" if bug.ok else "[red]FAIL[/red]",
        )
    console.print(table)

    for bug in report.bugs:
        for finding in bug.findings:
            colour = "red" if finding.severity is Severity.ERROR else "yellow"
            excerpt = f" ({finding.excerpt})" if finding.excerpt else ""
            console.print(
                f"[{colour}]{finding.severity.value}[/{colour}] {finding.location} "
                f"{finding.detector}: {finding.message}{excerpt}"
            )

    verdict = "[green]OK[/green]" if report.ok else "[red]FAILED[/red]"
    console.print(
        f"\n{verdict} — {len(report.bugs)} bug(s), {report.error_count} error(s), "
        f"{report.warning_count} warning(s)"
    )


@corpus_app.command("validate")
def corpus_validate(
    ctx: typer.Context,
    bug: Annotated[
        str | None, typer.Option("--bug", help="Validate only this bug id; default is all.")
    ] = None,
) -> None:
    """Check every bug against the schema, the leakage rules and the scrubber.

    Exits non-zero on any hard failure: a bug that will not load, a scrubber hit, or a channel
    referencing the fix. Similarity flags and unreviewed ground truths are warnings and do not
    fail the run.
    """
    settings = _settings(ctx)
    report = validate_corpus(
        settings.paths.corpus,
        bug_ids=[bug] if bug else None,
        policy=_scrub_policy(settings),
        similarity_threshold=settings.corpus.similarity_threshold,
    )
    _print_corpus_report(report)
    raise typer.Exit(code=0 if report.ok else 1)


@corpus_app.command("add")
def corpus_add(
    ctx: typer.Context,
    launchpad: Annotated[
        str | None, typer.Option(help="Launchpad bug number, e.g. 2048221.")
    ] = None,
    github: Annotated[
        str | None, typer.Option(help="GitHub issue reference, as OWNER/REPO#NUMBER.")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing scaffold for this bug.")
    ] = False,
) -> None:
    """Scaffold a bug directory from a tracker into a raw/ staging area.

    Fetches tracker metadata and comments only. Splitting raw text into channels is a supervised
    step (plan.md section 3.4) and is deliberately not automated.
    """
    settings = _settings(ctx)
    if bool(launchpad) == bool(github):
        console.print("[red]error:[/red] pass exactly one of --launchpad or --github")
        raise typer.Exit(code=2)

    try:
        fetched = fetch_launchpad(launchpad) if launchpad else fetch_github(github or "")
        directory = scaffold(fetched, settings.paths.corpus, force=force)
    except AddError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(next_steps(directory))


@corpus_app.command("derive")
def corpus_derive(
    ctx: typer.Context,
    bug: Annotated[str, typer.Option("--bug", help="Bug id.")],
    repo: Annotated[
        Path, typer.Option("--repo", help="Path to a local clone containing the fix commit.")
    ],
    commit: Annotated[
        str | None,
        typer.Option("--commit", help="Override ground_truth.fix.commit for this derivation."),
    ] = None,
    write: Annotated[
        bool, typer.Option("--write/--no-write", help="Write the result into ground_truth.yaml.")
    ] = True,
) -> None:
    """Derive ground_truth fix.files and fix.symbols mechanically from the fix commit.

    Phase 1 reads the commit from a local clone; phase 4 will route this through the truncating
    mirror. These two fields are never hand-maintained (plan.md section 3.3).
    """
    settings = _settings(ctx)
    directory = settings.paths.corpus / "bugs" / bug
    try:
        loaded = load_bug(directory)
        derived = derive_fix(repo, commit or loaded.ground_truth.fix.commit)
    except (CorpusError, DeriveError) as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"commit {derived.commit}")
    console.print(f"files ({len(derived.files)}):")
    for path in derived.files:
        console.print(f"  {path}")
    if derived.symbols is None:
        console.print("symbols: null (no extractor for this bug's languages)")
    else:
        console.print(f"symbols ({len(derived.symbols)}):")
        for symbol in derived.symbols:
            console.print(f"  {symbol}")

    if write:
        write_derived_fix(directory / "ground_truth.yaml", derived)
        console.print(f"\nwrote fix.files and fix.symbols to {directory / 'ground_truth.yaml'}")


def _runtime(settings: Settings) -> Runtime:
    """Build the configured backend. Raises :class:`BackendUnavailable` if it is not usable."""
    backend: Runtime
    if settings.runtime.backend == "lxd":
        backend = LXDRuntime(project=settings.runtime.lxd_project)
    else:
        backend = PodmanRuntime()
    backend.preflight()
    return backend


def _builder(settings: Settings) -> ImageBuilder:
    """Build the image builder. LXD only: see segbench.runtime.podman for why."""
    if settings.runtime.backend != "lxd":
        console.print(
            f"[red]error:[/red] image building requires the lxd backend; "
            f"runtime.backend is {settings.runtime.backend!r}"
        )
        raise typer.Exit(code=2)
    runtime = LXDRuntime(project=settings.runtime.lxd_project)
    runtime.preflight()
    return ImageBuilder(
        runtime=runtime,
        cache_dir=settings.paths.image_cache,
        definitions_dir=settings.runtime.image_definitions,
    )


@image_app.command("build")
def image_build(
    ctx: typer.Context,
    overlay: Annotated[
        str | None,
        typer.Option(
            "--overlay",
            help="Build this product overlay (and its parents) instead of just the base.",
        ),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Rebuild even if the definition is unchanged.")
    ] = False,
) -> None:
    """Build or refresh the container image and record its digest.

    Idempotent: an image whose definition and provisioning script are unchanged, and whose alias
    still resolves to the recorded fingerprint, is left alone. The build needs outbound network
    and takes several minutes from cold — see the README.
    """
    settings = _settings(ctx)
    builder = _builder(settings)
    try:
        results = builder.build(overlay or "base", force=force)
    except RuntimeFailure as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    for result in results:
        if result.rebuilt:
            console.print(
                f"[green]built[/green] {result.name} -> {result.alias} "
                f"({result.fingerprint[:12]}) in {result.duration_s:.0f}s"
            )
        else:
            console.print(
                f"[dim]up to date[/dim] {result.name} -> {result.alias} ({result.fingerprint[:12]})"
            )


@image_app.command("status")
def image_status(ctx: typer.Context) -> None:
    """Print every image definition, its alias, its digest and whether it is stale."""
    settings = _settings(ctx)
    builder = _builder(settings)
    try:
        statuses = builder.status()
    except RuntimeFailure as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(title="images", title_justify="left")
    table.add_column("name")
    table.add_column("alias")
    table.add_column("digest")
    table.add_column("built (UTC)")
    table.add_column("status")

    for status in statuses:
        colour = "green" if not status.stale else "yellow"
        table.add_row(
            status.name,
            status.alias,
            status.fingerprint[:12] if status.fingerprint else "-",
            status.built_at.strftime("%Y-%m-%d %H:%M") if status.built_at else "-",
            f"[{colour}]{status.summary}[/{colour}]",
        )
    console.print(table)


@runtime_app.command("smoke")
def runtime_smoke(
    ctx: typer.Context,
    image: Annotated[
        str | None,
        typer.Option("--image", help="Image alias to test; default is the base image's alias."),
    ] = None,
) -> None:
    """Create a container, exec, push, pull, kill a background process, and destroy it.

    A debug command: it proves the runtime layer end to end and prints per-step timings. Killing
    it mid-run leaves no container behind — the backend registers every container it creates with
    a cleanup registry that fires on SIGINT and on exit.
    """
    settings = _settings(ctx)
    runtime = _runtime(settings)

    base = load_definitions(settings.runtime.image_definitions)["base"]
    if image is None:
        if settings.runtime.backend != "lxd":
            console.print("[red]error:[/red] pass --image; only the lxd backend has a default")
            raise typer.Exit(code=2)
        image = base.alias

    try:
        result = run_smoke(runtime, image, name_factory=sanitise_name, agent_user=base.agent_user)
    except RuntimeFailure as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(title=f"runtime smoke ({result.backend})", title_justify="left")
    table.add_column("step")
    table.add_column("seconds", justify="right")
    table.add_column("detail")
    for step in result.steps:
        table.add_row(step.name, f"{step.duration_s:.2f}", step.detail)
    console.print(table)
    console.print(f"image {result.image} ({result.image_digest[:12]})")
    console.print(f"container {result.container}")

    if result.ok:
        console.print(f"\n[green]PASS[/green] — {result.total_s:.1f}s total")
    else:
        console.print(f"\n[red]FAILED[/red] — {result.error}")
    raise typer.Exit(code=0 if result.ok else 1)


@mirror_app.command("sync")
def mirror_sync(
    ctx: typer.Context,
    bug: Annotated[str, typer.Option(help="Bug id.")],
    force: Annotated[
        bool, typer.Option("--force", help="Rebuild even if a cached truncation exists.")
    ] = False,
) -> None:
    """Populate the truncating git mirror for one bug's target repository.

    Fetches the repository (host-side network access only — CLAUDE.md invariant 2), resolves
    ``repo.pre_fix_ref``, and caches the result as a single rootless commit. Idempotent: a repeat
    call against an unchanged ``pre_fix_ref`` touches no network at all.
    """
    settings = _settings(ctx)
    directory = settings.paths.corpus / "bugs" / bug
    try:
        loaded = load_bug(directory)
        result = sync_bug(settings.paths.mirror_cache, loaded, force=force)
    except (CorpusError, MirrorError) as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"bug: {result.bug_id}")
    console.print(f"repo: {result.repo_url}")
    console.print(f"pre_fix_ref: {result.pre_fix_ref}")
    console.print(f"truncated commit: {result.commit}")
    console.print(f"cached at: {result.path}")


@mirror_app.command("status")
def mirror_status_cmd(ctx: typer.Context) -> None:
    """Show every bug's target-mirror cache state: cached or not, and the source commit."""
    settings = _settings(ctx)
    table = Table(title="git mirror status", title_justify="left")
    table.add_column("bug")
    table.add_column("cached")
    table.add_column("source commit")
    table.add_column("path")

    for directory in bug_directories(settings.paths.corpus):
        try:
            loaded = load_bug(directory)
        except CorpusError as exc:
            table.add_row(directory.name, "[red]error[/red]", "-", str(exc))
            continue
        status = mirror_status_for(settings.paths.mirror_cache, loaded)
        table.add_row(
            status.bug_id,
            "[green]yes[/green]" if status.cached else "no",
            (status.source_commit or "-")[:12],
            str(status.path),
        )
    console.print(table)


def _print_verify_report(report: VerifyReport) -> None:
    table = Table(title=f"netpol verify — {report.policy}", title_justify="left")
    table.add_column("check")
    table.add_column("expected")
    table.add_column("result")
    table.add_column("detail")
    for result in report.results:
        expected = "reachable" if result.check.expect_reachable else "blocked"
        via = "via proxy" if result.check.via_proxy else "bypassing proxy"
        status = "[green]pass[/green]" if result.passed else "[red]FAIL[/red]"
        table.add_row(f"{result.check.name} ({via})", expected, status, result.detail)
    console.print(table)
    verdict = "[green]PASS[/green]" if report.ok else "[red]FAIL[/red]"
    console.print(f"\n{verdict} — {report.policy}")


@netpol_app.command("verify")
def netpol_verify(
    ctx: typer.Context,
    env: Annotated[
        str, typer.Option("--env", help="Policy to verify: E0, E1, E2, or 'all'.")
    ] = "all",
    image: Annotated[
        str | None, typer.Option("--image", help="Image alias; default is the base image's alias.")
    ] = None,
) -> None:
    """Prove a network policy holds against a real container.

    Boots a container wired up exactly as a benchmark run would be — proxy, dedicated routeless
    LXD network, /etc/hosts pins, iptables, the proxy's MITM CA — then asserts the inference
    endpoint is reachable and that a representative set of tracker, search, package-index and
    DNS-exfiltration destinations are not, some of them bypassing the proxy entirely to prove the
    defence-in-depth layers hold on their own. This is the command to paste into documentation as
    evidence the benchmark's no-leakage invariant (CLAUDE.md) is real rather than asserted.
    """
    settings = _settings(ctx)
    runtime = _runtime(settings)

    if image is None:
        base = load_definitions(settings.runtime.image_definitions)["base"]
        image = base.alias

    policies = sorted(build_policies(settings)) if env.lower() == "all" else [env.upper()]
    reports: list[VerifyReport] = []
    for name in policies:
        try:
            report = verify_policy(runtime, image, name, settings)
        except (RuntimeFailure, ValueError) as exc:
            console.print(f"[red]error:[/red] {name}: {exc}")
            raise typer.Exit(code=1) from exc
        _print_verify_report(report)
        reports.append(report)

    raise typer.Exit(code=0 if all(r.ok for r in reports) else 1)


@run_app.callback(invoke_without_command=True)
def run_matrix(
    ctx: typer.Context,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the matrix and cost estimate; execute nothing.")
    ] = False,
    bugs: Annotated[str | None, typer.Option(help="Comma-separated bug ids.")] = None,
    models: Annotated[str | None, typer.Option(help="Comma-separated model ids.")] = None,
    environments: Annotated[str | None, typer.Option(help="Comma-separated: E0,E1,E2.")] = None,
) -> None:
    """Execute the run matrix, or a subset of it."""
    if ctx.invoked_subcommand is not None:
        return
    raise _todo(7, "run")


@run_app.command("once")
def run_once(
    ctx: typer.Context,
    bug: Annotated[str, typer.Option("--bug", help="Bug id.")],
    env: Annotated[str, typer.Option("--env", help="Environment: E0, E1, or E2.")],
    channels: Annotated[
        str, typer.Option("--channels", help="'full', or 'loo:<channel_id>'.")
    ] = "full",
    model: Annotated[
        str | None,
        typer.Option("--model", help="Model id; default is the first entry in the model matrix."),
    ] = None,
) -> None:
    """Execute a single (bug, environment, channel set, model) cell, for debugging.

    Provisions a container and its network policy, seeds the environment, runs the agent, and
    writes a complete run directory under ``results/runs/<run_id>`` plus one line to
    ``results/runs.jsonl`` — the same artefacts a campaign run produces, just one of them.
    """
    settings = _settings(ctx)
    try:
        loaded = load_bug(settings.paths.corpus / "bugs" / bug)
    except CorpusError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    model_config: ModelConfig | None = (
        next((m for m in settings.models if m.id == model), None)
        if model
        else (settings.models[0] if settings.models else None)
    )
    if model_config is None:
        if model:
            console.print(f"[red]error:[/red] model {model!r} is not in the model matrix")
        else:
            console.print("[red]error:[/red] --model is required; the model matrix is empty")
        raise typer.Exit(code=2)

    try:
        record = execute_run(
            settings, bug=loaded, environment=env.upper(), channel_set=channels, model=model_config
        )
    except RunError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"run: {record.run_id}")
    console.print(f"outcome: {record.outcome} (answer: {record.answer_outcome})")
    console.print(f"truncated: {record.truncated}")
    tokens = f"{record.cost.prompt_tokens}+{record.cost.completion_tokens} tokens"
    console.print(f"cost: ${record.cost.usd:.4f} ({tokens})")
    console.print(f"leak attempts: {record.leak_attempts}")
    console.print(f"results: {record.results_dir}")
    raise typer.Exit(code=0 if record.outcome == "ok" else 1)


def _print_grading_summary(summary: GradingSummary) -> None:
    console.print(
        f"graded: {len(summary.graded)}  skipped (already graded): {len(summary.skipped)}  "
        f"failed: {len(summary.failed)}"
    )
    for run_id, error in summary.failed:
        console.print(f"[red]error:[/red] {run_id}: {error}")


@grade_app.callback(invoke_without_command=True)
def grade_runs(
    ctx: typer.Context,
    pending: Annotated[bool, typer.Option("--pending", help="Grade only ungraded runs.")] = False,
    regrade: Annotated[
        bool, typer.Option("--regrade", help="Re-grade runs that already have a grade record.")
    ] = False,
    run_id: Annotated[
        str | None, typer.Option("--run-id", help="Grade (or re-grade) only this run id.")
    ] = None,
) -> None:
    """Grade runs with the deterministic checks and the pinned judge.

    A separate pass over ``results/runs.jsonl``: never re-runs inference against the model under
    test. ``--regrade`` re-writes ``results/grades.jsonl`` from scratch, so changing a scoring
    weight in config and re-running with ``--regrade`` changes scores without paying for
    inference again (plan.md section 7, CLAUDE.md invariant 4).
    """
    if ctx.invoked_subcommand is not None:
        return
    del pending  # documented no-op: grading ungraded runs is already the default behaviour
    settings = _settings(ctx)
    summary = run_grading(settings, regrade=regrade, run_id=run_id)
    _print_grading_summary(summary)
    raise typer.Exit(code=0 if not summary.failed else 1)


@grade_app.command("sample")
def grade_sample(
    ctx: typer.Context,
    n: Annotated[int, typer.Option("--n", help="Number of runs to sample.")] = 40,
    out: Annotated[
        Path, typer.Option("--out", help="Directory to write neutral hand-grading forms into.")
    ] = Path("docs/judge-validation"),
    seed: Annotated[int, typer.Option(help="Sampling seed, for a reproducible sample.")] = 0,
) -> None:
    """Emit a neutral hand-gradable form per sampled run (plan.md section 7.4).

    Each form carries the agent's answer and the ground truth only — no model identity, no
    environment, no channel set, and no score the judge already assigned — so a maintainer's
    hand-grading is not anchored on the judge's own output.
    """
    settings = _settings(ctx)
    try:
        paths = sample_for_hand_grading(settings, n=n, out_dir=out, seed=seed)
    except ValidationError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"wrote {len(paths)} form(s) to {out}")
    console.print("fill in human_root_cause_score and human_fix_score in each, then run:")
    console.print(f"  segbench grade agreement --sample-dir {out}")


@grade_app.command("agreement")
def grade_agreement(
    ctx: typer.Context,
    sample_dir: Annotated[
        Path, typer.Option("--sample-dir", help="Directory produced by `segbench grade sample`.")
    ] = Path("docs/judge-validation"),
) -> None:
    """Compare hand-graded forms against the judge's scores (plan.md section 7.4)."""
    settings = _settings(ctx)
    try:
        agreement = compute_agreement(settings, sample_dir)
    except ValidationError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(title="judge agreement", title_justify="left")
    table.add_column("axis")
    table.add_column("n", justify="right")
    table.add_column("exact match", justify="right")
    table.add_column("within one", justify="right")
    table.add_column("Cohen's kappa", justify="right")
    for label, stats in (("root_cause_score", agreement.root_cause), ("fix_score", agreement.fix)):
        table.add_row(
            label,
            str(stats.n),
            f"{stats.exact_match:.2f}" if stats.n else "-",
            f"{stats.within_one:.2f}" if stats.n else "-",
            f"{stats.kappa:.2f}" if stats.n else "-",
        )
    console.print(table)


@export_app.callback(invoke_without_command=True)
def export_results(ctx: typer.Context) -> None:
    """Aggregate graded runs into the dashboard's data.json."""
    if ctx.invoked_subcommand is not None:
        return
    raise _todo(8, "export")


def main() -> None:
    """Console-script entry point.

    ``Ctrl-C`` exits 130 without a traceback. The containers are already gone by this point: the
    runtime's cleanup registry destroys them from inside the signal handler, before the
    ``KeyboardInterrupt`` reaches here.
    """
    try:
        app()
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted[/yellow] — containers cleaned up")
        raise typer.Exit(code=130) from None


if __name__ == "__main__":
    main()
