"""``segbench netpol verify`` — proves a policy holds against a real container.

This is the thing that convinces a reader the benchmark is sound (CLAUDE.md invariants 1 and 2):
it wires up a real container exactly as a benchmark run would — the proxy, the dedicated routeless
LXD network, the ``/etc/hosts`` pins, the ``iptables`` rules, the MITM CA — and then, from *inside*
that container, asserts the inference endpoint is reachable and a representative set of denied
destinations are not, each checked both through the proxy (proving the allowlist) and, for two of
them, bypassing the proxy outright (proving the defence-in-depth actually stops something the
proxy never gets a chance to see).
"""

from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from segbench.config import Settings
from segbench.logging import get_logger
from segbench.netpol import enforce
from segbench.netpol.policy import EgressPolicy, build_policies
from segbench.netpol.proxy import EgressProxy
from segbench.runtime.base import ContainerSpec, Runtime, RuntimeFailure
from segbench.runtime.base import NetworkPolicy as RuntimeNetworkPolicy
from segbench.runtime.lxd import LXDRuntime, sanitise_name

log = get_logger(__name__)


@dataclass(frozen=True)
class Check:
    """One representative destination to probe, and what should happen to it."""

    name: str
    target: str
    via_proxy: bool
    expect_reachable: bool


@dataclass
class CheckResult:
    check: Check
    passed: bool
    detail: str


@dataclass
class VerifyReport:
    policy: str
    results: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.passed for r in self.results)


#: plan.md section 13's acceptance list: the inference endpoint plus a search engine, launchpad,
#: github, opendev, pypi, a raw IP over HTTP, and DNS exfiltration via a direct resolver. The last
#: two deliberately bypass the proxy (``via_proxy=False``) — they are the defence-in-depth checks,
#: not the allowlist checks.
_INFERENCE = "__inference__"


def standard_checks() -> list[Check]:
    return [
        Check("inference endpoint", _INFERENCE, via_proxy=True, expect_reachable=True),
        Check("search engine", "https://www.google.com/", via_proxy=True, expect_reachable=False),
        Check("launchpad", "https://launchpad.net/", via_proxy=True, expect_reachable=False),
        Check("github", "https://github.com/", via_proxy=True, expect_reachable=False),
        Check("opendev", "https://opendev.org/", via_proxy=True, expect_reachable=False),
        Check("pypi", "https://pypi.org/", via_proxy=True, expect_reachable=False),
        Check(
            "raw IP over HTTP (bypassing the proxy)",
            "http://1.1.1.1/",
            via_proxy=False,
            expect_reachable=False,
        ),
        Check(
            "DNS exfiltration via a direct resolver",
            "8.8.8.8:53",
            via_proxy=False,
            expect_reachable=False,
        ),
    ]


def run_check(runtime: Runtime, handle, check: Check, *, inference_url: str) -> CheckResult:
    """Run one check inside the container and compare against what it should be able to reach."""
    if check.name.startswith("DNS exfiltration"):
        host, _, port = check.target.partition(":")
        result = runtime.exec(
            handle,
            ["timeout", "3", "bash", "-c", f"exec 3<>/dev/tcp/{host}/{port}"],
            timeout=10.0,
        )
    else:
        url = inference_url if check.target == _INFERENCE else check.target
        argv = ["curl", "-sS", "-o", "/dev/null", "--max-time", "8"]
        if not check.via_proxy:
            argv += ["--noproxy", "*"]
        argv.append(url)
        result = runtime.exec(handle, argv, timeout=15.0)

    # curl (and the bash /dev/tcp probe) exit 0 only on a completed TCP/TLS/HTTP round trip;
    # a proxy 403 on CONNECT, a dropped packet, or a timeout all exit non-zero. The response's
    # actual status code does not matter here — a 401 from the real inference API without a key
    # still proves the path is open, which is all this check asserts.
    reachable = result.returncode == 0
    passed = reachable == check.expect_reachable
    detail = f"exit={result.returncode} reachable={reachable} (expected {check.expect_reachable})"
    return CheckResult(check=check, passed=passed, detail=detail)


def _gateway_ip(lxd: LXDRuntime, network: str) -> str:
    proc = lxd.run_lxc("network", "get", network, "ipv4.address", check=False)
    if proc.returncode != 0:
        raise RuntimeFailure(
            f"could not read the gateway address of LXD network {network!r}", stderr=proc.stderr
        )
    cidr = proc.stdout.strip()
    if not cidr:
        raise RuntimeFailure(f"LXD network {network!r} has no ipv4.address set")
    return cidr.split("/", 1)[0]


def verify_policy(
    runtime: Runtime,
    image: str,
    policy_name: str,
    settings: Settings,
    *,
    proxy: EgressProxy | None = None,
) -> VerifyReport:
    """Build a real container under ``policy_name`` and run :func:`standard_checks` against it."""
    policies = build_policies(settings)
    if policy_name not in policies:
        raise ValueError(f"unknown policy {policy_name!r}; choose one of {sorted(policies)}")
    policy: EgressPolicy = policies[policy_name]

    is_lxd = isinstance(runtime, LXDRuntime)
    # The dedicated network must exist before the proxy's env vars can name its gateway address,
    # and before the container can attach to it (plan.md section 5.1.1).
    network = enforce.ensure_network(runtime, policy) if is_lxd else None
    bridge_ip = _gateway_ip(runtime, network) if network else "127.0.0.1"

    owns_proxy = proxy is None
    proxy = proxy or EgressProxy(settings)
    run_id = f"verify-{policy_name.lower()}-{int(time.time())}"
    netlog_path = Path(tempfile.mkdtemp(prefix="segbench-verify-")) / "netlog.jsonl"
    run_handle = proxy.start_run(
        run_id, policy, netlog_path=netlog_path, max_cost_usd=settings.caps.max_cost_usd
    )

    spec = ContainerSpec(
        image=image,
        name=sanitise_name(f"segbench-verify-{policy_name.lower()}"),
        network=RuntimeNetworkPolicy(
            name=policy.name,
            network=network,
            env=run_handle.env(advertise_host=bridge_ip),
        ),
        wait_for_network=True,
    )

    report = VerifyReport(policy=policy.name)
    handle = None
    try:
        handle = runtime.create(spec)
        enforce.apply(
            runtime,
            handle,
            policy,
            proxy_host=bridge_ip,
            proxy_port=run_handle.port,
            ca_cert_pem=proxy.ca_cert_pem,
        )
        for check in standard_checks():
            report.results.append(
                run_check(runtime, handle, check, inference_url=settings.provider.base_url)
            )
    finally:
        if handle is not None:
            runtime.destroy(handle)
        proxy.stop_run(run_id)
        if owns_proxy:
            proxy.shutdown()

    return report
