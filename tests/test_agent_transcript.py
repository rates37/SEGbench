"""Tests for the opencode session-export normaliser (plan.md section 10)."""

from __future__ import annotations

import json

import pytest

from segbench.agent.transcript import TranscriptError, extract_transcript

GOOD_EXPORT = {
    "info": {"id": "ses_1"},
    "messages": [
        {
            "info": {"id": "msg_1", "role": "user", "time": {"created": 1000}},
            "parts": [{"type": "text", "text": "diagnose this bug"}],
        },
        {
            "info": {
                "id": "msg_2",
                "role": "assistant",
                "time": {"created": 2000},
                "tokens": {"input": 120, "output": 45},
            },
            "parts": [
                {"type": "step-start"},
                {"type": "text", "text": "Let me look at the trace."},
                {
                    "type": "tool",
                    "tool": "bash",
                    "state": {"input": {"command": "grep -r foo"}, "output": "no matches"},
                },
                {"type": "text", "text": " Then the answer."},
            ],
        },
    ],
}


def test_extracts_messages_with_roles_and_content() -> None:
    messages = extract_transcript(json.dumps(GOOD_EXPORT))
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[0].content == "diagnose this bug"
    assert messages[1].content == "Let me look at the trace. Then the answer."


def test_extracts_tool_calls() -> None:
    messages = extract_transcript(json.dumps(GOOD_EXPORT))
    tool_calls = messages[1].tool_calls
    assert len(tool_calls) == 1
    assert tool_calls[0].tool == "bash"
    assert tool_calls[0].output == "no matches"


def test_extracts_tokens_and_timestamp() -> None:
    messages = extract_transcript(json.dumps(GOOD_EXPORT))
    assert messages[1].tokens == {"input": 120, "output": 45}
    assert messages[1].timestamp_ms == 2000


def test_unknown_part_types_are_skipped_not_fatal() -> None:
    # 'step-start' in the assistant message above has no handler and must not raise.
    messages = extract_transcript(json.dumps(GOOD_EXPORT))
    assert len(messages) == 2


def test_empty_output_raises() -> None:
    with pytest.raises(TranscriptError):
        extract_transcript("")


def test_non_json_raises() -> None:
    with pytest.raises(TranscriptError):
        extract_transcript("not json at all")


def test_missing_messages_key_raises() -> None:
    with pytest.raises(TranscriptError):
        extract_transcript(json.dumps({"info": {"id": "x"}}))


def test_message_missing_role_raises() -> None:
    broken = {"messages": [{"info": {"id": "m"}, "parts": []}]}
    with pytest.raises(TranscriptError):
        extract_transcript(json.dumps(broken))


def test_message_not_an_object_raises() -> None:
    broken = {"messages": ["not an object"]}
    with pytest.raises(TranscriptError):
        extract_transcript(json.dumps(broken))
