"""The runtime protocol, name generation, and the cleanup registry."""

from __future__ import annotations

import subprocess
import time

import pytest

from segbench.runtime.base import (
    REGISTRY,
    UNPOLICED,
    CleanupRegistry,
    ContainerSpec,
    ExecFailed,
    ExecResult,
    Handle,
    NetworkPolicy,
    Runtime,
    RuntimeFailure,
)
from segbench.runtime.lxd import LXDRuntime, sanitise_name
from segbench.runtime.podman import PodmanRuntime


@pytest.mark.parametrize("backend", [LXDRuntime(), PodmanRuntime()])
def test_backends_implement_the_protocol(backend: object) -> None:
    assert isinstance(backend, Runtime)


def test_both_backends_expose_the_same_surface() -> None:
    """The orchestrator must not be able to tell them apart by their method set."""
    surface = {
        name
        for name in dir(Runtime)
        if not name.startswith("_") and callable(getattr(Runtime, name, None))
    }
    for backend in (LXDRuntime, PodmanRuntime):
        assert surface <= {name for name in dir(backend) if not name.startswith("_")}


class TestSanitiseName:
    def test_is_unique_across_calls(self) -> None:
        assert sanitise_name("lp-2048221-e1") != sanitise_name("lp-2048221-e1")

    def test_maps_illegal_characters(self) -> None:
        # Run ids contain both: ':' from `loo:error_trace` and '_' from channel ids.
        name = sanitise_name("lp-2048221/E1/loo:error_trace")
        assert name.startswith("lp-2048221-e1-loo-error-trace-")
        assert set(name) <= set("abcdefghijklmnopqrstuvwxyz0123456789-")

    def test_prefixes_a_leading_digit(self) -> None:
        assert sanitise_name("2048221").startswith("sb-2048221-")

    def test_stays_within_the_lxd_length_limit(self) -> None:
        assert len(sanitise_name("x" * 200)) <= 63

    def test_survives_an_entirely_illegal_prefix(self) -> None:
        assert sanitise_name("///").startswith("sb-")


class TestExecResult:
    def _result(self, **kwargs: object) -> ExecResult:
        base = {
            "argv": ("false",),
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "duration_s": 0.1,
        }
        return ExecResult(**{**base, **kwargs})  # type: ignore[arg-type]

    def test_check_passes_through_on_success(self) -> None:
        result = self._result()
        assert result.check() is result

    def test_check_raises_on_non_zero(self) -> None:
        with pytest.raises(ExecFailed, match="exited 3"):
            self._result(returncode=3).check()

    def test_check_raises_on_timeout_even_with_zero_returncode(self) -> None:
        with pytest.raises(ExecFailed, match="timed out"):
            self._result(timed_out=True).check()

    def test_stderr_is_surfaced_not_swallowed(self) -> None:
        with pytest.raises(ExecFailed, match="lxc: no such object"):
            self._result(returncode=1, stderr="lxc: no such object").check()


class TestContainerSpec:
    def test_spec_env_wins_over_policy_env(self) -> None:
        spec = ContainerSpec(
            image="segbench-base",
            name="c",
            network=NetworkPolicy(name="p", env={"https_proxy": "policy", "no_proxy": "keep"}),
            env={"https_proxy": "explicit"},
        )
        assert spec.merged_env() == {"https_proxy": "explicit", "no_proxy": "keep"}

    def test_defaults_are_the_conservative_ones(self) -> None:
        spec = ContainerSpec(image="i", name="c")
        assert spec.network is UNPOLICED
        assert spec.privileged is False
        assert spec.wait_for_network is False


class TestCleanupRegistry:
    def test_cleanup_destroys_everything_registered(self) -> None:
        registry = CleanupRegistry()
        destroyed: list[str] = []
        registry.register("a", lambda: destroyed.append("a"))
        registry.register("b", lambda: destroyed.append("b"))

        registry.cleanup()

        assert sorted(destroyed) == ["a", "b"]

    def test_unregistered_entries_are_left_alone(self) -> None:
        registry = CleanupRegistry()
        destroyed: list[str] = []
        registry.register("a", lambda: destroyed.append("a"))
        registry.unregister("a")

        registry.cleanup()

        assert destroyed == []

    def test_one_failure_does_not_block_the_others(self) -> None:
        """Cleanup runs from a signal handler; one bad container must not orphan the rest."""
        registry = CleanupRegistry()
        destroyed: list[str] = []

        def boom() -> None:
            raise RuntimeFailure("storage pool busy")

        registry.register("a", boom)
        registry.register("b", lambda: destroyed.append("b"))

        registry.cleanup()

        assert destroyed == ["b"]

    def test_cleanup_is_idempotent(self) -> None:
        registry = CleanupRegistry()
        destroyed: list[str] = []
        registry.register("a", lambda: destroyed.append("a"))

        registry.cleanup()
        registry.cleanup()

        assert destroyed == ["a"]


class TestLXDDestroy:
    """Ctrl-C during `create` is the orphan-producing case, so it gets its own tests."""

    BUSY = (
        'Error: Failed deleting instance "c" in project "default": Failed creating instance '
        'delete operation: Instance is busy running a "create" operation'
    )

    def _runtime(self, results: list[tuple[int, str]]) -> tuple[LXDRuntime, list[list[str]]]:
        runtime = LXDRuntime()
        calls: list[list[str]] = []

        def fake(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(list(args))
            returncode, stderr = results[min(len(calls) - 1, len(results) - 1)]
            return subprocess.CompletedProcess(list(args), returncode, "", stderr)

        runtime.run_lxc = fake  # type: ignore[method-assign]
        return runtime, calls

    def _handle(self) -> Handle:
        return Handle(name="c", backend="lxd", image="i", image_digest="d")

    def test_retries_while_the_instance_is_busy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(time, "sleep", lambda _: None)
        runtime, calls = self._runtime([(1, self.BUSY), (1, self.BUSY), (0, "")])

        runtime.destroy(self._handle())

        assert len(calls) == 3

    def test_gives_up_after_the_busy_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(time, "sleep", lambda _: None)
        runtime, _ = self._runtime([(1, self.BUSY)])

        with pytest.raises(RuntimeFailure, match="failed to delete"):
            runtime.destroy(self._handle(), busy_timeout_s=0.0)

    def test_an_absent_container_is_not_an_error(self) -> None:
        runtime, calls = self._runtime([(1, "Error: Instance not found")])

        runtime.destroy(self._handle())

        assert len(calls) == 1

    def test_a_real_failure_is_not_retried(self) -> None:
        runtime, calls = self._runtime([(1, "Error: storage pool is gone")])

        with pytest.raises(RuntimeFailure, match="storage pool is gone"):
            runtime.destroy(self._handle())

        assert len(calls) == 1

    def test_destroy_unregisters_even_when_it_fails(self) -> None:
        """Otherwise atexit retries a delete that will never work, on every campaign exit."""
        runtime, _ = self._runtime([(1, "Error: storage pool is gone")])
        handle = self._handle()
        REGISTRY.register("lxd:c", lambda: None)

        with pytest.raises(RuntimeFailure):
            runtime.destroy(handle)

        assert "lxd:c" not in REGISTRY._entries


def test_runtime_failure_reports_the_command_and_stderr() -> None:
    """Debugging at 11pm means the exception must contain a pasteable command line."""
    exc = RuntimeFailure("nope", argv=["lxc", "launch", "x"], stderr="Error: not found")

    assert "lxc launch x" in str(exc)
    assert "Error: not found" in str(exc)
