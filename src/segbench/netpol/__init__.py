"""Network policy layer.

Responsibility: the host-side egress proxy with its allowlist (inference endpoint, plus the git
mirror in E2), the per-run ``netlog.jsonl`` of allowed and denied requests, and the proxy-side cost
meter that trips the per-run USD ceiling.

Phase 3 (plan.md section 5.1, 5.2): :mod:`.policy` defines what each named environment (E0/E1/E2)
may reach; :mod:`.proxy` is the host-side CONNECT proxy and cost meter; :mod:`.enforce` is the
container-side defence in depth (LXD network ACLs, ``/etc/hosts`` pins, ``iptables``);
:mod:`.verify` is ``segbench netpol verify``, which proves all of the above against a real
container.

The truncating git mirror itself is phase 4.
"""
