"""Harness configuration.

Settings are loaded from ``segbench.toml`` (see ``segbench.example.toml``) and may be overridden
by environment variables named ``SEGBENCH_<SECTION>__<FIELD>``, e.g. ``SEGBENCH_CAPS__REPEATS=3``.
Precedence, highest first: explicit init arguments, environment variables, the TOML file, then the
field defaults.

The provider API key is never stored in the config file or on the settings object: config records
only the *name* of the environment variable holding it, and :meth:`ProviderConfig.api_key` reads
it on demand.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

DEFAULT_CONFIG_FILE = Path("segbench.toml")
EXAMPLE_CONFIG_FILE = Path("segbench.example.toml")


class ConfigError(Exception):
    """Raised when the configuration is present but unusable."""


class PathsConfig(BaseModel):
    """Filesystem locations the harness reads and writes."""

    corpus: Path = Path("corpus")
    results: Path = Path("results")
    image_cache: Path = Path(".cache/images")
    log_file: Path = Path("results/segbench.jsonl")


class ProviderConfig(BaseModel):
    """Inference provider used by the in-container agent and by the judge."""

    kind: Literal["openrouter", "copilot"] = "openrouter"
    base_url: str = "https://openrouter.ai/api/v1"
    api_key_env: str = "OPENROUTER_API_KEY"

    def api_key(self) -> str:
        """Read the API key from the environment. Never store or log the return value."""
        key = os.environ.get(self.api_key_env)
        if not key:
            raise ConfigError(
                f"provider API key not found: environment variable {self.api_key_env!r} is unset. "
                f"Export it, or point provider.api_key_env at the variable that holds it."
            )
        return key


class ModelConfig(BaseModel):
    """One model under test. Together these form the model matrix."""

    id: str
    label: str | None = None

    @property
    def display_name(self) -> str:
        return self.label or self.id


class JudgeConfig(BaseModel):
    """The pinned grading model. Never a model under test (CLAUDE.md invariant 3)."""

    model: str = "deepseek/deepseek-v4-flash"
    prompt_version: str = "v1"
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)


class CapsConfig(BaseModel):
    """Per-run limits. Exceeding one is a normal outcome, not an error."""

    wall_clock_s: int = Field(default=300, gt=0)
    max_cost_usd: float = Field(default=5.00, gt=0)
    repeats: int = Field(default=1, ge=1)


class ScoringConfig(BaseModel):
    """Composite score weights (plan.md section 7.3), stamped onto every grade record."""

    diagnosis_weight: float = Field(default=0.50, ge=0.0, le=1.0)
    localisation_weight: float = Field(default=0.30, ge=0.0, le=1.0)
    remedy_weight: float = Field(default=0.20, ge=0.0, le=1.0)

    component_weight: float = Field(default=0.50, ge=0.0, le=1.0)
    file_f1_weight: float = Field(default=0.30, ge=0.0, le=1.0)
    symbol_weight: float = Field(default=0.20, ge=0.0, le=1.0)

    unsupported_specific_penalty: float = Field(default=0.05, ge=0.0)
    max_penalty: float = Field(default=0.15, ge=0.0, le=1.0)


class RuntimeConfig(BaseModel):
    """Container backend selection and per-container defaults (plan.md section 9).

    ``backend`` picks the implementation of :class:`segbench.runtime.base.Runtime`. LXD is
    primary; podman is a fallback with deliberate gaps, documented in
    :mod:`segbench.runtime.podman`, and a run record must carry which one produced it.
    """

    backend: Literal["lxd", "podman"] = "lxd"
    #: Where image definitions live. Overridable so tests can build against a fixture directory.
    image_definitions: Path = Path("images")
    #: LXD project to create instances in. ``None`` uses the client's current project.
    lxd_project: str | None = None

    cpu: int | None = 2
    memory: str | None = "4GiB"
    disk: str | None = None
    processes: int | None = 4096

    #: Seconds to wait for a fresh container to accept commands.
    ready_timeout_s: float = Field(default=90.0, gt=0)


class ModelPrice(BaseModel):
    """USD per million tokens for one model, used by the proxy's cost meter (plan.md sec. 5.2)."""

    input_per_million_usd: float = Field(ge=0.0)
    output_per_million_usd: float = Field(ge=0.0)


class NetpolConfig(BaseModel):
    """Egress proxy and git-mirror addressing (plan.md section 5.1 and 5.2).

    ``mirror_host``/``mirror_port`` are reserved here so E2's allowlist entry is stable across
    phases even though the mirror listener itself is phase 4 (plan.md section 13) — the ACL, the
    ``/etc/hosts`` pin list and the proxy's policy model all need one fixed answer for "where is
    the mirror" today.
    """

    #: Interface the per-run proxy listeners bind on. ``0.0.0.0`` so both the host's loopback (for
    #: tests) and the LXD bridge gateway address (for real containers) can reach the same socket.
    bind_host: str = "0.0.0.0"
    mirror_host: str = "127.0.0.1"
    mirror_port: int = 18080
    #: Price table keyed on model id string, e.g. ``models.<id>`` or the judge's model string. A
    #: model absent here is still metered in tokens; its USD figure is reported as an estimate.
    pricing: dict[str, ModelPrice] = Field(default_factory=dict)


class CorpusConfig(BaseModel):
    """Tunables for ``segbench corpus validate`` (plan.md section 3.4).

    ``customer_host_patterns`` is empty by default. The maintainer knows their customers' naming
    conventions; the harness does not, and guessing would produce noise that gets the whole check
    switched off.
    """

    similarity_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "Fraction of ground_truth.root_cause vocabulary present in a channel above which the "
            "channel is flagged for human review. A warning, never a hard failure."
        ),
    )
    customer_host_patterns: list[str] = Field(
        default_factory=list, description="Regexes; a hostname matching one is a scrubber failure."
    )
    allow_values: list[str] = Field(
        default_factory=list, description="Literal values the scrubber should never flag."
    )
    allow_email_domains: list[str] = Field(default_factory=list)
    allow_mac_prefixes: list[str] = Field(default_factory=list)


class Settings(BaseSettings):
    """Top-level harness configuration."""

    model_config = SettingsConfigDict(
        env_prefix="SEGBENCH_",
        env_nested_delimiter="__",
        toml_file=DEFAULT_CONFIG_FILE,
        extra="forbid",
    )

    paths: PathsConfig = Field(default_factory=PathsConfig)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    models: list[ModelConfig] = Field(default_factory=list)
    judge: JudgeConfig = Field(default_factory=JudgeConfig)
    caps: CapsConfig = Field(default_factory=CapsConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    corpus: CorpusConfig = Field(default_factory=CorpusConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    netpol: NetpolConfig = Field(default_factory=NetpolConfig)

    concurrency: int = Field(default=4, ge=1)
    verbose: bool = False

    @field_validator("models")
    @classmethod
    def _reject_claude_models(cls, models: list[ModelConfig]) -> list[ModelConfig]:
        """CLAUDE.md invariant 6: Claude is not a model under test."""
        offenders = [m.id for m in models if "claude" in m.id.lower()]
        if offenders:
            raise ValueError(
                f"Claude models must not appear in the model matrix: {', '.join(offenders)}. "
                f"See CLAUDE.md invariant 6."
            )
        return models

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, env_settings, TomlConfigSettingsSource(settings_cls))


def load_settings(config_file: Path | None = None) -> Settings:
    """Load settings, optionally from a config file other than ``segbench.toml``.

    Environment variables still override file values. A missing file is not an error: the
    defaults in this module are the same as those in ``segbench.example.toml``.
    """
    if config_file is None:
        return Settings()

    path = Path(config_file)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")

    class _FileSettings(Settings):
        model_config = SettingsConfigDict(
            env_prefix="SEGBENCH_",
            env_nested_delimiter="__",
            toml_file=path,
            extra="forbid",
        )

    return _FileSettings()
