"""Tests for channel-set resolution and the opencode config generator (plan.md sections 6, 10)."""

from __future__ import annotations

from pathlib import Path

import pytest

from segbench.agent.opencode import build_config, provider_id
from segbench.agent.run import RunError, resolve_channel_set
from segbench.config import ModelConfig, Settings
from segbench.corpus.loader import load_bug
from segbench.corpus.models import ChannelId

FIXTURE_BUG_DIR = Path(__file__).parent / "fixtures" / "corpus" / "good" / "bugs" / "lp-9000001"


@pytest.fixture
def bug():
    return load_bug(FIXTURE_BUG_DIR)


def test_full_channel_set_is_every_channel(bug) -> None:
    resolved = resolve_channel_set(bug, "full")
    assert resolved.label == "full"
    assert set(resolved.visible) == set(bug.channel_ids)


def test_loo_excludes_exactly_one_channel(bug) -> None:
    resolved = resolve_channel_set(bug, "loo:engineer_notes")
    assert resolved.label == "loo:engineer_notes"
    assert ChannelId.ENGINEER_NOTES not in resolved.visible
    assert len(resolved.visible) == len(bug.channel_ids) - 1


def test_loo_for_channel_bug_does_not_have_raises(bug) -> None:
    with pytest.raises(RunError):
        resolve_channel_set(bug, "loo:system_environment")


def test_unknown_channel_set_label_raises(bug) -> None:
    with pytest.raises(RunError):
        resolve_channel_set(bug, "single:error_trace")


def test_unknown_channel_id_raises(bug) -> None:
    with pytest.raises(RunError):
        resolve_channel_set(bug, "loo:not_a_real_channel")


def test_opencode_config_references_api_key_env_not_the_secret(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-super-secret")
    settings = Settings()
    model = ModelConfig(id="deepseek/deepseek-v4-flash")
    config = build_config(settings, model)
    assert config["model"] == "openrouter/deepseek/deepseek-v4-flash"
    assert "sk-super-secret" not in str(config)
    assert config["provider"]["openrouter"]["options"]["apiKey"].startswith("{env:")
    assert config["autoupdate"] is False
    assert config["share"] == "disabled"


def test_provider_id_maps_copilot_kind() -> None:
    settings = Settings(provider={"kind": "copilot", "api_key_env": "COPILOT_TOKEN"})
    assert provider_id(settings) == "github-copilot"
