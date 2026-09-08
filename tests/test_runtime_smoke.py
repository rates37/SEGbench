"""The smoke routine, against an in-memory backend.

The point of these tests is not to prove that LXD works — ``segbench runtime smoke`` against real
LXD is the acceptance check for that. It is to prove that the smoke routine actually *checks* what
it claims to, and that it cleans up on every path. A smoke test that passes when the file did not
round-trip, or that leaks a container when a step fails, is worse than none.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from segbench.runtime.base import (
    BackgroundProcess,
    ContainerSpec,
    ExecResult,
    Handle,
    RuntimeFailure,
)
from segbench.runtime.smoke import TOOLS, run_smoke


class FakeRuntime:
    """An in-memory backend: a dict for the filesystem, a list for running processes."""

    name = "fake"

    def __init__(self, *, tools: tuple[str, ...] = TOOLS) -> None:
        self.tools = tools
        self.files: dict[str, str] = {}
        self.live: set[str] = set()
        self.running: list[BackgroundProcess] = []
        self.killed: list[str] = []
        self.destroyed: list[str] = []

    def preflight(self) -> None:
        pass

    def image_digest(self, image: str) -> str:
        return "d" * 64

    def create(self, spec: ContainerSpec) -> Handle:
        self.live.add(spec.name)
        return Handle(name=spec.name, backend=self.name, image=spec.image, image_digest="d" * 64)

    def destroy(self, handle: Handle) -> None:
        self.live.discard(handle.name)
        self.destroyed.append(handle.name)

    def push(self, handle: Handle, local: Path, remote: str, *, mode: str | None = None) -> None:
        self.files[remote] = Path(local).read_text(encoding="utf-8")

    def pull(self, handle: Handle, remote: str, local: Path) -> None:
        Path(local).write_text(self.files[remote], encoding="utf-8")

    def exec(self, handle: Handle, argv: list[str], **kwargs: object) -> ExecResult:
        stdout = ""
        joined = " ".join(argv)
        if "command -v" in joined:
            stdout = "\n".join(self.tools) + "\n"
        elif kwargs.get("user"):
            stdout = f"{kwargs['user']}\n"
        elif "id -un" in joined:
            stdout = "root\nPython 3.12.3\ngit version 2.43.0\n"
        elif ">>" in joined:
            self.files["/workspace/probe.txt"] += "round trip\n"
        elif "pgrep" in joined:
            stdout = f"{len(self.running)}\n"
        result = ExecResult(
            argv=tuple(argv), returncode=0, stdout=stdout, stderr="", duration_s=0.0
        )
        return result.check() if kwargs.get("check") else result

    def exec_background(self, handle: Handle, argv: list[str], **kwargs: object) -> Background:
        process = BackgroundProcess(
            handle=handle, argv=tuple(argv), token="t", started_at=time.monotonic()
        )
        self.running.append(process)
        return process

    def wait_or_kill(self, process: BackgroundProcess, timeout: float) -> ExecResult:
        self.running.remove(process)
        self.killed.append(process.token)
        return ExecResult(
            argv=process.argv,
            returncode=-1,
            stdout="",
            stderr="",
            duration_s=timeout,
            timed_out=True,
        )


Background = BackgroundProcess


def _names(prefix: str) -> str:
    return f"{prefix}-test"


def test_smoke_passes_against_a_working_backend() -> None:
    runtime = FakeRuntime()

    result = run_smoke(runtime, "segbench-base", name_factory=_names)

    assert result.ok, result.error
    assert [step.name for step in result.steps][-1] == "destroy"
    assert runtime.live == set()


def test_smoke_reports_the_image_digest() -> None:
    """It goes onto the run record, so the smoke command is where a wrong one gets noticed."""
    result = run_smoke(FakeRuntime(), "segbench-base", name_factory=_names)

    assert result.image_digest == "d" * 64


def test_smoke_times_every_step() -> None:
    result = run_smoke(FakeRuntime(), "segbench-base", name_factory=_names)

    assert {"create", "exec", "push", "pull", "destroy"} <= {s.name for s in result.steps}
    assert result.total_s >= 0


def test_smoke_fails_when_the_file_does_not_round_trip() -> None:
    runtime = FakeRuntime()
    original_pull = runtime.pull

    def corrupt(handle: Handle, remote: str, local: Path) -> None:
        original_pull(handle, remote, local)
        Path(local).write_text("corrupted", encoding="utf-8")

    runtime.pull = corrupt  # type: ignore[method-assign]

    result = run_smoke(runtime, "segbench-base", name_factory=_names)

    assert not result.ok
    assert "round-trip" in (result.error or "")


def test_smoke_fails_when_a_background_process_survives_the_kill() -> None:
    """Killing only the client and leaving the agent running would blow the wall clock."""
    runtime = FakeRuntime()

    def leak(process: BackgroundProcess, timeout: float) -> ExecResult:
        return ExecResult(
            argv=process.argv, returncode=-1, stdout="", stderr="", duration_s=0.0, timed_out=True
        )

    runtime.wait_or_kill = leak  # type: ignore[method-assign]

    result = run_smoke(runtime, "segbench-base", name_factory=_names)

    assert not result.ok
    assert "survived the kill" in (result.error or "")


def test_smoke_fails_when_the_deadline_is_not_enforced() -> None:
    runtime = FakeRuntime()

    def finish(process: BackgroundProcess, timeout: float) -> ExecResult:
        runtime.running.remove(process)
        return ExecResult(argv=process.argv, returncode=0, stdout="", stderr="", duration_s=0.0)

    runtime.wait_or_kill = finish  # type: ignore[method-assign]

    result = run_smoke(runtime, "segbench-base", name_factory=_names)

    assert not result.ok
    assert "deadline" in (result.error or "")


def test_smoke_fails_when_the_image_is_missing_tooling() -> None:
    """`command -v a b c` reports only the first argument in dash, so this is easy to get wrong."""
    runtime = FakeRuntime(tools=("git", "python3"))

    result = run_smoke(runtime, "segbench-base", name_factory=_names)

    assert not result.ok
    assert "missing expected tooling" in (result.error or "")
    for tool in ("rg", "fd", "jq", "opencode"):
        assert tool in (result.error or "")


def test_smoke_runs_a_step_as_the_agent_user() -> None:
    """Every real run execs as the unprivileged agent user, so the smoke must too."""
    result = run_smoke(FakeRuntime(), "segbench-base", name_factory=_names, agent_user="agent")

    assert result.ok, result.error
    step = next(s for s in result.steps if s.name == "exec (agent user)")
    assert step.detail == "agent can write /workspace"


def test_smoke_fails_when_the_exec_lands_as_the_wrong_user() -> None:
    """A backend that quietly ignores `user=` would otherwise run every agent as root."""

    class IgnoresUser(FakeRuntime):
        def exec(self, handle: Handle, argv: list[str], **kwargs: object) -> ExecResult:
            return super().exec(handle, argv, **{**kwargs, "user": None})

    result = run_smoke(IgnoresUser(), "segbench-base", name_factory=_names, agent_user="agent")

    assert not result.ok
    assert "expected to be running as 'agent'" in (result.error or "")


def test_smoke_destroys_the_container_even_when_a_step_raises() -> None:
    runtime = FakeRuntime()

    def explode(*args: object, **kwargs: object) -> ExecResult:
        raise RuntimeFailure("exec is broken")

    runtime.exec = explode  # type: ignore[method-assign]

    result = run_smoke(runtime, "segbench-base", name_factory=_names)

    assert not result.ok
    assert runtime.destroyed == [result.container]
    assert runtime.live == set()


@pytest.mark.skipif(shutil.which("lxc") is None, reason="LXD is not installed")
def test_lxd_preflight_reports_clearly() -> None:
    """With LXD installed, preflight either passes or explains itself. It never hangs or lies."""
    from segbench.runtime.base import BackendUnavailable
    from segbench.runtime.lxd import LXDRuntime

    try:
        LXDRuntime().preflight()
    except BackendUnavailable as exc:
        assert "lxd" in str(exc).lower()
