"""Tests for the answer.json schema and its validator (plan.md section 2)."""

from __future__ import annotations

import json

from segbench.agent.schema import ANSWER_JSON_SCHEMA, Answer, AnswerOutcome, validate_answer_text

VALID_ANSWER = {
    "root_cause": "A race between two threads corrupts shared state.",
    "component": "nova",
    "suspect_files": ["nova/virt/libvirt/driver.py"],
    "suspect_symbols": ["LibvirtDriver._get_guest_config"],
    "proposed_fix": "Take the lock before reading the shared dict.",
    "confidence": 0.6,
    "evidence": ["error_trace"],
    "uncertain_about": ["whether this reproduces on the reporter's exact version"],
}


def test_valid_answer_parses_ok() -> None:
    result = validate_answer_text(json.dumps(VALID_ANSWER))
    assert result.ok
    assert result.outcome is AnswerOutcome.OK
    assert isinstance(result.answer, Answer)
    assert result.answer.component == "nova"


def test_missing_file_is_no_answer() -> None:
    result = validate_answer_text(None)
    assert result.outcome is AnswerOutcome.NO_ANSWER
    assert result.answer is None


def test_empty_file_is_no_answer() -> None:
    result = validate_answer_text("   \n")
    assert result.outcome is AnswerOutcome.NO_ANSWER


def test_unparseable_json_is_invalid_answer() -> None:
    result = validate_answer_text("{not json")
    assert result.outcome is AnswerOutcome.INVALID_ANSWER
    assert "not valid JSON" in (result.reason or "")


def test_non_object_json_is_invalid_answer() -> None:
    result = validate_answer_text("[1, 2, 3]")
    assert result.outcome is AnswerOutcome.INVALID_ANSWER


def test_schema_violation_is_invalid_answer() -> None:
    bad = dict(VALID_ANSWER)
    bad["confidence"] = 2.0  # out of [0, 1]
    result = validate_answer_text(json.dumps(bad))
    assert result.outcome is AnswerOutcome.INVALID_ANSWER
    assert "confidence" in (result.reason or "")


def test_extra_key_is_rejected() -> None:
    bad = dict(VALID_ANSWER)
    bad["extra_field"] = "surprise"
    result = validate_answer_text(json.dumps(bad))
    assert result.outcome is AnswerOutcome.INVALID_ANSWER


def test_missing_required_field_is_invalid() -> None:
    bad = dict(VALID_ANSWER)
    del bad["root_cause"]
    result = validate_answer_text(json.dumps(bad))
    assert result.outcome is AnswerOutcome.INVALID_ANSWER


def test_json_schema_has_expected_properties() -> None:
    props = ANSWER_JSON_SCHEMA["properties"]
    for field in (
        "root_cause",
        "component",
        "suspect_files",
        "suspect_symbols",
        "proposed_fix",
        "confidence",
        "evidence",
        "uncertain_about",
    ):
        assert field in props
