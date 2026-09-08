"""E1 seeding: materialising a truncated repo as a working tree with an inert `.git`, and the
verification that must abort a run rather than let it degrade silently (plan.md section 5, E1).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from segbench.netpol.gitmirror import (
    MirrorError,
    build_e1_worktree,
    ensure_target_mirror,
    seed_e1,
    verify_e1_worktree_in_container,
    verify_e1_worktree_on_host,
)
from segbench.runtime.base import BackgroundProcess, ContainerSpec, ExecResult, Handle


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check, capture_output=True, text=True
    )


@pytest.fixture
def truncated_repo(mirror_bug, tmp_path: Path) -> Path:
    bug, _repo, _fix = mirror_bug
    return ensure_target_mirror(tmp_path / "cache", bug)


class TestBuildAndVerifyOnHost:
    def test_seeded_tree_has_one_commit_no_remote_no_shallow_marker(
        self, truncated_repo: Path, tmp_path: Path
    ) -> None:
        dest = tmp_path / "workdir"

        commit = build_e1_worktree(truncated_repo, dest)

        assert _git(dest, "rev-parse", "HEAD").stdout.strip() == commit
        verify_e1_worktree_on_host(dest)  # must not raise

    def test_verification_catches_a_lingering_remote(
        self, truncated_repo: Path, tmp_path: Path
    ) -> None:
        dest = tmp_path / "workdir"
        build_e1_worktree(truncated_repo, dest)
        _git(dest, "remote", "add", "origin", str(truncated_repo))

        with pytest.raises(MirrorError, match="no remotes"):
            verify_e1_worktree_on_host(dest)

    def test_verification_catches_extra_history(self, truncated_repo: Path, tmp_path: Path) -> None:
        dest = tmp_path / "workdir"
        build_e1_worktree(truncated_repo, dest)
        (dest / "extra.txt").write_text("more\n")
        _git(dest, "add", "-A")
        _git(dest, "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", "extra")

        with pytest.raises(MirrorError, match="exactly one commit"):
            verify_e1_worktree_on_host(dest)

    def test_verification_catches_a_shallow_marker(
        self, truncated_repo: Path, tmp_path: Path
    ) -> None:
        dest = tmp_path / "workdir"
        commit = build_e1_worktree(truncated_repo, dest)
        # A real commit hash, so `git log`/`git remote -v` still succeed normally and the explicit
        # shallow-file check below is what actually catches this, not git refusing a bogus one.
        (dest / ".git" / "shallow").write_text(commit + "\n")

        with pytest.raises(MirrorError, match="shallow marker"):
            verify_e1_worktree_on_host(dest)

    def test_rebuilding_replaces_an_existing_destination(
        self, truncated_repo: Path, tmp_path: Path
    ) -> None:
        dest = tmp_path / "workdir"
        dest.mkdir()
        (dest / "stale.txt").write_text("leftover from a previous run\n")

        build_e1_worktree(truncated_repo, dest)

        assert not (dest / "stale.txt").exists()
        verify_e1_worktree_on_host(dest)


class _FakeRuntime:
    """A minimal in-memory backend: enough of the `Runtime` protocol for `seed_e1` and the
    in-container verification to exercise real git commands against a plain host directory
    standing in for `/workspace/repo` -- no container needed to test this logic.

    "Remote" paths are mapped under a scratch root rather than treated as literal host paths --
    `/workspace` is not writable by an ordinary user, same as inside a real container it would not
    be the *host's* `/workspace`.
    """

    name = "fake"

    def __init__(self, root: Path) -> None:
        self._root = root
        self.pushed: dict[str, Path] = {}

    def _map(self, remote: str) -> Path:
        return self.pushed.setdefault(remote, self._root / remote.lstrip("/"))

    def preflight(self) -> None:
        pass

    def image_digest(self, image: str) -> str:
        return "d" * 64

    def create(self, spec: ContainerSpec) -> Handle:
        return Handle(name=spec.name, backend=self.name, image=spec.image, image_digest="d" * 64)

    def destroy(self, handle: Handle) -> None:
        pass

    def push(self, handle: Handle, local: Path, remote: str, *, mode: str | None = None) -> None:
        dest = self._map(remote)
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(local, dest)

    def pull(self, handle: Handle, remote: str, local: Path) -> None:
        shutil.copytree(self.pushed[remote], local)

    def exec(self, handle: Handle, argv, **kwargs: object) -> ExecResult:
        remote_path = self.pushed.get("/workspace/repo")
        real_argv = list(argv)
        if real_argv[:3] == ["git", "config", "--system"]:
            # A real container's git config lives in its own `/etc/gitconfig`; this fake has no
            # such thing, and there is nothing here for `seed_e1`'s safe.directory dance to fix
            # (files copied by `shutil.copytree` are already owned by whoever is running pytest).
            return ExecResult(argv=tuple(argv), returncode=0, stdout="", stderr="", duration_s=0.0)
        if real_argv and real_argv[0] == "git" and "-C" in real_argv:
            idx = real_argv.index("-C")
            if real_argv[idx + 1] == "/workspace/repo" and remote_path is not None:
                real_argv[idx + 1] = str(remote_path)
        elif real_argv[:2] == ["test", "-f"]:
            real_argv[-1] = real_argv[-1].replace("/workspace/repo", str(remote_path))
        result = subprocess.run(real_argv, capture_output=True, text=True)
        exec_result = ExecResult(
            argv=tuple(argv),
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_s=0.0,
        )
        return exec_result.check() if kwargs.get("check") else exec_result

    def exec_background(self, handle: Handle, argv, **kwargs: object) -> BackgroundProcess:
        raise NotImplementedError

    def wait_or_kill(self, process: BackgroundProcess, timeout: float) -> ExecResult:
        raise NotImplementedError


class TestSeedE1WithARuntime:
    def test_seed_e1_pushes_and_verifies(self, truncated_repo: Path, tmp_path: Path) -> None:
        runtime = _FakeRuntime(tmp_path / "container-root")
        handle = runtime.create(ContainerSpec(image="i", name="c"))
        host_workdir = tmp_path / "hostwork"

        commit = seed_e1(runtime, handle, truncated_repo, host_workdir=host_workdir)

        assert commit
        # The in-container verification ran for real (against the pushed copy) and did not raise.
        verify_e1_worktree_in_container(runtime, handle)

    def test_seed_e1_raises_when_verification_would_fail(
        self, truncated_repo: Path, tmp_path: Path
    ) -> None:
        """If the pushed copy is somehow not what was built (corruption, a backend that mangles
        the push), `seed_e1` must raise rather than let the run proceed on an unverified tree."""
        runtime = _FakeRuntime(tmp_path / "container-root")
        handle = runtime.create(ContainerSpec(image="i", name="c"))
        host_workdir = tmp_path / "hostwork"

        real_push = runtime.push

        def sabotaging_push(handle, local, remote, *, mode=None) -> None:
            real_push(handle, local, remote, mode=mode)
            pushed = runtime.pushed[remote]
            _git(pushed, "remote", "add", "origin", str(truncated_repo))

        runtime.push = sabotaging_push  # type: ignore[method-assign]

        with pytest.raises(MirrorError, match="no remotes"):
            seed_e1(runtime, handle, truncated_repo, host_workdir=host_workdir)
