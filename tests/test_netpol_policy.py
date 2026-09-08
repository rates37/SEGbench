"""The egress allowlist model (plan.md section 5.1)."""

from __future__ import annotations

import pytest

from segbench.config import Settings
from segbench.netpol.policy import EgressPolicy, build_policies, is_leak_target


def test_policies_are_built_from_the_provider_base_url() -> None:
    settings = Settings()  # default provider.base_url is https://openrouter.ai/api/v1

    policies = build_policies(settings)

    assert set(policies) == {"E0", "E1", "E2"}
    for policy in policies.values():
        assert policy.inference_host == "openrouter.ai"
        assert policy.inference_port == 443


def test_only_e2_allows_the_mirror() -> None:
    policies = build_policies(Settings())

    assert not policies["E0"].allow_mirror
    assert not policies["E1"].allow_mirror
    assert policies["E2"].allow_mirror
    assert policies["E2"].permits(policies["E2"].mirror_host, policies["E2"].mirror_port)
    assert not policies["E0"].permits(
        policies["E2"].mirror_host or "", policies["E2"].mirror_port or 0
    )


def test_permits_only_the_configured_inference_endpoint() -> None:
    policy = EgressPolicy(name="E0", inference_host="openrouter.ai", inference_port=443)

    assert policy.permits("openrouter.ai", 443)
    assert policy.permits("OpenRouter.AI", 443)  # case-insensitive
    assert not policy.permits("openrouter.ai", 80)
    assert not policy.permits("evil.example.com", 443)


@pytest.mark.parametrize(
    "host",
    [
        "launchpad.net",
        "bugs.launchpad.net",
        "www.github.com",  # subdomain of a watched host
        "GITHUB.com",  # case-insensitive
        "pypi.org",
        "opendev.org",
    ],
)
def test_leak_watchlist_catches_trackers_and_indexes(host: str) -> None:
    assert is_leak_target(host)


def test_leak_watchlist_does_not_flag_the_inference_host() -> None:
    assert not is_leak_target("openrouter.ai")
