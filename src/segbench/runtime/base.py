"""The backend-agnostic container runtime protocol.

Everything in this module is deliberately free of LXD, podman and container-versus-VM concepts.
The orchestrator talks to a :class:`Runtime`; whether that is an LXD system container, an LXD
virtual machine (plan.md section 9 anticipates one for kernel bugs) or a podman container is a
configuration decision it never sees.

The protocol is small on purpose:

``create``/``destroy``
    lifecycle, with :class:`Handle` as the opaque token joining the two.
``push``/``pull``
    move files in and out — how the channel files, the seeded repository and ``answer.json``
    cross the boundary.
``exec``
    run a command to completion.
``exec_background`` / ``wait_or_kill``
    run the agent, which needs a wall clock and a guaranteed kill (plan.md section 2: timeout is
    a normal outcome, not an error).
``image_digest``
    stamp the image onto the run record (CLAUDE.md invariant 4).

Two design notes worth keeping:

*No streaming API.* The agent's output is captured to files and read afterwards. A streaming
interface would have to be implemented identically by every backend, and nothing upstream wants
partial output while a run is in flight.

*No network configuration.* :class:`NetworkPolicy` is an opaque-ish handle produced by the
``netpol`` layer in phase 3 and applied verbatim by the backend. The runtime layer never decides
what a container may reach.
"""

from __future__ import annotations

import atexit
import contextlib
import signal
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import FrameType
from typing import Any, Protocol, runtime_checkable

from segbench.logging import get_logger

log = get_logger(__name__)


class RuntimeError_(Exception):
    """Base class for runtime failures.

    Named with a trailing underscore because shadowing the builtin ``RuntimeError`` in a module
    that also raises it would be a genuinely bad time at 11pm. Exported as
    :data:`RuntimeFailure`, which is the name to use.
    """

    def __init__(
        self, message: str, *, argv: Sequence[str] | None = None, stderr: str = ""
    ) -> None:
        self.argv = list(argv or [])
        self.stderr = stderr
        detail = ""
        if self.argv:
            detail += f"\n  command: {' '.join(self.argv)}"
        if stderr.strip():
            detail += f"\n  stderr: {stderr.strip()}"
        super().__init__(message + detail)


#: Preferred name. Backends raise this (or a subclass) for anything they cannot do.
RuntimeFailure = RuntimeError_


class BackendUnavailable(RuntimeFailure):
    """The backend's CLI is missing, or the daemon is not reachable."""


class UnsupportedOperation(RuntimeFailure):
    """The backend cannot do this at all. Raised loudly rather than degraded silently."""


class ContainerNotReady(RuntimeFailure):
    """The container did not become usable within the readiness timeout."""


class ExecFailed(RuntimeFailure):
    """A command run inside the container exited non-zero and the caller asked for a check."""


@dataclass(frozen=True)
class ResourceLimits:
    """Per-container resource caps.

    All fields are optional; ``None`` means "whatever the backend defaults to". Values are
    expressed in the backend-neutral forms that both LXD and podman accept (``"2GiB"``,
    ``"4"``), and each backend translates them into its own configuration keys.
    """

    cpu: int | None = None
    memory: str | None = None
    disk: str | None = None
    processes: int | None = None


@dataclass(frozen=True)
class NetworkPolicy:
    """What the container is allowed to reach, as decided by the ``netpol`` layer.

    Phase 2 only carries this through; phase 3 (plan.md section 5.1) fills it in and adds the
    proxy, the ACLs and the netlog. The fields are the minimum a backend needs to attach a
    container to a policed network:

    ``name``
        an identifier for logging and for the run record.
    ``network``
        the backend network to attach the primary NIC to. ``None`` means the backend's default
        bridge, which is unpoliced and therefore only appropriate for image builds and smoke
        tests.
    ``acls``
        backend-level ACL names to apply to the NIC, if the backend supports them.
    ``env``
        environment variables the policy needs in the container, e.g. ``https_proxy``. Merged
        into :attr:`ContainerSpec.env`, with the spec winning on a conflict so a caller can
        always see what it set.
    """

    name: str = "unpoliced"
    network: str | None = None
    acls: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)


#: The default for image builds and the smoke test: the backend's own bridge, full egress.
#: Never appropriate for a benchmark run — phase 3 supplies the real thing.
UNPOLICED = NetworkPolicy(name="unpoliced")


@dataclass(frozen=True)
class ContainerSpec:
    """Everything needed to create one container.

    ``privileged`` exists because LXD offers it and kernel-adjacent tooling may eventually need
    it, but nothing in the harness sets it today. Anything that turns it on must say why at the
    call site: an unprivileged container that escapes is a nuisance, a privileged one that
    escapes is the host.
    """

    image: str
    name: str
    network: NetworkPolicy = UNPOLICED
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    env: Mapping[str, str] = field(default_factory=dict)
    #: Poll for a routable IPv4 address before returning from ``create``. Only image builds and
    #: E2 need this; E0 and E1 have no egress at all and would time out waiting for one.
    wait_for_network: bool = False
    #: Seconds to wait for the container to become usable.
    ready_timeout_s: float = 90.0
    privileged: bool = False

    def merged_env(self) -> dict[str, str]:
        """Policy environment first, spec environment second, so the caller's values win."""
        return {**dict(self.network.env), **dict(self.env)}


@dataclass(frozen=True)
class Handle:
    """An opaque token for one live container.

    Callers must treat :attr:`extra` as private to the backend that produced it. It exists so a
    backend can carry state (a resolved uid, a network name it created) without a parallel
    lookup table.
    """

    name: str
    backend: str
    image: str
    image_digest: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecResult:
    """The outcome of one command run inside a container."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    #: True when the command was killed by :meth:`Runtime.wait_or_kill` hitting its deadline.
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def check(self) -> ExecResult:
        """Return self, or raise :class:`ExecFailed` describing the failure."""
        if self.ok:
            return self
        what = "timed out" if self.timed_out else f"exited {self.returncode}"
        raise ExecFailed(f"command {what} in container", argv=self.argv, stderr=self.stderr)


@dataclass
class BackgroundProcess:
    """A command still running inside a container.

    ``token`` is a marker the backend injects into the command's environment so it can find and
    kill the *in-container* process, not merely the client that started it. Detaching the client
    and leaving the agent running would silently blow the wall-clock cap.
    """

    handle: Handle
    argv: tuple[str, ...]
    token: str
    started_at: float
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Runtime(Protocol):
    """The operations the orchestrator needs from a container backend."""

    #: Short backend identifier, stamped onto handles and run records.
    name: str

    def preflight(self) -> None:
        """Raise :class:`BackendUnavailable` unless the backend is usable right now."""
        ...

    def image_digest(self, image: str) -> str:
        """Return the content digest of ``image``, for the run record."""
        ...

    def create(self, spec: ContainerSpec) -> Handle:
        """Create and start a container, returning once it is ready to exec in."""
        ...

    def destroy(self, handle: Handle) -> None:
        """Remove the container. Never raises for an already-absent container."""
        ...

    def push(self, handle: Handle, local: Any, remote: str, *, mode: str | None = None) -> None:
        """Copy a host path into the container, creating parent directories."""
        ...

    def pull(self, handle: Handle, remote: str, local: Any) -> None:
        """Copy a container path out to the host."""
        ...

    def exec(
        self,
        handle: Handle,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        user: str | None = None,
        cwd: str | None = None,
        check: bool = False,
    ) -> ExecResult:
        """Run a command to completion inside the container."""
        ...

    def exec_background(
        self,
        handle: Handle,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
    ) -> BackgroundProcess:
        """Start a command and return without waiting for it."""
        ...

    def wait_or_kill(self, process: BackgroundProcess, timeout: float) -> ExecResult:
        """Wait up to ``timeout`` seconds, then kill the process inside the container.

        Returns an :class:`ExecResult` with ``timed_out=True`` on the kill path rather than
        raising: a run that hits the wall clock is a normal outcome (plan.md section 2).
        """
        ...


class CleanupRegistry:
    """Force-destroys tracked containers on interpreter exit and on SIGINT/SIGTERM.

    A benchmark campaign that leaves orphaned containers behind will eventually fill the host's
    storage pool and the failure will surface days later as an unrelated build error. Every
    backend registers each container it creates here and unregisters it on a clean destroy.

    Signal handlers are installed lazily on first registration, chain to whatever was installed
    before, and re-raise so ``Ctrl-C`` still looks like ``Ctrl-C`` to the caller.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, Callable[[], None]] = {}
        self._installed = False

    def register(self, key: str, destroy: Callable[[], None]) -> None:
        with self._lock:
            self._entries[key] = destroy
            self._install()

    def unregister(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def _install(self) -> None:
        """Install exit and signal hooks once. Caller holds the lock."""
        if self._installed:
            return
        self._installed = True
        atexit.register(self.cleanup)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                previous = signal.getsignal(sig)
            except ValueError:  # pragma: no cover - not on the main thread
                continue

            def handler(
                signum: int, frame: FrameType | None, _previous: Any = previous, _sig: Any = sig
            ) -> None:
                log.warning("signal received, destroying containers", extra={"signal": signum})
                self.cleanup()
                if callable(_previous) and _previous not in (
                    signal.SIG_IGN,
                    signal.SIG_DFL,
                ):
                    _previous(signum, frame)
                elif _sig == signal.SIGINT:
                    raise KeyboardInterrupt
                else:
                    signal.signal(_sig, signal.SIG_DFL)
                    signal.raise_signal(_sig)

            with contextlib.suppress(ValueError):  # not on the main thread
                signal.signal(sig, handler)

    def cleanup(self) -> None:
        """Destroy everything still registered, swallowing individual failures.

        Swallowing is right here and nowhere else: this runs from a signal handler or from
        ``atexit``, where raising would mask the original reason for the exit and would stop the
        remaining containers from being cleaned up at all. Each failure is logged.

        The destroys run on a **worker thread**, not inline. ``Ctrl-C`` at a terminal delivers
        SIGINT to the whole process group, and the harness reliably sees two: the second one
        arrives while the first handler is still deleting, raises ``KeyboardInterrupt`` in the
        middle of the delete, and leaves the container running — which is exactly the orphan this
        registry exists to prevent. Python only delivers signals to the main thread, so work on a
        worker cannot be interrupted this way, and because the thread is non-daemon the
        interpreter will not exit until it has finished even if the join below is abandoned.
        """
        with self._lock:
            entries = list(self._entries.items())
            self._entries.clear()
        if not entries:
            return

        def work() -> None:
            for key, destroy in entries:
                try:
                    destroy()
                except Exception as exc:  # see docstring
                    log.error("cleanup failed", extra={"container": key, "error": str(exc)})

        if threading.current_thread() is not threading.main_thread():
            work()
            return

        worker = threading.Thread(target=work, name="segbench-cleanup", daemon=False)
        worker.start()
        while worker.is_alive():
            # A further Ctrl-C interrupts this join, never the deletes themselves.
            with contextlib.suppress(KeyboardInterrupt):
                worker.join()


#: Process-wide registry, shared by every backend instance.
REGISTRY = CleanupRegistry()
