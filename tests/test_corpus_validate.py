"""End-to-end corpus validation, including the `segbench corpus validate` command.

This is the phase-1 acceptance check: both good fixtures validate clean, and both bad fixtures fail
with the specific detector they were built to trip.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from typer.testing import CliRunner

from segbench.cli import app
from segbench.corpus.findings import Severity
from segbench.corpus.scrub import ScrubPolicy
from segbench.corpus.validate import validate_corpus

runner = CliRunner()

#: The customer naming convention the unscrubbed fixture deliberately violates.
ACME_HOST_PATTERN = r"\.acmecorp\.internal$"

ACME_POLICY = ScrubPolicy(customer_host_patterns=(ACME_HOST_PATTERN,))


# --- the good fixtures -------------------------------------------------------------------------


def test_good_corpus_validates_clean(good_corpus: Path) -> None:
    report = validate_corpus(good_corpus, policy=ACME_POLICY)

    assert report.load_errors == []
    assert report.ok, [str(f) for bug in report.bugs for f in bug.errors]
    assert report.error_count == 0
    assert report.warning_count == 0
    assert [b.bug_id for b in report.bugs] == ["lp-9000001", "lp-9000002"]
    assert all(b.ready for b in report.bugs)
    assert all(b.channel_count >= 6 for b in report.bugs)


# --- the leaky fixture -------------------------------------------------------------------------


def test_leaky_corpus_fails_on_the_leak_detectors(leaky_corpus: Path) -> None:
    report = validate_corpus(leaky_corpus, policy=ACME_POLICY)

    assert not report.ok
    errors = {f.detector for bug in report.bugs for f in bug.errors}
    assert errors == {"fix_commit_reference", "fix_url_reference", "review_url_reference"}


def test_leaky_corpus_flags_the_restated_root_cause_as_a_warning(leaky_corpus: Path) -> None:
    report = validate_corpus(leaky_corpus, policy=ACME_POLICY)

    warnings = [f for bug in report.bugs for f in bug.warnings]
    assert [f.detector for f in warnings] == ["root_cause_similarity"]
    assert warnings[0].severity is Severity.WARNING
    assert warnings[0].file.name == "customer_report.md"


# --- the unscrubbed fixture --------------------------------------------------------------------


def test_unscrubbed_corpus_fails_on_the_scrubber(unscrubbed_corpus: Path) -> None:
    report = validate_corpus(unscrubbed_corpus, policy=ACME_POLICY)

    assert not report.ok
    detectors = {f.detector for bug in report.bugs for f in bug.errors}
    assert detectors == {
        "customer_hostname",
        "email",
        "mac_address",
        "ipv4",
        "ipv6",
        "credential",
        "aws_access_key_id",
        "aws_account_id",
        "private_key",
    }


def test_unscrubbed_corpus_does_not_flag_its_safe_values(unscrubbed_corpus: Path) -> None:
    """The fixture deliberately mixes in documentation ranges and reserved values."""
    report = validate_corpus(unscrubbed_corpus, policy=ACME_POLICY)
    excerpts = " ".join(f.excerpt or "" for bug in report.bugs for f in bug.errors)

    for safe in ("127", "192", "ff:", "fe8", "198"):
        assert not excerpts.startswith(safe)
    assert len([f for bug in report.bugs for f in bug.errors if f.detector == "ipv4"]) == 1


def test_customer_hostname_is_only_flagged_when_configured(unscrubbed_corpus: Path) -> None:
    report = validate_corpus(unscrubbed_corpus)  # default policy: no customer patterns

    detectors = {f.detector for bug in report.bugs for f in bug.errors}
    assert "customer_hostname" not in detectors


# --- attachments and readiness -------------------------------------------------------------


def test_attachments_are_scrubbed_too(good_corpus: Path, tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    shutil.copytree(good_corpus, corpus)
    attachments = corpus / "bugs" / "lp-9000001" / "attachments"
    attachments.mkdir()
    (attachments / "sosreport-excerpt.txt").write_text(
        "inet 91.189.88.152/24 brd\n", encoding="utf-8"
    )

    report = validate_corpus(corpus, policy=ACME_POLICY)

    assert not report.ok
    assert [f.detector for bug in report.bugs for f in bug.errors] == ["ipv4"]


def test_unreviewed_ground_truth_is_a_warning_not_a_failure(
    good_corpus: Path, tmp_path: Path
) -> None:
    corpus = tmp_path / "corpus"
    shutil.copytree(good_corpus, corpus)
    truth = corpus / "bugs" / "lp-9000002" / "ground_truth.yaml"
    truth.write_text(
        truth.read_text(encoding="utf-8").replace(
            "reviewed_by: fixture-maintainer", "reviewed_by: null"
        ),
        encoding="utf-8",
    )

    report = validate_corpus(corpus, policy=ACME_POLICY)

    assert report.ok
    assert [f.detector for bug in report.bugs for f in bug.warnings] == ["not_ready"]


def test_a_bug_that_will_not_load_fails_the_pass(good_corpus: Path, tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    shutil.copytree(good_corpus, corpus)
    (corpus / "bugs" / "lp-9000001" / "ground_truth.yaml").unlink()

    report = validate_corpus(corpus, policy=ACME_POLICY)

    assert not report.ok
    assert len(report.load_errors) == 1
    assert "ground_truth.yaml" in report.load_errors[0]


def test_bug_ids_subset_the_pass(good_corpus: Path) -> None:
    report = validate_corpus(good_corpus, bug_ids=["lp-9000001"], policy=ACME_POLICY)

    assert [b.bug_id for b in report.bugs] == ["lp-9000001"]


# --- the CLI -----------------------------------------------------------------------------------


def _invoke(corpus: Path, tmp_path: Path, *extra: str, patterns: str = "[]") -> object:
    config = tmp_path / "segbench.toml"
    config.write_text(
        f'[paths]\ncorpus = "{corpus}"\n\n[corpus]\ncustomer_host_patterns = {patterns}\n',
        encoding="utf-8",
    )
    return runner.invoke(app, ["--config", str(config), "corpus", "validate", *extra])


def test_cli_exits_zero_on_a_clean_corpus(good_corpus: Path, tmp_path: Path) -> None:
    result = _invoke(good_corpus, tmp_path)

    assert result.exit_code == 0, result.output
    assert "OK" in result.output
    assert "lp-9000001" in result.output


def test_cli_exits_non_zero_on_a_leak(leaky_corpus: Path, tmp_path: Path) -> None:
    result = _invoke(leaky_corpus, tmp_path)

    assert result.exit_code == 1
    assert "FAILED" in result.output
    assert "fix_commit_reference" in result.output


def test_cli_exits_non_zero_on_an_unscrubbed_bug(unscrubbed_corpus: Path, tmp_path: Path) -> None:
    # A TOML *literal* string, so the regex's backslashes survive unescaped.
    result = _invoke(unscrubbed_corpus, tmp_path, patterns=f"['{ACME_HOST_PATTERN}']")

    assert result.exit_code == 1
    assert "private_key" in result.output
    assert "customer_hostname" in result.output


def test_cli_bug_option_subsets(good_corpus: Path, tmp_path: Path) -> None:
    result = _invoke(good_corpus, tmp_path, "--bug", "lp-9000002")

    assert result.exit_code == 0
    assert "lp-9000002" in result.output
    assert "lp-9000001" not in result.output
