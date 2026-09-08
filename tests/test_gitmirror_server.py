"""The mirror's HTTP smart-protocol server: real `git clone` against a real (small, local) `git
http-backend` CGI process, proving the wire protocol and the request-time resolution work
end-to-end without a container in the loop (plan.md section 13 phase 4).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from segbench.config import Settings
from segbench.netpol.gitmirror import GitMirror, _upstream_cache_path


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return result.stdout


@pytest.fixture
def mirror(tmp_path: Path):
    m = GitMirror(
        Settings(),
        cache_dir=tmp_path / "cache",
        bind_host="127.0.0.1",
        port=0,
        known_hosts={"fakehost.example": "fh"},
    )
    m.start()
    yield m
    m.stop()


def test_clone_of_the_target_repo_is_truncated_and_remote_free_of_future_history(
    mirror: GitMirror, mirror_bug, tmp_path: Path
) -> None:
    bug, _repo, fix_commit = mirror_bug
    netlog = tmp_path / "netlog.jsonl"
    mirror.start_run("run1", bug, netlog_path=netlog)

    clone_dir = tmp_path / "clone"
    url = f"http://127.0.0.1:{mirror.port}/run/run1/target.git"
    result = subprocess.run(
        ["git", "clone", "-q", url, str(clone_dir)], capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
    assert len(_git(clone_dir, "log", "--all", "--format=%H").split()) == 1
    assert (clone_dir / "f.txt").read_text() == "v2\n"
    unreachable = subprocess.run(
        ["git", "-C", str(clone_dir), "cat-file", "-e", fix_commit], capture_output=True
    )
    assert unreachable.returncode != 0

    entries = [json.loads(line) for line in netlog.read_text().splitlines()]
    assert entries
    assert all(e["run_id"] == "run1" and e["allowed"] for e in entries)


def test_clone_of_another_host_is_lazily_fetched_and_cut_at_the_report_date(
    mirror: GitMirror, mirror_bug, tmp_path: Path
) -> None:
    bug, repo, fix_commit = mirror_bug
    mirror.start_run("run2", bug, netlog_path=tmp_path / "netlog.jsonl")

    # The mirror would normally reach `https://fakehost.example/...` itself; stand in for that by
    # pre-seeding the upstream cache at the exact path it would compute, so this test needs no
    # real network while still exercising the full host-slug reconstruction and lazy-fetch path.
    fake_url = "https://fakehost.example/owner/repo.git"
    upstream_cache = _upstream_cache_path(mirror.cache_dir, fake_url)
    upstream_cache.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(upstream_cache)], check=True)

    clone_dir = tmp_path / "other-clone"
    url = f"http://127.0.0.1:{mirror.port}/run/run2/other/fh/owner/repo.git"
    result = subprocess.run(
        ["git", "clone", "-q", url, str(clone_dir)], capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
    assert (clone_dir / "f.txt").read_text() == "v2\n"  # cut at reported_at, not the fix
    unreachable = subprocess.run(
        ["git", "-C", str(clone_dir), "cat-file", "-e", fix_commit], capture_output=True
    )
    assert unreachable.returncode != 0

    entries = [json.loads(line) for line in (tmp_path / "netlog.jsonl").read_text().splitlines()]
    assert any(e.get("resolved_cutoff") for e in entries)


def test_an_unresolvable_repository_is_rejected_with_a_readable_error(
    mirror: GitMirror, mirror_bug, tmp_path: Path
) -> None:
    bug, _repo, _fix = mirror_bug
    mirror.start_run("run3", bug, netlog_path=tmp_path / "netlog.jsonl")

    url = f"http://127.0.0.1:{mirror.port}/run/run3/other/fh/no/such/repo.git"
    result = subprocess.run(
        ["git", "clone", "-q", url, str(tmp_path / "x")], capture_output=True, text=True
    )

    assert result.returncode != 0
    assert "could not resolve" in result.stderr or "not found" in result.stderr


def test_an_unknown_host_slug_is_rejected(mirror: GitMirror, mirror_bug, tmp_path: Path) -> None:
    bug, _repo, _fix = mirror_bug
    mirror.start_run("run4", bug, netlog_path=tmp_path / "netlog.jsonl")

    url = f"http://127.0.0.1:{mirror.port}/run/run4/other/not-a-real-slug/foo/bar.git"
    result = subprocess.run(
        ["git", "clone", "-q", url, str(tmp_path / "x")], capture_output=True, text=True
    )

    assert result.returncode != 0
    assert "no upstream host registered" in result.stderr


def test_an_unknown_run_id_is_rejected(mirror: GitMirror, tmp_path: Path) -> None:
    url = f"http://127.0.0.1:{mirror.port}/run/never-started/target.git"

    result = subprocess.run(
        ["git", "clone", "-q", url, str(tmp_path / "x")], capture_output=True, text=True
    )

    assert result.returncode != 0
    assert "unknown or expired run" in result.stderr


def test_stop_run_removes_its_serving_namespace_but_leaves_the_cache_intact(
    mirror: GitMirror, mirror_bug, tmp_path: Path
) -> None:
    bug, _repo, _fix = mirror_bug
    mirror.start_run("run5", bug, netlog_path=tmp_path / "netlog.jsonl")
    url = f"http://127.0.0.1:{mirror.port}/run/run5/target.git"
    subprocess.run(["git", "clone", "-q", url, str(tmp_path / "first")], check=True)

    mirror.stop_run("run5")

    result = subprocess.run(
        ["git", "clone", "-q", url, str(tmp_path / "second")], capture_output=True, text=True
    )
    assert result.returncode != 0  # the run's namespace is gone

    # But a fresh run for the same bug reuses the already-truncated, cached artifact.
    mirror.start_run("run6", bug, netlog_path=tmp_path / "netlog2.jsonl")
    url6 = f"http://127.0.0.1:{mirror.port}/run/run6/target.git"
    result6 = subprocess.run(
        ["git", "clone", "-q", url6, str(tmp_path / "third")], capture_output=True, text=True
    )
    assert result6.returncode == 0, result6.stderr
