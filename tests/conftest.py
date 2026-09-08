"""Shared fixtures for the corpus tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "corpus"


@pytest.fixture
def good_corpus() -> Path:
    """Corpus root containing two complete bugs that must validate clean."""
    return FIXTURE_ROOT / "good"


@pytest.fixture
def leaky_corpus() -> Path:
    """Corpus root containing one bug that leaks the fix into its channels."""
    return FIXTURE_ROOT / "leaky"


@pytest.fixture
def unscrubbed_corpus() -> Path:
    """Corpus root containing one bug committed straight out of a support bundle."""
    return FIXTURE_ROOT / "unscrubbed"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def fix_repo(tmp_path: Path) -> Path:
    """A two-commit git repository whose second commit touches Python, Go and Markdown.

    The Python change is inside ``Driver.stop``; the Go change is inside ``(*Server).Handle``; the
    Markdown change exists so ``derive`` has a file with no symbol extractor to skip.
    """
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")

    (repo / "pkg" / "mod.py").write_text(
        "class Driver:\n"
        "    def start(self):\n"
        "        return 1\n"
        "\n"
        "    def stop(self):\n"
        "        return 2\n"
        "\n"
        "\n"
        "def helper():\n"
        "    return 3\n",
        encoding="utf-8",
    )
    (repo / "main.go").write_text(
        "package main\n"
        "\n"
        "func Alpha() int {\n"
        "\treturn 1\n"
        "}\n"
        "\n"
        "func (s *Server) Handle() error {\n"
        "\treturn nil\n"
        "}\n"
        "\n"
        "type Config struct {\n"
        "\tName string\n"
        "}\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("readme\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")

    (repo / "pkg" / "mod.py").write_text(
        (repo / "pkg" / "mod.py").read_text(encoding="utf-8").replace("return 2", "return 22"),
        encoding="utf-8",
    )
    (repo / "main.go").write_text(
        (repo / "main.go").read_text(encoding="utf-8").replace("return nil", "return errBoom"),
        encoding="utf-8",
    )
    (repo / "README.md").write_text("readme\nmore\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "the fix")
    return repo
