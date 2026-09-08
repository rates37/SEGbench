"""The anti-leak checks (plan.md section 3.4).

Two hard failures — the fix commit and the fix/review URL — and one warning, the similarity flag.
The severity split is the point: a hash is proof, an overlap score is a suspicion for a human.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from segbench.corpus.findings import Severity
from segbench.corpus.leakage import check_bug, check_channel, similarity
from segbench.corpus.loader import load_bug

FIX = "9b2e0f43a1c7d8e5f6021b4c8ad39e7f0c5a2b16"
FIX_URL = "https://opendev.org/openstack/neutron/commit/9b2e0f43a1c7"
ROOT_CAUSE = "The reconnect path skips the datapath cache refresh, leaving proxies misconfigured."


def check(text: str, **kwargs: object) -> list[str]:
    findings = check_channel(
        bug_id="b",
        file=Path("f"),
        text=text,
        fix_commit=kwargs.pop("fix_commit", FIX),
        fix_url=kwargs.pop("fix_url", FIX_URL),
        root_cause=kwargs.pop("root_cause", ROOT_CAUSE),
        **kwargs,
    )
    return [f.detector for f in findings]


# --- fix commit --------------------------------------------------------------------------------


def test_full_fix_hash_is_a_hard_failure() -> None:
    assert "fix_commit_reference" in check(f"landed as {FIX}")


@pytest.mark.parametrize("length", [7, 8, 12, 20, 40])
def test_abbreviated_fix_hash_is_a_hard_failure(length: int) -> None:
    assert "fix_commit_reference" in check(f"see commit {FIX[:length]} for details")


def test_abbreviation_shorter_than_seven_is_not_flagged() -> None:
    """Git's own default abbreviation is 7; below that a hex run is not a commit reference."""
    assert "fix_commit_reference" not in check(f"see {FIX[:6]} for details")


def test_an_unrelated_hash_is_not_flagged() -> None:
    assert "fix_commit_reference" not in check("parent is 4c1de8a90b3f27ee5d0a1cb7f9a2e6d4831bb05c")


def test_fix_commit_finding_is_an_error() -> None:
    findings = check_channel(
        bug_id="b",
        file=Path("f"),
        text=f"fixed by {FIX[:7]}",
        fix_commit=FIX,
        fix_url=None,
        root_cause=ROOT_CAUSE,
    )

    assert findings[0].severity is Severity.ERROR
    assert findings[0].line == 1


# --- fix and review URLs -----------------------------------------------------------------------


def test_fix_url_is_a_hard_failure() -> None:
    assert "fix_url_reference" in check(f"the patch is at {FIX_URL}")


def test_fix_url_is_matched_without_its_scheme() -> None:
    assert "fix_url_reference" in check("see opendev.org/openstack/neutron/commit/9b2e0f43a1c7")


@pytest.mark.parametrize(
    "line",
    [
        "https://review.opendev.org/c/openstack/neutron/+/908812",
        "https://github.com/juju/juju/pull/16712",
        "https://github.com/juju/juju/commit/abcdef1234",
        "https://code.launchpad.net/~user/charm/+git/x/+merge/44012",
        "Change-Id: I1234567890abcdef1234567890abcdef12345678",
        "attached 0001-fix-the-thing.patch",
    ],
)
def test_review_and_patch_links_are_hard_failures(line: str) -> None:
    assert "review_url_reference" in check(line)


def test_the_bug_tracker_url_itself_is_not_a_review_link() -> None:
    """A channel may legitimately be sourced from the tracker; only the *fix* is forbidden."""
    assert "review_url_reference" not in check(
        "reported at bugs.launchpad.net/neutron/+bug/9000001"
    )


# --- similarity --------------------------------------------------------------------------------


def test_similarity_is_a_warning_not_a_failure() -> None:
    findings = check_channel(
        bug_id="b",
        file=Path("f"),
        text=ROOT_CAUSE,
        fix_commit=FIX,
        fix_url=None,
        root_cause=ROOT_CAUSE,
    )

    assert [f.detector for f in findings] == ["root_cause_similarity"]
    assert findings[0].severity is Severity.WARNING


def test_verbatim_root_cause_scores_one() -> None:
    assert similarity(ROOT_CAUSE, ROOT_CAUSE) == pytest.approx(1.0)


def test_unrelated_text_scores_zero() -> None:
    assert similarity("the kettle boiled and nobody noticed", ROOT_CAUSE) == pytest.approx(0.0)


def test_similarity_ignores_stopwords() -> None:
    """Otherwise every channel looks similar to every root cause and the check is noise."""
    assert similarity("the and of it was a bug error failure", ROOT_CAUSE) == pytest.approx(0.0)


def test_similarity_is_containment_so_a_long_channel_cannot_dilute_a_leak() -> None:
    padding = " ".join(f"unrelated{n}" for n in range(500))

    assert similarity(f"{padding} {ROOT_CAUSE}", ROOT_CAUSE) == pytest.approx(1.0)


def test_threshold_is_configurable() -> None:
    text = "The reconnect path skips something."

    assert "root_cause_similarity" not in check(text, similarity_threshold=0.9)
    assert "root_cause_similarity" in check(text, similarity_threshold=0.2)


# --- against the fixtures ----------------------------------------------------------------------


def test_good_bugs_have_no_leaks(good_corpus: Path) -> None:
    for bug_id in ("lp-9000001", "lp-9000002"):
        findings = check_bug(load_bug(good_corpus / "bugs" / bug_id))

        assert findings == [], f"{bug_id} leaked: {[str(f) for f in findings]}"


def test_leaky_fixture_trips_every_leak_detector(leaky_corpus: Path) -> None:
    findings = check_bug(load_bug(leaky_corpus / "bugs" / "lp-9000003"))
    detectors = {f.detector for f in findings}

    assert detectors == {
        "fix_commit_reference",
        "fix_url_reference",
        "review_url_reference",
        "root_cause_similarity",
    }
