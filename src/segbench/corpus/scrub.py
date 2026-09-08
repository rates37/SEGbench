"""The scrubber: detect data that must never be committed to the corpus.

Corpus channels and attachments are drawn from real support material — sosreports, ``juju status``
dumps, service logs — which routinely carry hostnames, addresses, cloud account identifiers and
credentials. ``segbench corpus validate`` runs every detector here over every channel file and
attachment and fails on a hit (CLAUDE.md, "Safety and data hygiene").

The design constraint that matters is **usability**: a checker that flags ``127.0.0.1`` and
``admin@example.com`` gets switched off within a day, and a switched-off checker protects nothing.
So every detector is paired with an allowlist of values that are safe by construction — non-global
IP space, the reserved documentation domains, broadcast MACs — and the maintainer can extend each
list from config.

Findings never quote the matched text in full; see :func:`segbench.corpus.findings.redact`.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from segbench.corpus.findings import Finding, Severity, redact

# Domains reserved by RFC 2606 / RFC 6761 for documentation and examples. Anything under them is
# safe by construction, so both the email and hostname detectors ignore them.
SAFE_DOMAIN_SUFFIXES: tuple[str, ...] = (
    "example.com",
    "example.org",
    "example.net",
    "example.edu",
    "example",
    "invalid",
    "test",
    "localhost",
    "localdomain",
)

SAFE_MACS: frozenset[str] = frozenset(
    {
        "00:00:00:00:00:00",
        "ff:ff:ff:ff:ff:ff",
        # The MAC ranges reserved for documentation by RFC 7042 section 2.1.
        "00:00:5e:00:53:00",
    }
)

# RFC 7042 reserved-for-documentation MAC prefixes.
SAFE_MAC_PREFIXES: tuple[str, ...] = ("00:00:5e:00:53", "00:53:00")


@dataclass(frozen=True)
class ScrubPolicy:
    """Tunable half of the scrubber: what counts as safe, and what counts as a customer host.

    ``customer_host_patterns`` are regular expressions matched case-insensitively against any
    hostname-shaped token. They are deliberately empty by default: the maintainer knows their
    customers' naming conventions and the harness does not, and guessing produces noise.
    """

    customer_host_patterns: tuple[str, ...] = ()
    allow_values: tuple[str, ...] = ()
    allow_email_domains: tuple[str, ...] = ()
    allow_mac_prefixes: tuple[str, ...] = ()
    _compiled_hosts: tuple[re.Pattern[str], ...] = field(init=False, repr=False, default=())

    def __post_init__(self) -> None:
        compiled = []
        for pattern in self.customer_host_patterns:
            try:
                compiled.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                raise ValueError(
                    f"corpus.customer_host_patterns contains an invalid regex {pattern!r}: {exc}"
                ) from exc
        object.__setattr__(self, "_compiled_hosts", tuple(compiled))

    def allows(self, value: str) -> bool:
        """True when ``value`` is on the literal allowlist."""
        return value.strip().lower() in {v.strip().lower() for v in self.allow_values}

    def is_customer_host(self, host: str) -> bool:
        return any(p.search(host) for p in self._compiled_hosts)


DEFAULT_POLICY = ScrubPolicy()


# --------------------------------------------------------------------------------------------
# Detectors
#
# Each is a function over one line, yielding (detector_name, matched_value, message). The line
# loop in `scan_text` does the plumbing: line numbers, redaction, finding construction.
# --------------------------------------------------------------------------------------------

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----"
)

# Secret-shaped assignments: `password: hunter2`, `api_key=AKIA...`, `"token": "abc"`.
_ASSIGNED_SECRET_RE = re.compile(
    r"""(?ix)
    \b(
        pass(?:wd|word)? | passphrase | secret(?:_key)? | api[_-]?key | auth[_-]?token |
        access[_-]?token | private[_-]?key | client[_-]?secret | admin[_-]?pass |
        keystone[_-]?password | rabbit[_-]?password | os[_-]?password
    )\b
    ["']? \s* [:=] \s*          # the key may be quoted, as in JSON
    ["']? (?P<value> [^\s"',;#]{6,} ) ["']?
    """
)

# Values that appear in the `password:` position but are obviously not secrets.
_PLACEHOLDER_SECRETS = frozenset(
    {
        "none",
        "null",
        "true",
        "false",
        "changeme",
        "redacted",
        "removed",
        "scrubbed",
        "placeholder",
        "example",
        "<redacted>",
        "xxxxx",
        "xxxxxx",
        "********",
    }
)

_TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "aws_access_key_id",
        re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b"),
        "AWS access key id",
    ),
    ("github_token", re.compile(r"\b gh[pousr]_[A-Za-z0-9]{16,} \b", re.VERBOSE), "GitHub token"),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        "JSON web token",
    ),
    (
        "openstack_token",
        re.compile(r"\bgAAAAA[A-Za-z0-9_-]{20,}\b"),
        "Keystone fernet token",
    ),
)

_CLOUD_ID_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "aws_account_id",
        re.compile(r"(?i)\b(?:aws[_ -]?)?account(?:[_ -]?id)?\b\D{0,4}\b(?P<value>\d{12})\b"),
        "AWS account id",
    ),
    (
        "aws_arn",
        re.compile(r"\barn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:(?P<value>\d{12}):"),
        "AWS account id inside an ARN",
    ),
    (
        "azure_subscription_id",
        re.compile(
            r"(?i)\b(?:subscription|tenant|client)[_ -]?id\b\W{0,4}"
            r"(?P<value>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b"
        ),
        "Azure subscription/tenant identifier",
    ),
    (
        "gcp_project_id",
        re.compile(
            r"(?i)\b(?:project[_ -]?id|gcp[_ -]?project)\b\W{0,4}"
            r"(?P<value>[a-z][a-z0-9-]{4,28}[a-z0-9])\b"
        ),
        "GCP project identifier",
    ),
)

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@(?P<domain>[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")

_MAC_RE = re.compile(
    r"(?<![0-9A-Za-z:.-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Za-z:.-])"
)

_IPV4_RE = re.compile(r"(?<![0-9A-Za-z.])(?:\d{1,3}\.){3}\d{1,3}(?![0-9A-Za-z.])")

# Deliberately narrow: requires a run of hex groups with `::` or at least four `:` separators, so
# it does not fire on timestamps (`12:34:56`) or on MAC addresses.
_IPV6_RE = re.compile(
    r"(?<![0-9A-Za-z:.])(?:"
    r"(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
    r"|(?:[0-9A-Fa-f]{1,4}:){1,7}:(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6})?"
    r")(?![0-9A-Za-z:.])"
)

_HOSTNAME_RE = re.compile(
    r"\b(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.){1,}[A-Za-z]{2,}\b"
)

_Hit = tuple[str, str, str]  # (detector, matched value, message)


def _domain_is_safe(domain: str, policy: ScrubPolicy) -> bool:
    domain = domain.lower().rstrip(".")
    allowed = (*SAFE_DOMAIN_SUFFIXES, *(d.lower() for d in policy.allow_email_domains))
    return any(domain == suffix or domain.endswith("." + suffix) for suffix in allowed)


def _detect_private_keys(line: str, policy: ScrubPolicy) -> Iterator[_Hit]:
    if _PRIVATE_KEY_RE.search(line):
        yield ("private_key", line.strip(), "PEM private key block header")


def _detect_assigned_secrets(line: str, policy: ScrubPolicy) -> Iterator[_Hit]:
    for match in _ASSIGNED_SECRET_RE.finditer(line):
        value = match.group("value")
        if value.lower() in _PLACEHOLDER_SECRETS or policy.allows(value):
            continue
        # `password: <same as above>` style prose, and templated values, are not secrets.
        if value.startswith(("<", "{{", "${", "$(")):
            continue
        yield ("credential", value, f"{match.group(1)} assigned a literal value")


def _detect_tokens(line: str, policy: ScrubPolicy) -> Iterator[_Hit]:
    for name, pattern, description in _TOKEN_PATTERNS:
        for match in pattern.finditer(line):
            value = match.group(0)
            if policy.allows(value):
                continue
            yield (name, value, description)


def _detect_cloud_ids(line: str, policy: ScrubPolicy) -> Iterator[_Hit]:
    for name, pattern, description in _CLOUD_ID_PATTERNS:
        for match in pattern.finditer(line):
            value = match.group("value")
            if policy.allows(value):
                continue
            yield (name, value, description)


def _detect_ipv4(line: str, policy: ScrubPolicy) -> Iterator[_Hit]:
    for match in _IPV4_RE.finditer(line):
        raw = match.group(0)
        if policy.allows(raw):
            continue
        try:
            address = ipaddress.IPv4Address(raw)
        except ValueError:
            continue  # Not an address: a version string like 4.300.1.2, or similar.
        if not address.is_global:
            continue  # RFC1918, loopback, link-local, and the documentation ranges.
        yield ("ipv4", raw, "routable IPv4 address")


def _detect_ipv6(line: str, policy: ScrubPolicy) -> Iterator[_Hit]:
    for match in _IPV6_RE.finditer(line):
        raw = match.group(0)
        if policy.allows(raw):
            continue
        try:
            address = ipaddress.IPv6Address(raw)
        except ValueError:
            continue
        if not address.is_global:
            continue  # ::1, fe80::/10, fc00::/7 and 2001:db8::/32 are all safe.
        yield ("ipv6", raw, "routable IPv6 address")


def _detect_macs(line: str, policy: ScrubPolicy) -> Iterator[_Hit]:
    allowed_prefixes = (*SAFE_MAC_PREFIXES, *(p.lower() for p in policy.allow_mac_prefixes))
    for match in _MAC_RE.finditer(line):
        raw = match.group(0)
        normalised = raw.replace("-", ":").lower()
        if normalised in SAFE_MACS or policy.allows(raw):
            continue
        if any(normalised.startswith(prefix) for prefix in allowed_prefixes):
            continue
        yield ("mac_address", raw, "MAC address")


def _detect_emails(line: str, policy: ScrubPolicy) -> Iterator[_Hit]:
    for match in _EMAIL_RE.finditer(line):
        if _domain_is_safe(match.group("domain"), policy) or policy.allows(match.group(0)):
            continue
        yield ("email", match.group(0), "email address")


def _detect_customer_hosts(line: str, policy: ScrubPolicy) -> Iterator[_Hit]:
    if not policy.customer_host_patterns:
        return
    for match in _HOSTNAME_RE.finditer(line):
        host = match.group(0)
        if _domain_is_safe(host, policy) or policy.allows(host):
            continue
        if policy.is_customer_host(host):
            yield ("customer_hostname", host, "hostname matching a configured customer pattern")


_DETECTORS = (
    _detect_private_keys,
    _detect_assigned_secrets,
    _detect_tokens,
    _detect_cloud_ids,
    _detect_ipv4,
    _detect_ipv6,
    _detect_macs,
    _detect_emails,
    _detect_customer_hosts,
)


def scan_text(
    text: str,
    *,
    bug_id: str,
    file: Path,
    policy: ScrubPolicy = DEFAULT_POLICY,
) -> list[Finding]:
    """Run every detector over ``text``, returning one finding per hit."""
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for detector in _DETECTORS:
            for name, value, message in detector(line, policy):
                findings.append(
                    Finding(
                        bug_id=bug_id,
                        detector=name,
                        severity=Severity.ERROR,
                        file=file,
                        line=number,
                        message=message,
                        excerpt=redact(value),
                    )
                )
    return findings


def scan_file(
    path: Path,
    *,
    bug_id: str,
    display_path: Path | None = None,
    policy: ScrubPolicy = DEFAULT_POLICY,
) -> list[Finding]:
    """Scan one file. Binary files are reported as a warning rather than silently skipped."""
    shown = display_path or path
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [
            Finding(
                bug_id=bug_id,
                detector="binary_attachment",
                severity=Severity.WARNING,
                file=shown,
                message=(
                    "file is not UTF-8 text and was not scrubbed; review it by hand before "
                    "committing, or convert it to text"
                ),
            )
        ]
    return scan_text(text, bug_id=bug_id, file=shown, policy=policy)


def scan_paths(
    paths: Iterable[tuple[Path, Path]],
    *,
    bug_id: str,
    policy: ScrubPolicy = DEFAULT_POLICY,
) -> list[Finding]:
    """Scan many files, given ``(actual path, path to display)`` pairs."""
    findings: list[Finding] = []
    for actual, shown in paths:
        findings.extend(scan_file(actual, bug_id=bug_id, display_path=shown, policy=policy))
    return findings
