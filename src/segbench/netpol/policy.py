"""What one run may reach on the network (plan.md section 5.1).

This is the single source of truth the rest of the ``netpol`` package agrees on: the proxy's
allowlist (:mod:`segbench.netpol.proxy`), the container-side defence in depth
(:mod:`segbench.netpol.enforce`) and the thing that proves it all holds
(:mod:`segbench.netpol.verify`) are all built from an :class:`EgressPolicy`.

Deliberately not the same type as :class:`segbench.runtime.base.NetworkPolicy` — that one is the
container-attachment handle the runtime layer applies verbatim (a network name, ACL names, env
vars); this one is the human-legible "what is reachable" statement everything else is derived
from. Conflating them would let a change to one silently drift from the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from segbench.config import Settings

#: Hosts that are denied even though they would fail anyway (no route out of the container's
#: network namespace reaches them) — a run reaching for one of these is trying to leak (CLAUDE.md
#: invariant 1), not merely making an ordinary mistake, so denials against this list are tagged
#: ``leak_attempt: true`` in the netlog and pinned to 127.0.0.1 in ``/etc/hosts`` as an extra
#: layer. plan.md section 5.1 names this list explicitly; kept here so policy, proxy and enforce
#: all read the same one.
LEAK_WATCHLIST: tuple[str, ...] = (
    "launchpad.net",
    "bugs.launchpad.net",
    "code.launchpad.net",
    "answers.launchpad.net",
    "github.com",
    "raw.githubusercontent.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "opendev.org",
    "review.opendev.org",
    "google.com",
    "www.google.com",
    "bing.com",
    "duckduckgo.com",
    "pypi.org",
    "files.pythonhosted.org",
    "pypi.python.org",
    "npmjs.com",
    "registry.npmjs.org",
    "stackoverflow.com",
    "stackexchange.com",
)


def is_leak_target(host: str) -> bool:
    """True when ``host`` is, or is a subdomain of, an entry on :data:`LEAK_WATCHLIST`."""
    normalised = host.lower().rstrip(".")
    return any(
        normalised == watched or normalised.endswith(f".{watched}") for watched in LEAK_WATCHLIST
    )


@dataclass(frozen=True)
class EgressPolicy:
    """What one run under a given environment (E0, E1 or E2) may reach.

    ``name`` is the environment id and doubles as the LXD ACL name suffix and the ``netpol
    verify`` report label — keep it one of ``E0``/``E1``/``E2``.
    """

    name: str
    inference_host: str
    inference_port: int = 443
    allow_mirror: bool = False
    mirror_host: str | None = None
    mirror_port: int | None = None
    description: str = ""

    def permits(self, host: str, port: int) -> bool:
        """True if ``host:port`` is on this policy's allowlist. Everything else is denied."""
        normalised = host.lower().rstrip(".")
        if normalised == self.inference_host.lower() and port == self.inference_port:
            return True
        return bool(
            self.allow_mirror
            and self.mirror_host
            and normalised == self.mirror_host.lower()
            and port == self.mirror_port
        )

    @property
    def allowed_hosts(self) -> frozenset[str]:
        hosts = {self.inference_host}
        if self.allow_mirror and self.mirror_host:
            hosts.add(self.mirror_host)
        return frozenset(hosts)


def _inference_endpoint(settings: Settings) -> tuple[str, int]:
    """Derive the allowed inference host and port from ``provider.base_url``.

    The allowlist must track whatever the provider is actually configured to, rather than
    hardcoding ``openrouter.ai``: the harness also supports Copilot (CLAUDE.md), and a policy
    built against the wrong host would either lock every run out or, worse, allow a host nobody
    reviewed.
    """
    parsed = urlsplit(settings.provider.base_url)
    if not parsed.hostname:
        raise ValueError(f"provider.base_url has no host: {settings.provider.base_url!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.hostname, port


def build_policies(settings: Settings) -> dict[str, EgressPolicy]:
    """Build the three named policies (plan.md section 5) from the current settings."""
    host, port = _inference_endpoint(settings)

    e0 = EgressPolicy(
        name="E0",
        inference_host=host,
        inference_port=port,
        description="knowledge only: no repository, inference API only",
    )
    e1 = EgressPolicy(
        name="E1",
        inference_host=host,
        inference_port=port,
        description="pre-seeded truncated repository, no git network access",
    )
    e2 = EgressPolicy(
        name="E2",
        inference_host=host,
        inference_port=port,
        allow_mirror=True,
        mirror_host=settings.netpol.mirror_host,
        mirror_port=settings.netpol.mirror_port,
        description="free cloning via the host-side truncating mirror",
    )
    return {"E0": e0, "E1": e1, "E2": e2}
