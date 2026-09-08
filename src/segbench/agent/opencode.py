"""Generates the per-run ``opencode`` config and invokes it headless in the container (plan.md
section 10).

Two things worth being upfront about, because they cannot be verified against a real container
from here:

* **Provider id for Copilot.** OpenCode's provider directory (``opencode.ai/docs/providers``)
  documents "GitHub Copilot" as a supported provider reached via ``/connect`` device-code OAuth,
  not via a plain API key. Headless, unattended device-code auth is not something this harness can
  drive, so :data:`PROVIDER_IDS` assumes the provider id is ``github-copilot`` and that it accepts
  a bearer token the same way every other provider accepts ``options.apiKey``. If a future opencode
  release disagrees, this is the one place to fix it.
* **The config is delivered via ``OPENCODE_CONFIG_CONTENT``**, an inline-JSON environment variable
  opencode reads at a config precedence tier below only the managed-settings layer. This avoids
  pushing a file into the container and pointing opencode at it, and keeps the config — including
  the API key reference — entirely out of the image and off disk.

The API key itself never appears in the generated JSON: the config only carries an
``{env:VARNAME}`` reference (opencode's own variable-substitution syntax), and the real secret is
set as a container-local exec environment variable, read fresh from the host process's own
environment for every run (never logged, never written to the run record).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from segbench.config import ModelConfig, Settings
from segbench.logging import get_logger
from segbench.runtime.base import BackgroundProcess, ExecResult, Handle, Runtime

log = get_logger(__name__)

#: Maps ``ProviderConfig.kind`` (segbench's own vocabulary) onto opencode's provider id.
PROVIDER_IDS: dict[str, str] = {
    "openrouter": "openrouter",
    "copilot": "github-copilot",
}

#: The env var opencode substitutes the API key from. Fixed rather than reusing
#: ``settings.provider.api_key_env`` verbatim, so the container-side variable name never depends
#: on whatever the maintainer happened to name the host-side one.
API_KEY_CONTAINER_ENV = "SEGBENCH_OPENCODE_API_KEY"

#: Grace period after the wall clock expires before the harness gives up waiting for opencode's
#: own client and kill sequence to unwind (plan.md section 2: the wall clock is enforced, but a
#: kill still has to propagate through a client process and an in-container pkill).
KILL_GRACE_S = 20.0


def provider_id(settings: Settings) -> str:
    kind = settings.provider.kind
    if kind not in PROVIDER_IDS:
        raise ValueError(f"no opencode provider id known for provider.kind={kind!r}")
    return PROVIDER_IDS[kind]


def build_config(settings: Settings, model: ModelConfig) -> dict[str, Any]:
    """The opencode config for one run: one provider, one pinned model, nothing interactive.

    ``share: disabled`` and ``autoupdate: false`` are not just tidiness — both would need network
    egress this run does not have, and a blocked network call at startup is a bad way to lose part
    of the wall clock. ``snapshot: false`` keeps opencode's own change-tracking git repository from
    ever touching the seeded E1/E2 working tree.
    """
    pid = provider_id(settings)
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": f"{pid}/{model.id}",
        "autoupdate": False,
        "share": "disabled",
        "snapshot": False,
        "permission": {"*": "allow"},
        "provider": {
            pid: {
                "options": {
                    "apiKey": f"{{env:{API_KEY_CONTAINER_ENV}}}",
                    "baseURL": settings.provider.base_url,
                }
            }
        },
    }


def render_config_json(settings: Settings, model: ModelConfig) -> str:
    return json.dumps(build_config(settings, model))


@dataclass
class AgentRunResult:
    """What came out of one headless ``opencode run`` invocation."""

    exec_result: ExecResult
    session_id: str | None
    started_at: float
    ended_at: float

    @property
    def timed_out(self) -> bool:
        return self.exec_result.timed_out


def invoke_agent(
    runtime: Runtime,
    handle: Handle,
    *,
    settings: Settings,
    model: ModelConfig,
    prompt: str,
    wall_clock_s: float,
    agent_user: str,
    cwd: str = "/workspace",
) -> AgentRunResult:
    """Run ``opencode run <prompt>`` headless, enforcing the wall clock.

    The prompt travels as a single argv element, never through a shell, so its content (which
    embeds arbitrary channel text) cannot be interpreted as shell syntax. Returns once the process
    exits or is killed at the wall clock; the caller is responsible for everything that must
    happen concurrently (answer snapshotting — see :mod:`segbench.agent.answers`).
    """
    env = {
        API_KEY_CONTAINER_ENV: settings.provider.api_key(),
        "OPENCODE_CONFIG_CONTENT": render_config_json(settings, model),
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
    }
    argv = ["opencode", "run", prompt, "--model", f"{provider_id(settings)}/{model.id}"]

    log.info(
        "invoking opencode",
        extra={"container": handle.name, "model": model.id, "wall_clock_s": wall_clock_s},
    )
    started = time.time()
    process: BackgroundProcess = runtime.exec_background(
        handle, argv, env=env, user=agent_user, cwd=cwd
    )
    result = runtime.wait_or_kill(process, timeout=wall_clock_s + KILL_GRACE_S)
    ended = time.time()

    if result.timed_out:
        log.warning(
            "opencode hit the wall clock and was killed",
            extra={"container": handle.name, "wall_clock_s": wall_clock_s},
        )

    session_id = _latest_session_id(runtime, handle, env=env, agent_user=agent_user, cwd=cwd)
    return AgentRunResult(
        exec_result=result, session_id=session_id, started_at=started, ended_at=ended
    )


def _latest_session_id(
    runtime: Runtime, handle: Handle, *, env: dict[str, str], agent_user: str, cwd: str
) -> str | None:
    """The most recently created session in this project, per ``opencode session list``.

    Each run gets a fresh container and a fresh ``/workspace``, i.e. a fresh opencode "project",
    so there should be exactly one session; this does not assume that and just takes the newest.
    Returns ``None`` (never raises) on any failure to list — a run whose agent process never even
    started opencode successfully has no session to find, and that is a normal (if unfortunate)
    outcome, not a harness error.
    """
    result = runtime.exec(
        handle,
        ["opencode", "session", "list", "--format", "json", "-n", "1"],
        env=env,
        user=agent_user,
        cwd=cwd,
        timeout=30.0,
    )
    if not result.ok:
        log.warning(
            "could not list opencode sessions",
            extra={"container": handle.name, "stderr": result.stderr[-2000:]},
        )
        return None
    try:
        sessions = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning(
            "opencode session list did not return JSON",
            extra={"container": handle.name, "stdout": result.stdout[-2000:]},
        )
        return None
    if not isinstance(sessions, list) or not sessions:
        return None
    first = sessions[0]
    session_id = first.get("id") if isinstance(first, dict) else None
    return session_id if isinstance(session_id, str) else None


def export_session(
    runtime: Runtime,
    handle: Handle,
    session_id: str,
    *,
    settings: Settings,
    model: ModelConfig,
    agent_user: str,
    cwd: str = "/workspace",
) -> str:
    """``opencode export <session_id> --sanitize``, returning the raw JSON text.

    ``--sanitize`` redacts sensitive transcript/file data at opencode's own discretion — the right
    default for anything that might end up in a shared results directory. The same env the run
    used is passed again: export reads the project's local session storage, not the network.
    """
    env = {
        API_KEY_CONTAINER_ENV: settings.provider.api_key(),
        "OPENCODE_CONFIG_CONTENT": render_config_json(settings, model),
    }
    result = runtime.exec(
        handle,
        ["opencode", "export", session_id, "--sanitize"],
        env=env,
        user=agent_user,
        cwd=cwd,
        timeout=60.0,
        check=True,
    )
    return result.stdout
