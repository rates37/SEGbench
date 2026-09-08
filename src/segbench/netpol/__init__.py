"""Network policy layer.

Responsibility: the host-side egress proxy with its allowlist (inference endpoint only), the
truncating git mirror, the per-run ``netlog.jsonl`` of allowed and denied requests, and the
proxy-side cost meter that trips the per-run USD ceiling.

Implemented in phases 3 and 4.
"""
