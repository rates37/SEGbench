"""The ``segbench`` command-line entry point.

Every command below is a phase-0 stub: the command groups, their names and their global options
are fixed here so later phases have an obvious home, but each raises :class:`NotImplementedError`
naming the phase that will implement it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from segbench import __version__
from segbench.config import Settings, load_settings
from segbench.logging import configure_logging, get_logger

log = get_logger(__name__)

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


@corpus_app.command("validate")
def corpus_validate(ctx: typer.Context) -> None:
    """Check every bug against the schema, the leakage rules and the scrubber."""
    raise _todo(1, "corpus validate")


@corpus_app.command("add")
def corpus_add(
    ctx: typer.Context,
    launchpad: Annotated[str | None, typer.Option(help="Launchpad bug number.")] = None,
    github: Annotated[str | None, typer.Option(help="GitHub issue URL.")] = None,
) -> None:
    """Scaffold a bug directory from a tracker into a raw/ staging area."""
    raise _todo(1, "corpus add")


@corpus_app.command("derive")
def corpus_derive(ctx: typer.Context, bug: Annotated[str, typer.Option(help="Bug id.")]) -> None:
    """Derive ground_truth fix.files and fix.symbols mechanically from the fix commit."""
    raise _todo(1, "corpus derive")


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
