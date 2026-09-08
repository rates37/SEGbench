"""Container-side defence in depth (plan.md sections 5.1 and 5.1.1).

The proxy's allowlist is the enforcement that actually matters; everything here exists so that a
misconfigured network, a stopped proxy, or a tool that bypasses the ``http_proxy`` env vars
entirely does not silently produce an unpoliced run. Three independent layers, all built from the
same :class:`~segbench.netpol.policy.EgressPolicy`:

1. a **dedicated, routeless LXD network** per named policy — no path in or out of the bridge at
   all (plan.md section 5.1.1 records why this is a dedicated network rather than a shared one
   with per-container ACLs: ACLs are plan.md's own preferred option, but `security.acls` is
   rejected as an invalid device option on the LXD this harness has actually been run against, on
   both NIC styles, confirmed by hand before writing this module);
2. **``/etc/hosts`` pins** for every host on the leak watchlist, so a DNS lookup for e.g.
   ``github.com`` resolves to ``127.0.0.1`` before a packet is ever sent;
3. **``iptables`` rules** inside the container's own network namespace, default-dropping outbound
   traffic except loopback, established/related return traffic, and the proxy (plus the mirror,
   in E2).

None of the three depends on the others being correct: layer 1 fails closed on its own regardless
of what runs inside the container, layer 2 fails closed if something resolves a host directly
against ``/etc/hosts`` rather than the proxy, layer 3 fails closed if both of the above are somehow
misapplied. Losing any one of them should still leave a run policed.

This module also installs the proxy's MITM CA (see :mod:`segbench.netpol.proxy`) into the
container's trust store — without it, the container's TLS client would refuse the proxy's
inference-host certificate and every inference call would fail closed.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from segbench.logging import get_logger
from segbench.netpol.policy import LEAK_WATCHLIST, EgressPolicy
from segbench.runtime.base import Handle, Runtime
from segbench.runtime.lxd import LXDRuntime

log = get_logger(__name__)

#: Prefix for the dedicated LXD networks this module creates, one per named policy.
NETWORK_PREFIX = "segbench"


def network_name(policy: EgressPolicy) -> str:
    """The dedicated LXD network for ``policy`` (e.g. ``segbench-e0``). Kept short: Linux caps
    bridge interface names at 15 characters."""
    return f"{NETWORK_PREFIX}-{policy.name.lower()}"


def ensure_network(lxd: LXDRuntime, policy: EgressPolicy) -> str:
    """Create the dedicated, routeless network for ``policy`` if it does not already exist.

    ``ipv4.address=auto`` picks a free subnet rather than guessing one that might collide with
    another network on the host; ``ipv4.nat=false`` and ``ipv4.routing=false`` together mean
    nothing on this bridge has a path to the internet or to any other network, while the host's
    own gateway address on it (queried by ``netpol.verify`` and the orchestrator) is still fully
    reachable — that address is exactly where the per-run proxy listener binds, since it binds
    ``0.0.0.0``. No IPv6: nothing needs it, and it is one less address family to have locked down.
    """
    name = network_name(policy)
    if lxd.run_lxc("network", "show", name, check=False).returncode == 0:
        return name
    lxd.run_lxc(
        "network",
        "create",
        name,
        "ipv4.address=auto",
        "ipv4.nat=false",
        "ipv4.routing=false",
        "ipv6.address=none",
    )
    log.info("dedicated egress network created", extra={"network": name, "policy": policy.name})
    return name


def _iptables_script(
    *, proxy_host: str, proxy_port: int, mirror_host: str | None, mirror_port: int | None
) -> str:
    lines = [
        "iptables -F OUTPUT",
        "iptables -P OUTPUT DROP",
        "iptables -A OUTPUT -o lo -j ACCEPT",
        "iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT",
        f"iptables -A OUTPUT -p tcp -d {proxy_host} --dport {proxy_port} -j ACCEPT",
    ]
    if mirror_host and mirror_port:
        lines.append(f"iptables -A OUTPUT -p tcp -d {mirror_host} --dport {mirror_port} -j ACCEPT")
    return "\n".join(lines) + "\n"


def apply(
    runtime: Runtime,
    handle: Handle,
    policy: EgressPolicy,
    *,
    proxy_host: str,
    proxy_port: int,
    ca_cert_pem: bytes | None = None,
) -> None:
    """Apply the two in-container layers (``/etc/hosts`` pins, ``iptables``) and, if given, install
    the proxy's MITM CA. Runs as root; called once, right after the container becomes ready."""
    mirror_host = policy.mirror_host if policy.allow_mirror else None
    mirror_port = policy.mirror_port if policy.allow_mirror else None

    hosts_lines = "\n".join(f"127.0.0.1 {host}" for host in sorted(LEAK_WATCHLIST))
    hosts_block = (
        f"cat >> /etc/hosts <<'SEGBENCH_HOSTS'\n# segbench-deny\n{hosts_lines}\nSEGBENCH_HOSTS\n"
    )
    iptables_block = _iptables_script(
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        mirror_host=mirror_host,
        mirror_port=mirror_port,
    )
    script = hosts_block + iptables_block
    runtime.exec(handle, ["sh", "-c", script], user="root", check=True, timeout=60.0)
    log.info(
        "container-side enforcement applied",
        extra={"container": handle.name, "policy": policy.name},
    )

    if ca_cert_pem is not None:
        with tempfile.NamedTemporaryFile(suffix=".crt") as tmp:
            tmp.write(ca_cert_pem)
            tmp.flush()
            runtime.push(
                handle,
                Path(tmp.name),
                "/usr/local/share/ca-certificates/segbench-proxy-ca.crt",
                mode="644",
            )
        runtime.exec(handle, ["update-ca-certificates"], user="root", check=True, timeout=60.0)
        log.info("proxy CA installed in container trust store", extra={"container": handle.name})
