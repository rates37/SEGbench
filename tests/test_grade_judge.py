"""The judge's JSON contract and CLAUDE.md invariant 3 (judge is never a model under test)."""

from __future__ import annotations

import json

import httpx
import pytest

from segbench.config import ModelConfig, Settings
from segbench.grade.judge import (
    JudgeError,
    JudgeInput,
    call_judge,
    render_judge_prompt,
)


@pytest.fixture
def judge_input() -> JudgeInput:
    return JudgeInput(
        ground_truth_root_cause="the datapath cache is never repopulated on reconnect",
        also_acceptable_root_causes=["equivalent framing at the charm layer"],
        agent_root_cause="the agent's reconnect handler skips the cache refresh",
        agent_proposed_fix="repopulate the datapath cache on reconnect",
    )


def _settings(**overrides) -> Settings:
    return Settings(provider={"api_key_env": "TEST_JUDGE_KEY"}, **overrides)


@pytest.fixture(autouse=True)
def _judge_api_key(monkeypatch):
    monkeypatch.setenv("TEST_JUDGE_KEY", "sk-fake")


def test_render_judge_prompt_carries_only_the_four_fields(judge_input: JudgeInput) -> None:
    prompt = render_judge_prompt(judge_input, prompt_version="v1")
    assert judge_input.ground_truth_root_cause in prompt
    assert judge_input.agent_root_cause in prompt
    assert judge_input.agent_proposed_fix in prompt
    assert "equivalent framing at the charm layer" in prompt


def test_judge_input_rejects_extra_fields() -> None:
    with pytest.raises(ValueError, match="transcript"):
        JudgeInput(
            ground_truth_root_cause="x",
            agent_root_cause="y",
            agent_proposed_fix="z",
            transcript="should not be allowed",  # type: ignore[call-arg]
        )


def _fake_response(payload: dict) -> httpx.Response:
    body = {"choices": [{"message": {"content": json.dumps(payload)}}]}
    return httpx.Response(200, json=body)


def test_call_judge_hard_fails_if_judge_model_is_under_test(judge_input: JudgeInput) -> None:
    settings = _settings(judge={"model": "deepseek/deepseek-v4-flash"})
    models = [ModelConfig(id="deepseek/deepseek-v4-flash")]
    with pytest.raises(JudgeError, match="model matrix under test"):
        call_judge(settings, judge_input, models=models)


def test_call_judge_parses_strict_json(judge_input: JudgeInput) -> None:
    settings = _settings()
    payload = {
        "root_cause_score": 3,
        "root_cause_rationale": "matches the ground truth mechanism",
        "fix_score": 2,
        "fix_rationale": "plausible fix in the right place",
        "contradicts_ground_truth": False,
        "unsupported_specifics": 0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return _fake_response(payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = call_judge(settings, judge_input, models=[], client=client)
    assert result.root_cause_score == 3
    assert result.fix_score == 2
    assert result.contradicts_ground_truth is False


def test_call_judge_retries_on_unparseable_then_succeeds(judge_input: JudgeInput) -> None:
    settings = _settings()
    payload = {
        "root_cause_score": 1,
        "root_cause_rationale": "wrong mechanism",
        "fix_score": 0,
        "fix_rationale": "no fix",
        "contradicts_ground_truth": True,
        "unsupported_specifics": 2,
    }
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})
        return _fake_response(payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = call_judge(settings, judge_input, models=[], client=client)
    assert calls["n"] == 2
    assert result.unsupported_specifics == 2


def test_call_judge_gives_up_after_bounded_retries(judge_input: JudgeInput) -> None:
    settings = _settings()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "still not json"}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(JudgeError, match="never returned a parseable response"):
        call_judge(settings, judge_input, models=[], client=client)
    assert calls["n"] == 3


def test_call_judge_extracts_json_from_markdown_fence(judge_input: JudgeInput) -> None:
    settings = _settings()
    payload = {
        "root_cause_score": 2,
        "root_cause_rationale": "close",
        "fix_score": 2,
        "fix_rationale": "close",
        "contradicts_ground_truth": False,
        "unsupported_specifics": 0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        fenced = f"Sure, here is the JSON:\n```json\n{json.dumps(payload)}\n```"
        return httpx.Response(200, json={"choices": [{"message": {"content": fenced}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = call_judge(settings, judge_input, models=[], client=client)
    assert result.root_cause_score == 2
