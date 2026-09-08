"""Scrubber detectors, one test per detector plus its allowlist.

The allowlist assertions matter as much as the detections: a checker that flags 127.0.0.1 gets
switched off, and a switched-off checker protects nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from segbench.corpus.findings import Severity, redact
from segbench.corpus.scrub import ScrubPolicy, scan_file, scan_text

POLICY = ScrubPolicy()


def detectors(text: str, policy: ScrubPolicy = POLICY) -> list[str]:
    return [f.detector for f in scan_text(text, bug_id="b", file=Path("f"), policy=policy)]


# --- credentials, tokens, keys ----------------------------------------------------------------


def test_private_key_header_is_detected() -> None:
    assert "private_key" in detectors("-----BEGIN RSA PRIVATE KEY-----")
    assert "private_key" in detectors("-----BEGIN OPENSSH PRIVATE KEY-----")


@pytest.mark.parametrize(
    "line",
    [
        "password: 8Hq2vLpZx4Tn6Wd0",
        "admin_pass=Sup3rSekritValue",
        '"client_secret": "abcdefghijklmnop"',
        "keystone_password = zK19dhAlq2",
    ],
)
def test_assigned_secrets_are_detected(line: str) -> None:
    assert "credential" in detectors(line)


@pytest.mark.parametrize(
    "line",
    [
        "password: REDACTED",
        "api_key: <redacted>",
        "password: none",
        "os_password: ${OS_PASSWORD}",
        "password: {{ vault_lookup }}",
    ],
)
def test_placeholder_secrets_are_not_flagged(line: str) -> None:
    assert "credential" not in detectors(line)


def test_allowlisted_secret_value_is_not_flagged() -> None:
    policy = ScrubPolicy(allow_values=("hunter2docsexample",))
    assert "credential" not in detectors("password: hunter2docsexample", policy)


@pytest.mark.parametrize(
    ("line", "detector"),
    [
        ("aws_access_key_id = AKIAIOSFODNN7EXAMPLE", "aws_access_key_id"),
        ("token: ghp_abcdefghij0123456789ABCDEFGHIJ", "github_token"),
        ("XOXB: xoxb-1234567890-abcdefghij", "slack_token"),
        ("auth: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP0mB92K", "jwt"),
    ],
)
def test_provider_tokens_are_detected(line: str, detector: str) -> None:
    assert detector in detectors(line)


# --- cloud account identifiers ----------------------------------------------------------------


def test_aws_account_id_is_detected() -> None:
    assert "aws_account_id" in detectors("account_id: 123456789012")


def test_aws_account_id_inside_an_arn_is_detected() -> None:
    assert "aws_arn" in detectors("arn:aws:iam::123456789012:role/ops")


def test_bare_twelve_digit_number_is_not_flagged() -> None:
    """Without the `account` context word this is just a number — a pid, a size, a timestamp."""
    assert "aws_account_id" not in detectors("bytes transferred: 123456789012")


def test_azure_subscription_id_is_detected() -> None:
    line = "subscription_id: 3f2504e0-4f89-41d3-9a0c-0305e82c3301"
    assert "azure_subscription_id" in detectors(line)


def test_gcp_project_id_is_detected() -> None:
    assert "gcp_project_id" in detectors("project_id: acme-prod-canary-01")


# --- addresses --------------------------------------------------------------------------------


def test_routable_ipv4_is_detected() -> None:
    assert "ipv4" in detectors("controller reachable at 91.189.88.152")


@pytest.mark.parametrize(
    "address",
    [
        "10.5.0.31",  # RFC1918
        "192.168.1.1",  # RFC1918
        "172.16.4.9",  # RFC1918
        "127.0.0.1",  # loopback
        "169.254.169.254",  # link local
        "203.0.113.9",  # RFC5737 documentation
        "192.0.2.4",  # RFC5737 documentation
        "198.51.100.7",  # RFC5737 documentation
    ],
)
def test_non_global_ipv4_is_allowed(address: str) -> None:
    assert "ipv4" not in detectors(f"address {address} seen")


def test_version_string_is_not_read_as_an_ipv4_address() -> None:
    assert "ipv4" not in detectors("nova-common 3:27.1.0-0ubuntu1 and 22.04.1.2 build")


def test_routable_ipv6_is_detected() -> None:
    assert "ipv6" in detectors("bound to 2a01:4f8:c17:b8f::1")


@pytest.mark.parametrize("address", ["::1", "fe80::3eec:efff:fe1a:8b44", "fd00::5", "2001:db8::7"])
def test_non_global_ipv6_is_allowed(address: str) -> None:
    assert "ipv6" not in detectors(f"address {address} seen")


def test_timestamp_is_not_read_as_an_ipv6_address() -> None:
    assert "ipv6" not in detectors("2024-05-02 11:44:19.552 4471 ERROR nova.compute.manager")


# --- MAC addresses ----------------------------------------------------------------------------


@pytest.mark.parametrize("mac", ["3c:ec:ef:1a:8b:44", "3C-EC-EF-1A-8B-44"])
def test_mac_address_is_detected(mac: str) -> None:
    assert "mac_address" in detectors(f"link/ether {mac} brd")


@pytest.mark.parametrize("mac", ["00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff", "00:00:5e:00:53:01"])
def test_reserved_macs_are_allowed(mac: str) -> None:
    assert "mac_address" not in detectors(f"link/ether {mac} brd")


def test_configured_mac_prefix_is_allowed() -> None:
    policy = ScrubPolicy(allow_mac_prefixes=("52:54:00",))
    assert "mac_address" not in detectors("link/ether 52:54:00:12:34:56 brd", policy)


# --- email and hostnames ----------------------------------------------------------------------


def test_email_is_detected() -> None:
    assert "email" in detectors("reported by dana.okafor@acme-operations.co.uk")


@pytest.mark.parametrize(
    "address",
    ["admin@example.com", "ops@example.org", "root@localhost", "noreply@something.invalid"],
)
def test_reserved_domains_are_allowed(address: str) -> None:
    assert "email" not in detectors(f"contact {address}")


def test_configured_email_domain_is_allowed() -> None:
    policy = ScrubPolicy(allow_email_domains=("canonical.com",))
    assert "email" not in detectors("contact support@canonical.com", policy)


def test_customer_hostname_is_detected_only_when_configured() -> None:
    line = "Host: compute-07.acmecorp.internal"
    assert "customer_hostname" not in detectors(line)

    policy = ScrubPolicy(customer_host_patterns=(r"\.acmecorp\.internal$",))
    assert "customer_hostname" in detectors(line, policy)


def test_unrelated_hostname_is_not_flagged_by_a_customer_pattern() -> None:
    policy = ScrubPolicy(customer_host_patterns=(r"\.acmecorp\.internal$",))
    assert "customer_hostname" not in detectors("see opendev.org for the repo", policy)


def test_invalid_customer_pattern_is_rejected_loudly() -> None:
    with pytest.raises(ValueError, match="invalid regex"):
        ScrubPolicy(customer_host_patterns=("[unclosed",))


# --- reporting shape --------------------------------------------------------------------------


def test_findings_carry_file_line_and_detector() -> None:
    text = "clean line\npassword: 8Hq2vLpZx4Tn6Wd0\n"
    findings = scan_text(text, bug_id="lp-1", file=Path("channels/x.yaml"))

    assert len(findings) == 1
    assert findings[0].bug_id == "lp-1"
    assert findings[0].file == Path("channels/x.yaml")
    assert findings[0].line == 2
    assert findings[0].detector == "credential"
    assert findings[0].severity is Severity.ERROR


def test_findings_never_quote_the_secret_in_full() -> None:
    secret = "8Hq2vLpZx4Tn6Wd0"
    findings = scan_text(f"password: {secret}", bug_id="b", file=Path("f"))

    assert secret not in (findings[0].excerpt or "")
    assert secret not in str(findings[0])


@pytest.mark.parametrize(("value", "expected"), [("abcd", "***"), ("abcdefghij", "abc...hij")])
def test_redact(value: str, expected: str) -> None:
    assert redact(value) == expected


def test_binary_file_is_warned_about_not_silently_skipped(tmp_path: Path) -> None:
    blob = tmp_path / "sosreport.bin"
    blob.write_bytes(b"\xff\xfe\x00\x01binary")

    findings = scan_file(blob, bug_id="b")

    assert [f.detector for f in findings] == ["binary_attachment"]
    assert findings[0].severity is Severity.WARNING
