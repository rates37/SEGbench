"""The LXD backend, driven through the ``lxc`` CLI.

Why the CLI and not ``pylxd`` (CLAUDE.md, technical decisions): the CLI's behaviour is stable
across LXD versions, and every command this module runs can be pasted into a shell verbatim when
something goes wrong at 11pm. Accordingly, every command line is logged at debug before it runs,
and ``lxc``'s stderr is carried into the exception rather than swallowed.

Things that are ugly and therefore explicit here:

* **Readiness.** ``lxc launch`` returns as soon as the container is *started*, which is well
  before ``exec`` works and much before it has an address. :meth:`LXDRuntime.create` polls for
  both, separately, with the network poll opt-in.
* **Cleanup.** Every container is registered with the shared cleanup registry the moment it
  exists, so a failure mid-``create`` or a ``Ctrl-C`` mid-run still removes it.
* **Naming.** Container names are derived from a caller-supplied prefix plus a random suffix and
  validated against LXD's naming rules, so two concurrent runs of the same cell cannot collide.
* **Killing background processes.** Killing the ``lxc exec`` client does not reliably kill the
  process inside the container. :meth:`LXDRuntime.wait_or_kill` kills the client *and* then
  ``pkill``s the in-container process by a marker injected into its environment.
"""

from __future__ import annotations

import json
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
)

log = get_logger(__name__)

#: LXD instance names: letters, digits and hyphens, no leading digit, 63 characters max.
_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]{0,62}$")

#: Environment variable carrying the kill marker for background processes.
TOKEN_ENV = "SEGBENCH_PROC_TOKEN"


class LxcTimeout(RuntimeFailure):
    """An ``lxc`` invocation exceeded its own timeout.

    Distinct from :class:`RuntimeFailure` so :meth:`LXDRuntime.exec` can turn a timed-out command
    into an :class:`ExecResult` with ``timed_out=True`` without matching on message text.
    """


def sanitise_name(prefix: str, *, suffix_bytes: int = 4) -> str:
    """Build a unique, LXD-legal container name from ``prefix``.

    Run ids contain characters LXD rejects (``:`` in ``loo:error_trace``, ``_`` in bug slugs), so
    they are mapped to hyphens and the result is truncated to leave room for the random suffix.
    The suffix is what makes concurrent repeats of one cell safe.
    """
    cleaned = re.sub(r"[^a-zA-Z0-9-]+", "-", prefix).strip("-").lower() or "sb"
    if not cleaned[0].isalpha():
        cleaned = f"sb-{cleaned}"
    suffix = secrets.token_hex(suffix_bytes)
    name = f"{cleaned[: 62 - len(suffix)]}-{suffix}"
    if not _NAME_RE.match(name):  # pragma: no cover - defensive
        raise RuntimeFailure(f"could not build a legal LXD name from {prefix!r} (got {name!r})")
    return name


class LXDRuntime:
    """Container backend over ``lxc``. Implements :class:`segbench.runtime.base.Runtime`."""

    name = "lxd"

    def __init__(
        self,
        *,
        lxc: str = "lxc",
        project: str | None = None,
        default_source_remote: str = "ubuntu",
    ) -> None:
        self._lxc = lxc
        self._project = project
        self.default_source_remote = default_source_remote

    # ------------------------------------------------------------------ plumbing

    def _base_argv(self) -> list[str]:
        argv = [self._lxc]
        if self._project:
            argv += ["--project", self._project]
        return argv

    def run_lxc(
        self,
        *args: str,
        timeout: float | None = 300.0,
        check: bool = True,
        stdin: bytes | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run one ``lxc`` command, logging the exact command line first.

        Public because the image builder and the smoke command legitimately need to reach LXD for
        things that are not container lifecycle (``lxc image``, ``lxc publish``), and giving them
        this one audited entry point is better than each of them shelling out on its own.
        """
        argv = self._base_argv() + list(args)
        log.debug("lxc", extra={"argv": argv})
        started = time.monotonic()
        try:
            proc = subprocess.run(  # argv is constructed, never shell-interpolated
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                input=stdin.decode() if stdin is not None else None,
            )
        except FileNotFoundError as exc:
            raise BackendUnavailable(
                f"{self._lxc!r} not found on PATH; is LXD installed?", argv=argv
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise LxcTimeout(
                f"lxc command timed out after {timeout}s", argv=argv, stderr=str(exc.stderr or "")
            ) from exc
        log.debug(
            "lxc done",
            extra={
                "argv": argv,
                "returncode": proc.returncode,
                "duration_s": round(time.monotonic() - started, 3),
            },
        )
        if check and proc.returncode != 0:
            raise RuntimeFailure(
                f"lxc command failed (exit {proc.returncode})", argv=argv, stderr=proc.stderr
            )
        return proc

    def preflight(self) -> None:
        """Fail early and clearly if LXD is not usable by this user."""
        if shutil.which(self._lxc) is None:
            raise BackendUnavailable(
                f"{self._lxc!r} not found on PATH. Install LXD (`snap install lxd`) or set "
                f"runtime.backend = 'podman'."
            )
        proc = self.run_lxc("query", "/1.0", timeout=30.0, check=False)
        if proc.returncode != 0:
            raise BackendUnavailable(
                "the LXD daemon is not reachable. Run `lxd init` once, and make sure this user "
                "is in the `lxd` group (`newgrp lxd` after adding).",
                stderr=proc.stderr,
            )

    # ------------------------------------------------------------------ images

    def image_digest(self, image: str) -> str:
        """Return the image fingerprint for ``image``, which may be an alias or a fingerprint."""
        proc = self.run_lxc("image", "info", image, timeout=60.0, check=False)
        if proc.returncode != 0:
            raise RuntimeFailure(f"no such image: {image!r}", stderr=proc.stderr)
        for line in proc.stdout.splitlines():
            if line.lower().startswith("fingerprint:"):
                return line.split(":", 1)[1].strip()
        raise RuntimeFailure(f"could not parse a fingerprint out of `lxc image info {image}`")

    def image_exists(self, alias: str) -> bool:
        return self.run_lxc("image", "info", alias, timeout=60.0, check=False).returncode == 0

    # ------------------------------------------------------------------ lifecycle

    def create(self, spec: ContainerSpec) -> Handle:
        """Launch a container from ``spec`` and return once it can run commands.

        Registered for cleanup *before* the readiness wait, because a container that starts and
        then fails to become ready is exactly the one that gets orphaned.
        """
        if not _NAME_RE.match(spec.name):
            raise RuntimeFailure(
                f"illegal LXD container name {spec.name!r}; build it with sanitise_name()"
            )
        digest = self.image_digest(spec.image)

        argv = ["launch", spec.image, spec.name]
        if spec.privileged:
            # Nothing in the harness sets this today. If something ever does, the reason belongs
            # at that call site, not here.
            argv += ["-c", "security.privileged=true"]
        for key, value in _config_for_limits(spec).items():
            argv += ["-c", f"{key}={value}"]
        for key, value in spec.merged_env().items():
            # Instance-level environment, so it applies to every exec including ones the harness
            # did not start. Phase 3's proxy variables must not be skippable.
            argv += ["-c", f"environment.{key}={value}"]
        if spec.limits.disk is not None:
            # A disk cap is a root-device override, not a config key; it only takes effect on
            # storage drivers that support quotas (zfs, btrfs, lvm — not dir).
            argv += ["-d", f"root,size={spec.limits.disk}"]
        if spec.network.network:
            argv += [
                "--device",
                f"eth0,type=nic,nictype=bridged,parent={spec.network.network},name=eth0",
            ]

        handle = Handle(
            name=spec.name, backend=self.name, image=spec.image, image_digest=digest, extra={}
        )
        REGISTRY.register(f"lxd:{spec.name}", lambda: self.destroy(handle))
        log.info(
            "creating container",
            extra={
                "container": spec.name,
                "image": spec.image,
                "image_digest": digest,
                "network_policy": spec.network.name,
            },
        )
        try:
            self.run_lxc(*argv, timeout=300.0)
            for acl in spec.network.acls:
                self.run_lxc("config", "device", "set", spec.name, "eth0", "security.acls", acl)
            self._wait_ready(spec)
        except Exception:
            log.error("create failed, destroying container", extra={"container": spec.name})
            self.destroy(handle)
            raise
        return handle

    def _wait_ready(self, spec: ContainerSpec) -> None:
        """Poll until the container can exec, and optionally until it has an IPv4 address.

        ``lxc launch`` returns when the instance is started, which is several seconds before the
        exec endpoint works. Waiting on an address is separate and opt-in: E0 and E1 containers
        have no egress by design and would never get one.
        """
        deadline = time.monotonic() + spec.ready_timeout_s
        last = ""
        while time.monotonic() < deadline:
            proc = self.run_lxc("exec", spec.name, "-T", "--", "true", timeout=30.0, check=False)
            if proc.returncode == 0:
                break
            last = proc.stderr
            time.sleep(0.5)
        else:
            raise ContainerNotReady(
                f"container {spec.name!r} did not accept exec within {spec.ready_timeout_s}s",
                stderr=last,
            )

        if not spec.wait_for_network:
            return
        while time.monotonic() < deadline:
            if self._ipv4(spec.name):
                return
            time.sleep(0.5)
        raise ContainerNotReady(
            f"container {spec.name!r} had no IPv4 address within {spec.ready_timeout_s}s. "
            f"Check that the network {spec.network.network or '(default profile)'} hands out "
            f"leases."
        )

    def _ipv4(self, name: str) -> str | None:
        proc = self.run_lxc("list", name, "--format", "json", timeout=30.0, check=False)
        if proc.returncode != 0:
            return None
        try:
            instances = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        for instance in instances:
            if instance.get("name") != name:
                continue
            for iface in (instance.get("state") or {}).get("network", {}).values():
                for address in iface.get("addresses", []):
                    if address.get("family") == "inet" and address.get("scope") == "global":
                        return str(address.get("address"))
        return None

    def stop(self, handle: Handle, *, timeout: float = 120.0) -> None:
        """Stop a container without deleting it. Used by the image builder before publishing."""
        self.run_lxc("stop", handle.name, "--timeout", "60", timeout=timeout, check=False)

    def destroy(self, handle: Handle, *, busy_timeout_s: float = 120.0) -> None:
        """Force-delete the container. Absent containers are not an error.

        Retries while LXD reports the instance busy. This is the ``Ctrl-C``-during-``create``
        case: interrupting the harness kills the ``lxc launch`` *client*, but the server-side
        create operation keeps running, and LXD refuses to delete an instance with an operation in
        flight. Giving up on the first refusal is precisely how a campaign accumulates orphans.
        """
        deadline = time.monotonic() + busy_timeout_s
        while True:
            proc = self.run_lxc("delete", handle.name, "--force", timeout=180.0, check=False)
            if proc.returncode == 0 or "not found" in proc.stderr.lower():
                break
            if _is_busy(proc.stderr) and time.monotonic() < deadline:
                log.debug(
                    "container busy, retrying delete",
                    extra={"container": handle.name, "stderr": proc.stderr.strip()},
                )
                time.sleep(1.0)
                continue
            REGISTRY.unregister(f"lxd:{handle.name}")
            raise RuntimeFailure(f"failed to delete container {handle.name!r}", stderr=proc.stderr)

        REGISTRY.unregister(f"lxd:{handle.name}")
        log.info("container destroyed", extra={"container": handle.name})

    # ------------------------------------------------------------------ files

    def push(
        self, handle: Handle, local: str | os.PathLike[str], remote: str, *, mode: str | None = None
    ) -> None:
        """Copy a host file or directory into the container, creating parent directories."""
        source = Path(local)
        if not source.exists():
            raise RuntimeFailure(f"nothing to push: {source} does not exist")
        argv = ["file", "push", "--create-dirs"]
        if source.is_dir():
            argv.append("--recursive")
        if mode:
            argv += ["--mode", mode]
        argv += [str(source), f"{handle.name}{remote}"]
        self.run_lxc(*argv, timeout=600.0)

    def pull(self, handle: Handle, remote: str, local: str | os.PathLike[str]) -> None:
        """Copy a container path out to the host, creating the destination's parent directory."""
        destination = Path(local)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.run_lxc(
            "file", "pull", "--recursive", f"{handle.name}{remote}", str(destination), timeout=600.0
        )

    # ------------------------------------------------------------------ exec

    def _exec_argv(
        self,
        handle: Handle,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None,
        user: str | None,
        cwd: str | None,
    ) -> list[str]:
        out = ["exec", handle.name, "-T"]
        for key, value in (env or {}).items():
            out += ["--env", f"{key}={value}"]
        if user is not None:
            out += ["--user", str(self._resolve_uid(handle, user))]
        if cwd is not None:
            out += ["--cwd", cwd]
        return [*out, "--", *argv]

    def _resolve_uid(self, handle: Handle, user: str) -> int:
        """Map a username to a uid inside the container. ``lxc exec --user`` takes a number only.

        Cached on the handle: this costs an exec, and the agent invocation path resolves the same
        unprivileged user for every command it runs.
        """
        if user.isdigit():
            return int(user)
        cache: dict[str, int] = handle.extra.setdefault("uids", {})
        if user in cache:
            return cache[user]
        proc = self.run_lxc(
            "exec", handle.name, "-T", "--", "id", "-u", user, timeout=60.0, check=False
        )
        if proc.returncode != 0:
            raise RuntimeFailure(
                f"no such user {user!r} in container {handle.name!r}", stderr=proc.stderr
            )
        cache[user] = int(proc.stdout.strip())
        return cache[user]

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
        full = self._exec_argv(handle, argv, env=env, user=user, cwd=cwd)
        started = time.monotonic()
        timed_out = False
        try:
            proc = self.run_lxc(*full, timeout=timeout, check=False)
            returncode, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except LxcTimeout as exc:
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
        """Start a long-running command (the agent) without waiting for it.

        The command's environment carries a unique token so :meth:`wait_or_kill` can find the
        process inside the container. Output is buffered by the ``lxc`` client and collected on
        the wait, which is fine because nothing consumes agent output before it exits.
        """
        token = secrets.token_hex(8)
        env = {**(env or {}), TOKEN_ENV: token}
        full = self._base_argv() + self._exec_argv(handle, argv, env=env, user=user, cwd=cwd)
        log.debug("lxc exec (background)", extra={"argv": full, "token": token})
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
        """Wait for the process, killing it inside the container if the deadline passes.

        Two kills, in this order and both necessary: ``pkill`` on the token matches the process
        in the container's own pid namespace, and terminating the ``lxc`` client releases the
        pipes so ``communicate`` returns. Killing only the client leaves the agent running and
        burning budget.
        """
        popen: subprocess.Popen[str] = process.extra["popen"]
        timed_out = False
        try:
            stdout, stderr = popen.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            log.warning(
                "background process hit its deadline, killing",
                extra={"container": process.handle.name, "timeout_s": timeout},
            )
            self._pkill_token(process)
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

    def _pkill_token(self, process: BackgroundProcess) -> None:
        """Kill the in-container process by the marker in its environment.

        ``pkill --full`` matches the command line, not the environment, so match on ``/proc``
        directly: ``grep -l`` over ``environ`` finds every process carrying the token, including
        children the agent spawned.
        """
        script = (
            f'for p in /proc/[0-9]*; do if tr "\\0" "\\n" < "$p/environ" 2>/dev/null | '
            f'grep -qx "{TOKEN_ENV}={process.token}"; then kill -9 "${{p#/proc/}}" 2>/dev/null; '
            f"fi; done"
        )
        self.run_lxc(
            "exec", process.handle.name, "-T", "--", "sh", "-c", script, timeout=60.0, check=False
        )


def _is_busy(stderr: str) -> bool:
    """True when LXD refused an operation because another one is still running on the instance.

    Matched on message text because the ``lxc`` CLI exits 1 for every failure and does not
    distinguish this case. Kept narrow deliberately: a broader match would turn a genuine delete
    failure into a two-minute retry loop.
    """
    lowered = stderr.lower()
    return "is busy running" in lowered or "instance is busy" in lowered


def _config_for_limits(spec: ContainerSpec) -> dict[str, str]:
    """Translate backend-neutral :class:`ResourceLimits` into LXD config keys."""
    limits = spec.limits
    config: dict[str, str] = {}
    if limits.cpu is not None:
        config["limits.cpu"] = str(limits.cpu)
    if limits.memory is not None:
        config["limits.memory"] = limits.memory
    if limits.processes is not None:
        config["limits.processes"] = str(limits.processes)
    return config
