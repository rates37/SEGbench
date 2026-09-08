"""Configuration loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from segbench.config import ConfigError, ModelConfig, Settings, load_settings

EXAMPLE = Path(__file__).resolve().parents[1] / "segbench.example.toml"


def test_loads_example_config() -> None:
    settings = load_settings(EXAMPLE)

    assert settings.paths.corpus == Path("corpus")
    assert settings.provider.kind == "openrouter"
    assert settings.provider.api_key_env == "OPENROUTER_API_KEY"
    assert [m.id for m in settings.models] == [
        "deepseek/deepseek-chat",
        "qwen/qwen-2.5-coder-32b-instruct",
    ]
    assert settings.judge.temperature == 0.0
    assert settings.caps.wall_clock_s == 300
    assert settings.caps.max_cost_usd == 5.00
    assert settings.caps.repeats == 1
    assert settings.scoring.diagnosis_weight == 0.50
    assert settings.scoring.localisation_weight == 0.30
    assert settings.scoring.remedy_weight == 0.20
    assert settings.concurrency == 4


def test_example_config_matches_field_defaults() -> None:
    """The example file documents the defaults; drift between the two is a bug."""
    from_file = load_settings(EXAMPLE)
    defaults = Settings(models=from_file.models)

    assert from_file.model_dump() == defaults.model_dump()


def test_env_var_overrides_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEGBENCH_CAPS__REPEATS", "3")
    monkeypatch.setenv("SEGBENCH_CONCURRENCY", "9")
    monkeypatch.setenv("SEGBENCH_PROVIDER__KIND", "copilot")

    settings = load_settings(EXAMPLE)

    assert settings.caps.repeats == 3
    assert settings.concurrency == 9
    assert settings.provider.kind == "copilot"


def test_missing_config_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "nope.toml")


def test_api_key_is_read_from_env_and_not_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_settings(EXAMPLE)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret")

    assert settings.provider.api_key() == "sk-secret"
    assert "sk-secret" not in repr(settings)
    assert "sk-secret" not in str(settings.model_dump())


def test_missing_api_key_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_settings(EXAMPLE)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        settings.provider.api_key()


def test_claude_models_are_rejected() -> None:
    with pytest.raises(ValueError, match="invariant 6"):
        Settings(models=[ModelConfig(id="anthropic/claude-opus-5")])
