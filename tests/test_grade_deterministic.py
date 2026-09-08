"""Path normalisation, component matching, file F1 and symbol matching (plan.md section 7.1)."""

from __future__ import annotations

import pytest

from segbench.agent.schema import Answer
from segbench.corpus.models import FixInfo, GroundTruth, Provenance
from segbench.grade.deterministic import (
    component_match,
    file_f1,
    file_hit,
    normalize_path,
    paths_equivalent,
    score_deterministic,
    symbol_hit,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("neutron/agent/ovn/metadata/agent.py", "neutron/agent/ovn/metadata/agent.py"),
        ("/workspace/neutron/agent/ovn/metadata/agent.py", "neutron/agent/ovn/metadata/agent.py"),
        ("/root/repo/neutron/agent/ovn/metadata/agent.py", "neutron/agent/ovn/metadata/agent.py"),
        ("a/neutron/agent/ovn/metadata/agent.py", "neutron/agent/ovn/metadata/agent.py"),
        ("b/neutron/agent/ovn/metadata/agent.py", "neutron/agent/ovn/metadata/agent.py"),
        ("neutron/agent/ovn/metadata/agent.py:142", "neutron/agent/ovn/metadata/agent.py"),
        ("neutron/agent/ovn/metadata/agent.py:142:8", "neutron/agent/ovn/metadata/agent.py"),
        ("neutron/agent/ovn/metadata/agent.py#L142", "neutron/agent/ovn/metadata/agent.py"),
        ("neutron/agent/ovn/metadata/agent.py#L142-L150", "neutron/agent/ovn/metadata/agent.py"),
        ("./neutron/agent/ovn/metadata/agent.py", "neutron/agent/ovn/metadata/agent.py"),
        ("`neutron/agent/ovn/metadata/agent.py`", "neutron/agent/ovn/metadata/agent.py"),
        (
            "/home/ubuntu/repo/neutron/agent/ovn/metadata/agent.py",
            "neutron/agent/ovn/metadata/agent.py",
        ),
        ("  ", ""),
        ("", ""),
    ],
)
def test_normalize_path_messy_cases(raw: str, expected: str) -> None:
    assert normalize_path(raw) == expected


def test_paths_equivalent_across_prefixes() -> None:
    assert paths_equivalent(
        "/root/repo/neutron/agent/ovn/metadata/agent.py",
        "neutron/agent/ovn/metadata/agent.py:142",
    )


def test_paths_equivalent_requires_matching_suffix() -> None:
    assert not paths_equivalent("neutron/agent/nic.py", "neutron/agent/ovn/metadata/agent.py")


def test_paths_equivalent_bare_filename_matches_bare_filename() -> None:
    assert paths_equivalent("agent.py", "agent.py")


def _answer(**overrides) -> Answer:
    defaults = dict(
        root_cause="x",
        component="neutron",
        suspect_files=[],
        suspect_symbols=[],
        proposed_fix="y",
        confidence=0.5,
        evidence=[],
        uncertain_about=[],
    )
    defaults.update(overrides)
    return Answer(**defaults)


def _ground_truth(**overrides) -> GroundTruth:
    defaults = dict(
        root_cause="the real root cause",
        component="neutron",
        fix=FixInfo(
            commit="0123456789abcdef",
            files=["neutron/agent/ovn/metadata/agent.py"],
            symbols=["MetadataAgent.sync"],
        ),
        acceptable_components=["neutron-api"],
        also_acceptable_root_causes=[],
        provenance=Provenance.AUTHORED,
    )
    defaults.update(overrides)
    return GroundTruth(**defaults)


def test_component_match_exact() -> None:
    assert component_match(_answer(component="neutron"), _ground_truth())


def test_component_match_alias_and_case_insensitive() -> None:
    assert component_match(_answer(component="Neutron-API"), _ground_truth())


def test_component_match_rejects_unrelated() -> None:
    assert not component_match(_answer(component="nova"), _ground_truth())


def test_file_hit_true_with_prefix_mismatch() -> None:
    answer = _answer(suspect_files=["/root/repo/neutron/agent/ovn/metadata/agent.py:99"])
    assert file_hit(answer, _ground_truth())


def test_file_hit_false_when_no_overlap() -> None:
    answer = _answer(suspect_files=["neutron/agent/linux/dhcp.py"])
    assert not file_hit(answer, _ground_truth())


def test_file_f1_perfect_match() -> None:
    answer = _answer(suspect_files=["neutron/agent/ovn/metadata/agent.py"])
    result = file_f1(answer, _ground_truth())
    assert result.precision == 1.0
    assert result.recall == 1.0
    assert result.f1 == 1.0


def test_file_f1_penalises_padding() -> None:
    """Forty guesses to guarantee a hit should not score like two well-chosen files."""
    padded = _answer(
        suspect_files=[
            "neutron/agent/ovn/metadata/agent.py",
            *[f"neutron/junk_{i}.py" for i in range(39)],
        ]
    )
    precise = _answer(suspect_files=["neutron/agent/ovn/metadata/agent.py"])
    padded_f1 = file_f1(padded, _ground_truth()).f1
    precise_f1 = file_f1(precise, _ground_truth()).f1
    assert padded_f1 < precise_f1
    assert precise_f1 == 1.0


def test_file_f1_no_prediction_scores_zero() -> None:
    result = file_f1(_answer(suspect_files=[]), _ground_truth())
    assert result.precision == 0.0
    assert result.recall == 0.0
    assert result.f1 == 0.0


def test_symbol_hit_true() -> None:
    answer = _answer(suspect_symbols=["MetadataAgent.sync"])
    assert symbol_hit(answer, _ground_truth()) is True


def test_symbol_hit_matches_last_component_case_insensitive() -> None:
    answer = _answer(suspect_symbols=["metadataagent.Sync()"])
    assert symbol_hit(answer, _ground_truth()) is True


def test_symbol_hit_false_when_no_overlap() -> None:
    answer = _answer(suspect_symbols=["OtherClass.other_method"])
    assert symbol_hit(answer, _ground_truth()) is False


def test_symbol_hit_null_when_ground_truth_has_no_symbols() -> None:
    gt = _ground_truth(fix=FixInfo(commit="0123456789abcdef", files=["README.md"], symbols=None))
    answer = _answer(suspect_symbols=["anything"])
    assert symbol_hit(answer, gt) is None


def test_score_deterministic_bundles_all_checks() -> None:
    answer = _answer(
        component="neutron",
        suspect_files=["neutron/agent/ovn/metadata/agent.py"],
        suspect_symbols=["MetadataAgent.sync"],
    )
    result = score_deterministic(answer, _ground_truth())
    assert result.component_match is True
    assert result.file_hit is True
    assert result.file_f1 == 1.0
    assert result.symbol_hit is True
