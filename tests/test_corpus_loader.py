"""Loading a bug directory, and failing loudly when it is malformed.

Every failure assertion here checks that the message names the offending *file*, because a
validation error that says only "1 validation error for GroundTruth" is useless at 11pm.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from segbench.corpus.loader import CorpusError, load_bug, load_corpus
from segbench.corpus.models import ChannelId, Origin, Product, Tracker


def test_loads_a_complete_bug(good_corpus: Path) -> None:
    bug = load_bug(good_corpus / "bugs" / "lp-9000001")

    assert bug.id == "lp-9000001"
    assert bug.manifest.product is Product.CHARMED_OPENSTACK
    assert bug.manifest.source.tracker is Tracker.LAUNCHPAD
    assert bug.ground_truth.component == "neutron"
    assert len(bug.channels) == 7
    assert ChannelId.ERROR_TRACE in bug.channel_ids
    assert bug.channel(ChannelId.CUSTOMER_REPORT) is not None
    assert bug.channel(ChannelId.CUSTOMER_REPORT).origin is Origin.SYNTHESISED
    assert "instance data service" in bug.channel(ChannelId.CUSTOMER_REPORT).text


def test_channel_inventories_differ_between_the_two_good_bugs(good_corpus: Path) -> None:
    """Leave-one-out only generates cells for channels a bug actually has (plan.md section 4)."""
    first = load_bug(good_corpus / "bugs" / "lp-9000001")
    second = load_bug(good_corpus / "bugs" / "lp-9000002")

    assert ChannelId.DEPLOYMENT_CONFIG in first.channel_ids
    assert ChannelId.DEPLOYMENT_CONFIG not in second.channel_ids
    assert ChannelId.SYSTEM_ENVIRONMENT in second.channel_ids


def test_load_corpus_finds_every_bug(good_corpus: Path) -> None:
    bugs, errors = load_corpus(good_corpus)

    assert errors == []
    assert [b.id for b in bugs] == ["lp-9000001", "lp-9000002"]


def test_load_corpus_subsets_by_id(good_corpus: Path) -> None:
    bugs, errors = load_corpus(good_corpus, bug_ids=["lp-9000002"])

    assert errors == []
    assert [b.id for b in bugs] == ["lp-9000002"]


def test_load_corpus_reports_an_unknown_bug_id(good_corpus: Path) -> None:
    bugs, errors = load_corpus(good_corpus, bug_ids=["lp-nope"])

    assert bugs == []
    assert "lp-nope" in str(errors[0])


@pytest.fixture
def scratch_bug(good_corpus: Path, tmp_path: Path) -> Path:
    """A writable copy of the first good bug, for mutating into invalid states."""
    destination = tmp_path / "bugs" / "lp-9000001"
    shutil.copytree(good_corpus / "bugs" / "lp-9000001", destination)
    return destination


def test_missing_manifest_names_the_file(scratch_bug: Path) -> None:
    (scratch_bug / "bug.yaml").unlink()

    with pytest.raises(CorpusError, match=r"bug\.yaml: required file is missing"):
        load_bug(scratch_bug)


def test_missing_ground_truth_names_the_file(scratch_bug: Path) -> None:
    (scratch_bug / "ground_truth.yaml").unlink()

    with pytest.raises(CorpusError, match=r"ground_truth\.yaml: required file is missing"):
        load_bug(scratch_bug)


def test_malformed_yaml_names_the_file(scratch_bug: Path) -> None:
    (scratch_bug / "bug.yaml").write_text("id: [unclosed\n", encoding="utf-8")

    with pytest.raises(CorpusError, match=r"bug\.yaml: not valid YAML"):
        load_bug(scratch_bug)


def test_missing_required_field_names_the_field(scratch_bug: Path) -> None:
    text = (scratch_bug / "ground_truth.yaml").read_text(encoding="utf-8")
    (scratch_bug / "ground_truth.yaml").write_text(
        text.replace("component: neutron", ""), encoding="utf-8"
    )

    with pytest.raises(CorpusError) as excinfo:
        load_bug(scratch_bug)

    assert "ground_truth.yaml" in str(excinfo.value)
    assert "component" in str(excinfo.value)


def test_unknown_field_is_rejected_rather_than_ignored(scratch_bug: Path) -> None:
    """A typo'd key that silently defaults is exactly how a corpus degrades quietly."""
    with (scratch_bug / "ground_truth.yaml").open("a", encoding="utf-8") as handle:
        handle.write("reviewd_by: typo\n")

    with pytest.raises(CorpusError, match="reviewd_by"):
        load_bug(scratch_bug)


def test_channel_outside_the_closed_vocabulary_is_rejected(scratch_bug: Path) -> None:
    text = (scratch_bug / "bug.yaml").read_text(encoding="utf-8")
    (scratch_bug / "bug.yaml").write_text(
        text.replace("id: customer_report", "id: customer_mood"), encoding="utf-8"
    )

    with pytest.raises(CorpusError, match="channels"):
        load_bug(scratch_bug)


def test_duplicate_channel_is_rejected(scratch_bug: Path) -> None:
    with (scratch_bug / "bug.yaml").open("a", encoding="utf-8") as handle:
        handle.write("")
    text = (scratch_bug / "bug.yaml").read_text(encoding="utf-8")
    (scratch_bug / "bug.yaml").write_text(
        text.replace("id: engineer_notes", "id: customer_report"), encoding="utf-8"
    )

    with pytest.raises(CorpusError, match="repeated channel id"):
        load_bug(scratch_bug)


def test_channel_referencing_a_nonexistent_file_is_rejected(scratch_bug: Path) -> None:
    (scratch_bug / "channels" / "error_trace.txt").unlink()

    with pytest.raises(CorpusError, match=r"error_trace.*does not exist"):
        load_bug(scratch_bug)


def test_empty_channel_file_is_rejected(scratch_bug: Path) -> None:
    (scratch_bug / "channels" / "error_trace.txt").write_text("\n\n", encoding="utf-8")

    with pytest.raises(CorpusError, match="is empty"):
        load_bug(scratch_bug)


def test_channel_escaping_the_bug_directory_is_rejected(scratch_bug: Path) -> None:
    text = (scratch_bug / "bug.yaml").read_text(encoding="utf-8")
    (scratch_bug / "bug.yaml").write_text(
        text.replace("channels/error_trace.txt", "../../../etc/hostname"), encoding="utf-8"
    )

    with pytest.raises(CorpusError, match="escapes the bug directory"):
        load_bug(scratch_bug)


def test_id_must_match_the_directory_name(scratch_bug: Path) -> None:
    text = (scratch_bug / "bug.yaml").read_text(encoding="utf-8")
    (scratch_bug / "bug.yaml").write_text(
        text.replace("id: lp-9000001", "id: lp-9999999", 1), encoding="utf-8"
    )

    with pytest.raises(CorpusError, match="but the directory is named"):
        load_bug(scratch_bug)


def test_launchpad_bug_must_have_a_url(scratch_bug: Path) -> None:
    text = (scratch_bug / "bug.yaml").read_text(encoding="utf-8")
    (scratch_bug / "bug.yaml").write_text(
        text.replace("url: https://bugs.launchpad.net/charm-neutron-api/+bug/9000001", "url: null"),
        encoding="utf-8",
    )

    with pytest.raises(CorpusError, match=r"source\.url is required"):
        load_bug(scratch_bug)


# --- readiness ---------------------------------------------------------------------------------


def test_bug_without_a_reviewer_loads_but_is_not_ready(scratch_bug: Path) -> None:
    text = (scratch_bug / "ground_truth.yaml").read_text(encoding="utf-8")
    (scratch_bug / "ground_truth.yaml").write_text(
        text.replace("reviewed_by: fixture-maintainer", "reviewed_by: null"), encoding="utf-8"
    )

    bug = load_bug(scratch_bug)

    assert bug.ready is False
    assert "reviewed_by" in (bug.not_ready_reason or "")


def test_unreviewed_bugs_are_excluded_from_campaigns_by_default(
    scratch_bug: Path, tmp_path: Path
) -> None:
    text = (scratch_bug / "ground_truth.yaml").read_text(encoding="utf-8")
    (scratch_bug / "ground_truth.yaml").write_text(
        text.replace("reviewed_by: fixture-maintainer", "reviewed_by: null"), encoding="utf-8"
    )

    included, _ = load_corpus(tmp_path, include_unreviewed=True)
    excluded, _ = load_corpus(tmp_path, include_unreviewed=False)

    assert [b.id for b in included] == ["lp-9000001"]
    assert excluded == []


def test_ready_bug_survives_the_campaign_filter(good_corpus: Path) -> None:
    bugs, _ = load_corpus(good_corpus, include_unreviewed=False)

    assert [b.id for b in bugs] == ["lp-9000001", "lp-9000002"]
