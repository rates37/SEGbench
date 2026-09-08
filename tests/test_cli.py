"""CLI surface: help works, and every stub fails loudly."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from segbench import __version__
from segbench.cli import app

runner = CliRunner()


def test_help_works() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for group in ("corpus", "image", "mirror", "run", "grade", "export"):
        assert group in result.stdout


@pytest.mark.parametrize("group", ["corpus", "image", "mirror", "run", "grade", "export"])
def test_group_help_works(group: str) -> None:
    result = runner.invoke(app, [group, "--help"])

    assert result.exit_code == 0


def test_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert __version__ in result.stdout


@pytest.mark.parametrize(
    "argv",
    [
        ["corpus", "validate"],
        ["image", "build"],
        ["mirror", "sync", "--bug", "lp-1"],
        ["run"],
        ["grade"],
        ["export"],
    ],
)
def test_stubs_raise_not_implemented(argv: list[str]) -> None:
    result = runner.invoke(app, argv)

    assert result.exit_code != 0
    assert isinstance(result.exception, NotImplementedError)
    assert "phase" in str(result.exception)


def test_verbose_flag_is_accepted() -> None:
    result = runner.invoke(app, ["--verbose", "version"])

    assert result.exit_code == 0
