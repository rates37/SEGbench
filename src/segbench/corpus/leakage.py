"""Anti-leak checks over a bug's channels (plan.md section 3.4).

The benchmark's first invariant is that the agent can never observe the fix. Most of that is
enforced structurally — network policy and truncated git history — but the channels are authored by
hand from tracker text, and tracker text is full of "fixed by I8a3f...", review links, and comments
where a developer states the resolved cause outright. Those leaks travel in the corpus itself,
where no proxy can stop them, so they are caught here.

Three checks, two of which are hard failures:

* **fix commit hash** — the full hash or any abbreviation of 7 or more characters. Hard failure.
* **fix or review URL** — the recorded fix URL, plus anything shaped like a code review or patch
  link on the forges these products use. Hard failure.
* **root-cause similarity** — token overlap between a channel and ``ground_truth.root_cause``. A
  warning, not a failure: high overlap is *suspicious*, not proof, since a channel legitimately
  shares vocabulary with the diagnosis. It goes to a human.
"""

from __future__ import annotations

import re
from pathlib import Path

from segbench.corpus.findings import Finding, Severity
from segbench.corpus.models import Bug

#: Default token-containment above which a channel is flagged for human review.
DEFAULT_SIMILARITY_THRESHOLD = 0.5

#: Minimum abbreviation length treated as a commit reference. Git's own default abbreviation is 7.
MIN_ABBREV = 7

_HEX_RUN_RE = re.compile(rf"\b[0-9a-fA-F]{{{MIN_ABBREV},40}}\b")

# Anything shaped like a code review, merge proposal or patch link on the forges these products
# use. Matched case-insensitively against the raw channel text.
_REVIEW_URL_RE = re.compile(
    r"""(?ix)
    \b(?:https?://)?(?:
        review\.opendev\.org \S*
      | review\.openstack\.org \S*
      | gerrit\.\S+
      | \S*github\.com/\S+/(?:pull|commit)/\S+
      | \S*gitlab\.\S+/\S+/-/(?:merge_requests|commit)/\S+
      | \S*opendev\.org/\S+/commit/\S+
      | \S*git\.launchpad\.net/\S+/commit/\S+
      | code\.launchpad\.net/\S*merge\S*
      | patchwork\.\S+
      | \S*\.patch\b
      | \bI[0-9a-f]{40}\b          # Gerrit Change-Id
    )
    """
)

# Tokens shared by every OpenStack-shaped text; counting them would make every channel look
# similar to every root cause.
_STOPWORD_TEXT = """
a an and are as at be because been but by can could did do does for from had has have how if
in into is it its not of on or should so than that the their then there these they this to
was were when where which while who why will with would you your
after before during under over thus hence also only just very more most such no nor
bug issue error fails failed failure problem occurs happens cause caused causes result results
"""

_STOPWORDS = frozenset(_STOPWORD_TEXT.split())

_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _tokenise(text: str) -> set[str]:
    """Lowercase content tokens of three or more characters, stopwords removed."""
    return {
        token
        for token in _TOKEN_RE.findall(text.lower())
        if len(token) >= 3 and token not in _STOPWORDS
    }


def similarity(channel_text: str, root_cause: str) -> float:
    """Fraction of the root cause's content vocabulary that appears in the channel.

    Containment rather than Jaccard, and oriented this way round deliberately: the question is
    "how much of the answer is sitting in this channel", and a Jaccard score would let a long
    channel dilute a total leak down to a harmless-looking number.
    """
    truth_tokens = _tokenise(root_cause)
    if not truth_tokens:
        return 0.0
    channel_tokens = _tokenise(channel_text)
    return len(truth_tokens & channel_tokens) / len(truth_tokens)


def _normalise_url(url: str) -> str:
    return re.sub(r"^https?://", "", url.strip().lower()).rstrip("/")


def _commit_hash_hits(text: str, commit: str) -> list[tuple[int, str]]:
    """Return ``(line number, matched text)`` for every reference to ``commit``."""
    commit = commit.strip().lower()
    hits: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in _HEX_RUN_RE.finditer(line):
            candidate = match.group(0).lower()
            # An abbreviation of the fix, or the fix quoted at greater length than we recorded.
            if commit.startswith(candidate) or candidate.startswith(commit):
                hits.append((number, match.group(0)))
    return hits


def _url_hits(text: str, needle: str) -> list[tuple[int, str]]:
    needle = _normalise_url(needle)
    if not needle:
        return []
    return [
        (number, needle)
        for number, line in enumerate(text.splitlines(), start=1)
        if needle in _normalise_url(line) or needle in line.lower()
    ]


def check_channel(
    *,
    bug_id: str,
    file: Path,
    text: str,
    fix_commit: str,
    fix_url: str | None,
    root_cause: str,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> list[Finding]:
    """Run all three leak checks over one channel's text."""
    findings: list[Finding] = []

    for line, matched in _commit_hash_hits(text, fix_commit):
        findings.append(
            Finding(
                bug_id=bug_id,
                detector="fix_commit_reference",
                severity=Severity.ERROR,
                file=file,
                line=line,
                message=(
                    f"channel references the fix commit {fix_commit[:12]} "
                    f"(as {matched!r}); the agent must never be able to see the fix"
                ),
                excerpt=matched,
            )
        )

    if fix_url:
        for line, matched in _url_hits(text, fix_url):
            findings.append(
                Finding(
                    bug_id=bug_id,
                    detector="fix_url_reference",
                    severity=Severity.ERROR,
                    file=file,
                    line=line,
                    message="channel contains ground_truth.fix.url",
                    excerpt=matched,
                )
            )

    for number, line in enumerate(text.splitlines(), start=1):
        for match in _REVIEW_URL_RE.finditer(line):
            findings.append(
                Finding(
                    bug_id=bug_id,
                    detector="review_url_reference",
                    severity=Severity.ERROR,
                    file=file,
                    line=number,
                    message=(
                        "channel cites a code review, merge proposal or patch link; strip it "
                        "(plan.md section 3.4, 'never let a channel cite the fix')"
                    ),
                    excerpt=match.group(0)[:80],
                )
            )

    score = similarity(text, root_cause)
    if score >= similarity_threshold:
        findings.append(
            Finding(
                bug_id=bug_id,
                detector="root_cause_similarity",
                severity=Severity.WARNING,
                file=file,
                message=(
                    f"{score:.0%} of ground_truth.root_cause's vocabulary appears in this channel "
                    f"(threshold {similarity_threshold:.0%}); review by hand — a channel must "
                    f"describe symptoms, not state the resolved cause"
                ),
            )
        )

    return findings


def check_bug(
    bug: Bug,
    *,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> list[Finding]:
    """Run the leak checks over every channel of one loaded bug."""
    findings: list[Finding] = []
    for channel in bug.channels:
        findings.extend(
            check_channel(
                bug_id=bug.id,
                file=channel.path,
                text=channel.text,
                fix_commit=bug.ground_truth.fix.commit,
                fix_url=bug.ground_truth.fix.url,
                root_cause=bug.ground_truth.root_cause,
                similarity_threshold=similarity_threshold,
            )
        )
    return findings
