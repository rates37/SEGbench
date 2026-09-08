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
from segbench.config import Settings, load_settings
from segbench.corpus.add import AddError, fetch_github, fetch_launchpad, next_steps, scaffold
from segbench.corpus.derive import DeriveError, derive_fix, write_derived_fix
from segbench.corpus.findings import Severity
from segbench.corpus.loader import CorpusError, load_bug
from segbench.corpus.scrub import ScrubPolicy
from segbench.corpus.validate import CorpusReport, validate_corpus
from segbench.logging import configure_logging, get_logger

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
mirror_app = typer.Typer(name="mirror", help="Manage the truncating git mirror.")
run_app = typer.Typer(name="run", help="Execute the run matrix.")
grade_app = typer.Typer(name="grade", help="Grade completed runs.")
export_app = typer.Typer(name="export", help="Export graded results for the dashboard.")

for sub in (corpus_app, image_app, mirror_app, run_app, grade_app, export_app):
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


@image_app.command("build")
def image_build(ctx: typer.Context) -> None:
    """Build or refresh the base container image and record its digest."""
    raise _todo(2, "image build")


@mirror_app.command("sync")
def mirror_sync(ctx: typer.Context, bug: Annotated[str, typer.Option(help="Bug id.")]) -> None:
    """Populate the truncating git mirror for one bug's target repository."""
    raise _todo(4, "mirror sync")


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


@grade_app.callback(invoke_without_command=True)
def grade_runs(
    ctx: typer.Context,
    pending: Annotated[bool, typer.Option("--pending", help="Grade only ungraded runs.")] = False,
) -> None:
    """Grade runs with the deterministic checks and the pinned judge."""
    if ctx.invoked_subcommand is not None:
        return
    raise _todo(6, "grade")


@export_app.callback(invoke_without_command=True)
def export_results(ctx: typer.Context) -> None:
    """Aggregate graded runs into the dashboard's data.json."""
    if ctx.invoked_subcommand is not None:
        return
    raise _todo(8, "export")


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
