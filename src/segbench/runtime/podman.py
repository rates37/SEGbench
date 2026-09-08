"""The podman fallback backend.

Feature parity with :mod:`segbench.runtime.lxd` is explicitly not a goal (CLAUDE.md). This exists
so the harness can be developed and smoke-tested on a machine without LXD, and so the runtime
protocol has a second implementation keeping it honest about leaking backend concepts.

What is missing, and raises :class:`UnsupportedOperation` rather than degrading quietly:

* **Image building.** ``segbench image build`` produces an LXD image from an LXD definition.
  Producing an OCI image from the same definition is a separate piece of work; until then,
  point the podman backend at an image you built yourself.
* **Network ACLs.** :attr:`NetworkPolicy.acls` are LXD network ACL names. Phase 3's enforcement
  story (plan.md section 5.1) is written against LXD, and silently ignoring an ACL would mean a
  run that believes it was policed and was not — the exact failure the invariants exist to
  prevent.

A run record produced on this backend is therefore *not* interchangeable with one produced on
LXD, which is why ``backend`` is stamped on every handle and belongs on the run record too.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from segbench.logging import get_logger
from segbench.runtime.base import (
    REGISTRY,
    BackendUnavailable,
    BackgroundProcess,
    ContainerNotReady,
    ContainerSpec,
    ExecResult,
    Handle,
    RuntimeFailure,
    UnsupportedOperation,
)

log = get_logger(__name__)

TOKEN_ENV = "SEGBENCH_PROC_TOKEN"

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")


class PodmanTimeout(RuntimeFailure):
    """A ``podman`` invocation exceeded its own timeout."""


class PodmanRuntime:
    """Container backend over ``podman``. Implements :class:`segbench.runtime.base.Runtime`."""

    name = "podman"

    def __init__(self, *, podman: str = "podman") -> None:
        self._podman = podman

    def _run(
        self, *args: str, timeout: float | None = 300.0, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        argv = [self._podman, *args]
        log.debug("podman", extra={"argv": argv})
        try:
            proc = subprocess.run(  # argv is constructed, never shell-interpolated
                argv, capture_output=True, text=True, timeout=timeout
            )
        except FileNotFoundError as exc:
            raise BackendUnavailable(f"{self._podman!r} not found on PATH", argv=argv) from exc
        except subprocess.TimeoutExpired as exc:
            raise PodmanTimeout(
                f"podman command timed out after {timeout}s",
                argv=argv,
                stderr=str(exc.stderr or ""),
            ) from exc
        if check and proc.returncode != 0:
            raise RuntimeFailure(
                f"podman command failed (exit {proc.returncode})", argv=argv, stderr=proc.stderr
            )
        return proc

    def preflight(self) -> None:
        if shutil.which(self._podman) is None:
            raise BackendUnavailable(f"{self._podman!r} not found on PATH")
        proc = self._run("info", "--format", "{{.Host.Arch}}", timeout=60.0, check=False)
        if proc.returncode != 0:
            raise BackendUnavailable("podman is installed but not usable", stderr=proc.stderr)

    def image_digest(self, image: str) -> str:
        proc = self._run("image", "inspect", "--format", "{{.Id}}", image, check=False)
        if proc.returncode != 0:
            raise RuntimeFailure(f"no such image: {image!r}", stderr=proc.stderr)
        return proc.stdout.strip()

    def create(self, spec: ContainerSpec) -> Handle:
        if not _NAME_RE.match(spec.name):
            raise RuntimeFailure(f"illegal podman container name {spec.name!r}")
        if spec.network.acls:
            raise UnsupportedOperation(
                "network ACLs are an LXD concept and the podman backend cannot enforce them; "
                "run policed benchmark runs on the LXD backend (see this module's docstring)."
            )
        digest = self.image_digest(spec.image)

        argv = ["run", "--detach", "--name", spec.name, "--init"]
        if spec.privileged:
            argv.append("--privileged")
        if spec.limits.cpu is not None:
            argv += ["--cpus", str(spec.limits.cpu)]
        if spec.limits.memory is not None:
            argv += ["--memory", spec.limits.memory.replace("iB", "")]
        if spec.limits.processes is not None:
            argv += ["--pids-limit", str(spec.limits.processes)]
        if spec.limits.disk is not None:
            raise UnsupportedOperation(
                "per-container disk quotas are not implemented on the podman backend"
            )
        argv += ["--network", spec.network.network or "bridge"]
        for key, value in spec.merged_env().items():
            argv += ["--env", f"{key}={value}"]
        # Podman containers exit as soon as their entrypoint does; the harness execs into a
        # long-lived container, so hold it open explicitly.
        argv += [spec.image, "sleep", "infinity"]

        handle = Handle(
            name=spec.name, backend=self.name, image=spec.image, image_digest=digest, extra={}
        )
        REGISTRY.register(f"podman:{spec.name}", lambda: self.destroy(handle))
        try:
            self._run(*argv, timeout=300.0)
            self._wait_ready(spec)
        except Exception:
            self.destroy(handle)
            raise
        return handle

    def _wait_ready(self, spec: ContainerSpec) -> None:
        deadline = time.monotonic() + spec.ready_timeout_s
        last = ""
        while time.monotonic() < deadline:
            proc = self._run("exec", spec.name, "true", timeout=30.0, check=False)
            if proc.returncode == 0:
                return
            last = proc.stderr
            time.sleep(0.25)
        raise ContainerNotReady(
            f"container {spec.name!r} did not accept exec within {spec.ready_timeout_s}s",
            stderr=last,
        )

    def destroy(self, handle: Handle) -> None:
        proc = self._run("rm", "--force", handle.name, timeout=180.0, check=False)
        REGISTRY.unregister(f"podman:{handle.name}")
        if proc.returncode != 0 and "no such container" not in proc.stderr.lower():
            raise RuntimeFailure(f"failed to remove {handle.name!r}", stderr=proc.stderr)

    def push(
        self, handle: Handle, local: str | os.PathLike[str], remote: str, *, mode: str | None = None
    ) -> None:
        source = Path(local)
        if not source.exists():
            raise RuntimeFailure(f"nothing to push: {source} does not exist")
        parent = os.path.dirname(remote.rstrip("/")) or "/"
        self._run("exec", handle.name, "mkdir", "-p", parent)
        self._run("cp", str(source), f"{handle.name}:{remote}", timeout=600.0)
        if mode:
            self._run("exec", handle.name, "chmod", mode, remote)

    def pull(self, handle: Handle, remote: str, local: str | os.PathLike[str]) -> None:
        destination = Path(local)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run("cp", f"{handle.name}:{remote}", str(destination), timeout=600.0)

    def _exec_argv(
        self,
        handle: Handle,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None,
        user: str | None,
        cwd: str | None,
    ) -> list[str]:
        out = ["exec"]
        for key, value in (env or {}).items():
            out += ["--env", f"{key}={value}"]
        if user is not None:
            out += ["--user", user]
        if cwd is not None:
            out += ["--workdir", cwd]
        return [*out, handle.name, *argv]

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
        full = self._exec_argv(handle, argv, env=env, user=user, cwd=cwd)
        started = time.monotonic()
        timed_out = False
        try:
            proc = self._run(*full, timeout=timeout, check=False)
            returncode, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except PodmanTimeout as exc:
            timed_out, returncode, stdout, stderr = True, -1, "", exc.stderr
        result = ExecResult(
            argv=tuple(argv),
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            duration_s=time.monotonic() - started,
            timed_out=timed_out,
        )
        return result.check() if check else result

    def exec_background(
        self,
        handle: Handle,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
    ) -> BackgroundProcess:
        token = secrets.token_hex(8)
        env = {**(env or {}), TOKEN_ENV: token}
        full = [self._podman, *self._exec_argv(handle, argv, env=env, user=user, cwd=cwd)]
        log.debug("podman exec (background)", extra={"argv": full, "token": token})
        popen = subprocess.Popen(  # argv is constructed, never shell-interpolated
            full,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return BackgroundProcess(
            handle=handle,
            argv=tuple(argv),
            token=token,
            started_at=time.monotonic(),
            extra={"popen": popen},
        )

    def wait_or_kill(self, process: BackgroundProcess, timeout: float) -> ExecResult:
        popen: subprocess.Popen[str] = process.extra["popen"]
        timed_out = False
        try:
            stdout, stderr = popen.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_token(process)
            popen.kill()
            stdout, stderr = popen.communicate()
        return ExecResult(
            argv=process.argv,
            returncode=popen.returncode if popen.returncode is not None else -1,
            stdout=stdout or "",
            stderr=stderr or "",
            duration_s=time.monotonic() - process.started_at,
            timed_out=timed_out,
        )

    def _kill_token(self, process: BackgroundProcess) -> None:
        """Kill the in-container process by the marker in its environment. See the LXD backend."""
        script = (
            f'for p in /proc/[0-9]*; do if tr "\\0" "\\n" < "$p/environ" 2>/dev/null | '
            f'grep -qx "{TOKEN_ENV}={process.token}"; then kill -9 "${{p#/proc/}}" 2>/dev/null; '
            f"fi; done"
        )
        self._run("exec", process.handle.name, "sh", "-c", script, timeout=60.0, check=False)
