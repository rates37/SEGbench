"""Tests for prompt assembly (plan.md section 10)."""

from __future__ import annotations

from pathlib import Path

import pytest

from segbench.agent.prompt import PromptError, render_prompt
from segbench.corpus.loader import load_bug
from segbench.corpus.models import ChannelId

FIXTURE_BUG_DIR = Path(__file__).parent / "fixtures" / "corpus" / "good" / "bugs" / "lp-9000001"


@pytest.fixture
def bug():
    return load_bug(FIXTURE_BUG_DIR)


def test_render_prompt_full_includes_every_channel(bug) -> None:
    prompt = render_prompt(bug, environment_name="E1", wall_clock_s=300)
    for channel in bug.channels:
        assert f"BEGIN CHANNEL: {channel.id.value}" in prompt
        assert channel.text.strip() in prompt


def test_render_prompt_loo_excludes_one_channel(bug) -> None:
    visible = [c for c in bug.channel_ids if c != ChannelId.CUSTOMER_REPORT]
    prompt = render_prompt(
        bug, environment_name="E0", wall_clock_s=300, visible_channel_ids=visible
    )
    assert "BEGIN CHANNEL: customer_report" not in prompt
    assert "BEGIN CHANNEL: engineer_notes" in prompt


def test_render_prompt_embeds_answer_schema(bug) -> None:
    prompt = render_prompt(bug, environment_name="E2", wall_clock_s=120)
    assert '"root_cause"' in prompt
    assert "/workspace/answer.json" in prompt
    assert "120" in prompt


def test_render_prompt_environment_descriptions_differ(bug) -> None:
    e0 = render_prompt(bug, environment_name="E0", wall_clock_s=60)
    e1 = render_prompt(bug, environment_name="E1", wall_clock_s=60)
    e2 = render_prompt(bug, environment_name="E2", wall_clock_s=60)
    assert "no repository here" in e0
    assert "/workspace/repo" in e1
    assert "empty of source" in e2


def test_unknown_environment_raises(bug) -> None:
    with pytest.raises(PromptError):
        render_prompt(bug, environment_name="E9", wall_clock_s=60)


def test_empty_visible_channels_raises(bug) -> None:
    with pytest.raises(PromptError):
        render_prompt(bug, environment_name="E0", wall_clock_s=60, visible_channel_ids=[])
