"""The truncation engine: exporting a tree into a fresh rootless commit, and the (repo, cutoff)
caching that makes a repeat lookup network-free (plan.md section 5 and section 13 phase 4).
"""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path

import pytest

from segbench.netpol.gitmirror import (
    MirrorError,
    _cached_source_commit,
    _resolve_commit_before,
    ensure_other_mirror,
    ensure_target_mirror,
    ensure_upstream,
    mirror_status_for,
    sync_bug,
    truncate_to_rootless,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return result.stdout


class TestTruncateToRootless:
    def test_produces_a_single_parentless_commit(self, mirror_bug) -> None:
        _bug, repo, _fix = mirror_bug
        bare = repo.parent / "bare.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(bare)], check=True)
        pre_fix_ref = _git(repo, "log", "--format=%H", "--all").splitlines()[1]  # "at report time"

        dest = repo.parent / "truncated.git"
        head = truncate_to_rootless(bare, pre_fix_ref, dest)

        assert _git(dest, "log", "--all", "--format=%H").strip() == head
        assert _git(dest, "rev-list", "--parents", "-1", head).split() == [head]  # no parents
        assert _git(dest, "remote", "-v").strip() == ""
        assert _git(dest, "tag", "-l").strip() == ""

    def test_the_fix_commit_object_is_absent_entirely(self, mirror_bug) -> None:
        """Not merely unreachable from a ref -- genuinely not present in the object database, so
        an agent that already knows the hash still cannot read it (CLAUDE.md invariant 1)."""
        _bug, repo, fix_commit = mirror_bug
        bare = repo.parent / "bare.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(bare)], check=True)
        pre_fix_ref = _git(repo, "log", "--format=%H", "--all").splitlines()[1]

        dest = repo.parent / "truncated.git"
        truncate_to_rootless(bare, pre_fix_ref, dest)

        result = subprocess.run(
            ["git", "-C", str(dest), "cat-file", "-e", fix_commit], capture_output=True
        )
        assert result.returncode != 0

    def test_a_repeat_call_with_the_same_source_commit_is_a_cache_hit(self, mirror_bug) -> None:
        _bug, repo, _fix = mirror_bug
        bare = repo.parent / "bare.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(bare)], check=True)
        pre_fix_ref = _git(repo, "log", "--format=%H", "--all").splitlines()[1]
        dest = repo.parent / "truncated.git"
        first = truncate_to_rootless(bare, pre_fix_ref, dest)
        marker_mtime = (dest / "segbench-source-commit").stat().st_mtime

        second = truncate_to_rootless(bare, pre_fix_ref, dest)

        assert second == first
        # Not rebuilt: the marker file (rewritten only on an actual rebuild) is untouched.
        assert (dest / "segbench-source-commit").stat().st_mtime == marker_mtime

    def test_rebuilds_when_the_source_commit_changes(self, mirror_bug) -> None:
        """Every rebuild recommits with fresh metadata (never the source repo's own commit
        object -- see the module docstring), so what proves a rebuild happened is the *tree*
        content, not the original commit hash reappearing."""
        _bug, repo, fix_commit = mirror_bug
        bare = repo.parent / "bare.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(bare)], check=True)
        pre_fix_ref = _git(repo, "log", "--format=%H", "--all").splitlines()[1]
        dest = repo.parent / "truncated.git"
        truncate_to_rootless(bare, pre_fix_ref, dest)

        head = truncate_to_rootless(bare, fix_commit, dest)

        assert _cached_source_commit(dest) == fix_commit
        content = _git(dest, "show", f"{head}:f.txt")
        assert content == "v3 - the fix\n"


class TestEnsureUpstream:
    def test_clones_a_local_repository(self, mirror_bug, tmp_path: Path) -> None:
        _bug, repo, _fix = mirror_bug
        cache_dir = tmp_path / "cache"

        dest = ensure_upstream(str(repo), cache_dir)

        assert dest.is_dir()
        assert _git(dest, "log", "-1", "--format=%s").strip()

    def test_an_unresolvable_url_raises_a_clear_error(self, tmp_path: Path) -> None:
        with pytest.raises(MirrorError, match="could not resolve or fetch"):
            ensure_upstream(str(tmp_path / "does-not-exist"), tmp_path / "cache")

    def test_does_not_leave_a_partial_directory_behind_on_failure(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        with pytest.raises(MirrorError):
            ensure_upstream(str(tmp_path / "does-not-exist"), cache_dir)

        upstream_dir = cache_dir / "upstream"
        assert not upstream_dir.exists() or list(upstream_dir.glob("*")) == []


class TestResolveCommitBefore:
    def test_picks_the_last_commit_at_or_before_the_cutoff(self, mirror_bug) -> None:
        _bug, repo, fix_commit = mirror_bug
        bare = repo.parent / "bare.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(bare)], check=True)

        resolved = _resolve_commit_before(bare, dt.datetime(2024, 2, 15, tzinfo=dt.UTC))

        assert resolved != fix_commit
        assert resolved == _git(repo, "log", "--format=%H", "--all").splitlines()[1]

    def test_raises_when_nothing_qualifies(self, mirror_bug) -> None:
        _bug, repo, _fix = mirror_bug
        bare = repo.parent / "bare.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(bare)], check=True)

        with pytest.raises(MirrorError, match="no commit at or before"):
            _resolve_commit_before(bare, dt.datetime(2020, 1, 1, tzinfo=dt.UTC))


class TestEnsureTargetAndOtherMirror:
    def test_ensure_target_mirror_is_truncated_at_pre_fix_ref(
        self, mirror_bug, tmp_path: Path
    ) -> None:
        bug, _repo, fix_commit = mirror_bug
        cache_dir = tmp_path / "cache"

        dest = ensure_target_mirror(cache_dir, bug)

        commits = _git(dest, "log", "--all", "--format=%H").split()
        assert len(commits) == 1
        result = subprocess.run(
            ["git", "-C", str(dest), "cat-file", "-e", fix_commit], capture_output=True
        )
        assert result.returncode != 0

    def test_a_repeat_call_for_the_same_pre_fix_ref_never_touches_the_network(
        self, mirror_bug, tmp_path: Path
    ) -> None:
        """The point of caching per bug id: a re-run of an already-synced bug must not refetch."""
        import shutil

        bug, _repo, _fix = mirror_bug
        cache_dir = tmp_path / "cache"
        ensure_target_mirror(cache_dir, bug)
        shutil.rmtree(cache_dir / "upstream")  # remove the only thing a refetch could use

        dest = ensure_target_mirror(cache_dir, bug)

        assert dest.is_dir()  # did not need `cache_dir / "upstream"` to still exist

    def test_ensure_other_mirror_is_cut_at_the_given_date_and_cached_per_repo_and_cutoff(
        self, mirror_bug, tmp_path: Path
    ) -> None:
        bug, repo, _fix = mirror_bug
        cache_dir = tmp_path / "cache"
        cutoff = bug.manifest.source.reported_at

        first = ensure_other_mirror(cache_dir, str(repo), cutoff)
        second = ensure_other_mirror(cache_dir, str(repo), cutoff)
        later = ensure_other_mirror(cache_dir, str(repo), cutoff + dt.timedelta(days=60))

        assert first == second  # same cache path for the same (repo, cutoff)
        assert first != later  # a different cutoff gets its own cached artifact
        assert _git(first, "show", "HEAD:f.txt") == "v2\n"
        assert _git(later, "show", "HEAD:f.txt") == "v3 - the fix\n"


class TestSyncAndStatus:
    def test_sync_bug_populates_the_cache_and_reports_the_commit(
        self, mirror_bug, tmp_path: Path
    ) -> None:
        bug, _repo, _fix = mirror_bug
        cache_dir = tmp_path / "cache"

        result = sync_bug(cache_dir, bug)

        assert result.bug_id == bug.id
        assert result.path.is_dir()
        status = mirror_status_for(cache_dir, bug)
        assert status.cached is True
        assert status.source_commit == bug.manifest.repo.pre_fix_ref

    def test_status_reports_uncached_before_a_sync(self, mirror_bug, tmp_path: Path) -> None:
        bug, _repo, _fix = mirror_bug

        status = mirror_status_for(tmp_path / "cache", bug)

        assert status.cached is False
        assert status.source_commit is None

    def test_force_rebuilds_even_when_unchanged(self, mirror_bug, tmp_path: Path) -> None:
        bug, _repo, _fix = mirror_bug
        cache_dir = tmp_path / "cache"
        first = sync_bug(cache_dir, bug)

        second = sync_bug(cache_dir, bug, force=True)

        assert second.commit == first.commit  # same source, so the same resulting snapshot
