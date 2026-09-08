"""The ``segbench runtime smoke`` debug routine.

Exercises every operation the orchestrator will depend on, in the order it will use them, against
a real backend: create, exec, push, pull, background exec with a kill, destroy. It exists because
"the image built" and "a run can actually happen" are different claims, and the second one is the
one that matters at 11pm.

Timings are reported per step. They are the honest way to answer "why is a campaign slow": if
container creation is eight seconds, a 3150-run matrix spends seven hours doing nothing else.
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from segbench.logging import get_logger
from segbench.runtime.base import ContainerSpec, Handle, Runtime, RuntimeFailure

log = get_logger(__name__)

PROBE_TEXT = "segbench smoke probe\n"

#: Tooling the base image promises (plan.md section 9). A missing one fails the smoke: an agent
#: that reaches for `rg` and does not find it wastes its wall clock discovering that.
TOOLS = ("git", "python3", "rg", "fd", "jq", "opencode")


@dataclass
class Step:
    """One timed step of the smoke run."""

    name: str
    duration_s: float
    detail: str = ""


@dataclass
class SmokeResult:
    """The outcome of a smoke run."""

    backend: str
    image: str
    image_digest: str
    container: str
    steps: list[Step] = field(default_factory=list)
    ok: bool = True
    error: str | None = None

    @property
    def total_s(self) -> float:
        return sum(step.duration_s for step in self.steps)


@contextmanager
def _timed(result: SmokeResult, name: str) -> Iterator[list[str]]:
    """Time a block and append a :class:`Step`; the yielded list becomes the step's detail."""
    detail: list[str] = []
    started = time.monotonic()
    try:
        yield detail
    finally:
        result.steps.append(
            Step(name=name, duration_s=time.monotonic() - started, detail=" ".join(detail))
        )


def run_smoke(
    runtime: Runtime,
    image: str,
    *,
    name_factory: Callable[[str], str],
    agent_user: str = "agent",
    kill_timeout_s: float = 3.0,
) -> SmokeResult:
    """Create a container, put it through its paces, and destroy it.

    The container is destroyed in a ``finally`` block *and* is registered with the shared cleanup
    registry by the backend, so interrupting this command leaves nothing behind either way.
    """
    runtime.preflight()
    digest = runtime.image_digest(image)
    spec = ContainerSpec(image=image, name=name_factory("segbench-smoke"))
    result = SmokeResult(
        backend=runtime.name,
        image=image,
        image_digest=digest,
        container=spec.name,
    )

    handle: Handle | None = None
    try:
        with _timed(result, "create") as detail:
            handle = runtime.create(spec)
            detail.append(spec.name)

        with _timed(result, "exec") as detail:
            probe = runtime.exec(
                handle, ["sh", "-c", "id -un; python3 --version; git --version"], check=True
            )
            detail.append(probe.stdout.replace("\n", "; ").strip())

        with _timed(result, "exec (tooling)") as detail:
            # One `command -v` per tool: dash's builtin takes a single argument, so passing four
            # silently reports only the first and an image missing three of them looks fine.
            script = " ".join(f"command -v {tool} >/dev/null && echo {tool};" for tool in TOOLS)
            tools = runtime.exec(handle, ["sh", "-c", f"{script} true"], timeout=60.0)
            found = set(tools.stdout.split())
            missing = [tool for tool in TOOLS if tool not in found]
            if missing:
                raise RuntimeFailure(f"image is missing expected tooling: {', '.join(missing)}")
            detail.append(",".join(TOOLS))

        with _timed(result, "exec (agent user)") as detail:
            # Every real run execs as the unprivileged agent user with /workspace as cwd
            # (plan.md section 10). If that user or that directory is wrong in the image, it must
            # surface here and not in the first campaign.
            whoami = runtime.exec(
                handle,
                ["sh", "-c", "id -un && touch /workspace/.writable && rm /workspace/.writable"],
                user=agent_user,
                cwd="/workspace",
                check=True,
            )
            if whoami.stdout.strip() != agent_user:
                raise RuntimeFailure(
                    f"expected to be running as {agent_user!r}, got {whoami.stdout.strip()!r}"
                )
            detail.append(f"{agent_user} can write /workspace")

        with tempfile.TemporaryDirectory() as tmp:
            outbound = Path(tmp) / "probe.txt"
            outbound.write_text(PROBE_TEXT, encoding="utf-8")
            with _timed(result, "push"):
                runtime.push(handle, outbound, "/workspace/probe.txt")

            with _timed(result, "exec (modify)"):
                runtime.exec(
                    handle,
                    ["sh", "-c", "printf 'round trip\\n' >> /workspace/probe.txt"],
                    check=True,
                )

            inbound = Path(tmp) / "returned.txt"
            with _timed(result, "pull") as detail:
                runtime.pull(handle, "/workspace/probe.txt", inbound)
                text = inbound.read_text(encoding="utf-8")
                if not text.startswith(PROBE_TEXT) or "round trip" not in text:
                    raise RuntimeFailure(f"file did not round-trip intact; got {text!r}")
                detail.append("round-tripped")

        # The agent path: start something long-running, then prove the wall clock kills it both
        # client-side and inside the container (plan.md section 2).
        with _timed(result, "background exec + kill") as detail:
            process = runtime.exec_background(handle, ["sleep", "600"])
            killed = runtime.wait_or_kill(process, timeout=kill_timeout_s)
            if not killed.timed_out:
                raise RuntimeFailure("background process was expected to hit its deadline")
            survivors = runtime.exec(handle, ["sh", "-c", "pgrep -c -x sleep || true"])
            remaining = survivors.stdout.strip() or "0"
            if remaining not in ("0", ""):
                raise RuntimeFailure(
                    f"{remaining} sleep process(es) survived the kill inside the container"
                )
            detail.append("killed, no survivors")

    except Exception as exc:
        result.ok = False
        result.error = str(exc)
        log.error("smoke failed", extra={"container": spec.name, "error": str(exc)})
    finally:
        if handle is not None:
            with _timed(result, "destroy"):
                runtime.destroy(handle)

    return result
