"""The corpus schemas themselves (plan.md sections 3.2, 3.3, 4)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from segbench.corpus.models import (
    Bug,
    BugManifest,
    ChannelId,
    FixInfo,
    GroundTruth,
    Provenance,
)

MANIFEST = {
    "id": "lp-1",
    "title": "Something breaks",
    "product": "juju",
    "source": {"tracker": "launchpad", "url": "https://x", "reported_at": "2024-01-05T00:00:00Z"},
    "repo": {"url": "https://opendev.org/openstack/nova", "pre_fix_ref": "7f3a9c1abc"},
    "channels": [{"id": "error_trace", "file": "channels/t.txt", "origin": "verbatim"}],
}

TRUTH = {
    "root_cause": "It is wrong.",
    "component": "nova",
    "fix": {"commit": "9b2e0f4abc", "url": "https://x/commit/9b2e0f4"},
}


def test_channel_vocabulary_is_closed() -> None:
    """Adding a member is a schema change requiring migration (plan.md section 4)."""
    assert {c.value for c in ChannelId} == {
        "customer_report",
        "engineer_notes",
        "error_trace",
        "version_manifest",
        "service_logs",
        "deployment_config",
        "reproduction_steps",
        "system_environment",
    }


def test_manifest_round_trips() -> None:
    manifest = BugManifest.model_validate(MANIFEST)

    assert manifest.id == "lp-1"
    assert manifest.channels[0].id is ChannelId.ERROR_TRACE
    assert manifest.tags == []
    assert manifest.difficulty_hint is None


def test_reproduction_may_omit_the_url() -> None:
    data = {
        **MANIFEST,
        "source": {"tracker": "reproduction", "url": None, "reported_at": "2024-01-05T00:00:00Z"},
    }

    assert BugManifest.model_validate(data).source.url is None


def test_a_bug_needs_at_least_one_channel() -> None:
    with pytest.raises(ValidationError, match="channels"):
        BugManifest.model_validate({**MANIFEST, "channels": []})


def test_unknown_manifest_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="severity"):
        BugManifest.model_validate({**MANIFEST, "severity": "critical"})


def test_ground_truth_defaults_to_authored_and_unreviewed() -> None:
    truth = GroundTruth.model_validate(TRUTH)

    assert truth.provenance is Provenance.AUTHORED
    assert truth.reviewed_by is None
    assert truth.fix.files == []
    assert truth.fix.symbols is None


def test_acceptable_components_extend_the_component() -> None:
    truth = GroundTruth.model_validate({**TRUTH, "acceptable_components": ["nova-compute"]})

    assert truth.components == ["nova", "nova-compute"]


def test_symbols_distinguish_null_from_empty() -> None:
    """Null means "no extractor for this language"; empty means "the fix touched no symbol"."""
    assert FixInfo(commit="abcdefg").symbols is None
    assert FixInfo(commit="abcdefg", symbols=[]).symbols == []


def test_short_commit_is_rejected() -> None:
    with pytest.raises(ValidationError, match="commit"):
        FixInfo(commit="abc")


def test_readiness_follows_reviewed_by(tmp_path) -> None:
    common = {
        "directory": tmp_path,
        "manifest": BugManifest.model_validate(MANIFEST),
        "channels": [],
    }

    unreviewed = Bug(**common, ground_truth=GroundTruth.model_validate(TRUTH))
    reviewed = Bug(
        **common, ground_truth=GroundTruth.model_validate({**TRUTH, "reviewed_by": "me"})
    )

    assert unreviewed.ready is False
    assert "reviewed_by" in (unreviewed.not_ready_reason or "")
    assert reviewed.ready is True
    assert reviewed.not_ready_reason is None
