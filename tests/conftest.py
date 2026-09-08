"""Shared fixtures for the corpus tests."""

from __future__ import annotations

import datetime as dt
import os
import subprocess
from pathlib import Path

import pytest

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


def _dated_commit(repo: Path, filename: str, content: str, message: str, date: str) -> str:
    """Commit ``content`` to ``filename``, backdated so cutoff-based mirror truncation has
    something meaningful to distinguish between."""
    (repo / filename).write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", message],
        check=True,
        capture_output=True,
        env=env,
    )
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


@pytest.fixture
def mirror_bug(tmp_path: Path) -> tuple[Bug, Path, str]:
    """A bug whose target repository is a real, local three-commit history: a commit before the
    report, the commit that is ``pre_fix_ref`` (and also what ``source.reported_at`` should
    resolve a cutoff-based lookup to), and a "future" fix commit that neither environment may ever
    let the agent see. Returns ``(bug, upstream_repo_path, fix_commit)``.
    """
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")

    _dated_commit(repo, "f.txt", "v1\n", "before the report", "2024-01-01T00:00:00Z")
    pre_fix_ref = _dated_commit(repo, "f.txt", "v2\n", "at report time", "2024-02-01T00:00:00Z")
    fix_commit = _dated_commit(repo, "f.txt", "v3 - the fix\n", "the fix", "2024-03-01T00:00:00Z")

    channel_path = tmp_path / "channels" / "customer_report.md"
    channel_path.parent.mkdir(parents=True, exist_ok=True)
    channel_path.write_text("something is wrong\n", encoding="utf-8")

    manifest = BugManifest(
        id="mirror-fixture",
        title="a fixture bug for the git mirror tests",
        product=Product.OTHER,
        source=BugSource(
            tracker=Tracker.REPRODUCTION, reported_at=dt.datetime(2024, 2, 15, tzinfo=dt.UTC)
        ),
        repo=BugRepo(url=str(repo), pre_fix_ref=pre_fix_ref),
        channels=[
            ChannelSpec(
                id=ChannelId.CUSTOMER_REPORT,
                file=Path("channels/customer_report.md"),
                origin=Origin.VERBATIM,
            )
        ],
    )
    ground_truth = GroundTruth(root_cause="x", component="c", fix=FixInfo(commit=fix_commit))
    bug = Bug(
        directory=tmp_path,
        manifest=manifest,
        ground_truth=ground_truth,
        channels=[
            LoadedChannel(
                id=ChannelId.CUSTOMER_REPORT,
                origin=Origin.VERBATIM,
                path=channel_path,
                text="something is wrong\n",
            )
        ],
    )
    return bug, repo, fix_commit
