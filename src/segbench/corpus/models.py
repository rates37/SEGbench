"""Corpus schemas: ``bug.yaml``, ``ground_truth.yaml``, and the closed channel vocabulary.

These models mirror plan.md sections 3.2, 3.3 and 4 field for field. They are the boundary between
a maintainer-authored YAML file and the harness, so they are strict: unknown keys are rejected
rather than ignored, because a typo'd key in ``ground_truth.yaml`` that silently defaults is
exactly the kind of quiet corpus degradation CLAUDE.md forbids.

Adding a member to :class:`ChannelId` is a schema change: occlusion results are only comparable
across bugs sharing a vocabulary, so every existing bug needs migrating (plan.md section 4).
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ChannelId(StrEnum):
    """The closed channel vocabulary (plan.md section 4)."""

    CUSTOMER_REPORT = "customer_report"
    ENGINEER_NOTES = "engineer_notes"
    ERROR_TRACE = "error_trace"
    VERSION_MANIFEST = "version_manifest"
    SERVICE_LOGS = "service_logs"
    DEPLOYMENT_CONFIG = "deployment_config"
    REPRODUCTION_STEPS = "reproduction_steps"
    SYSTEM_ENVIRONMENT = "system_environment"


class Product(StrEnum):
    """Canonical product the defect lives in."""

    SUNBEAM = "sunbeam"
    CHARMED_OPENSTACK = "charmed-openstack"
    JUJU = "juju"
    MICROK8S = "microk8s"
    KERNEL = "kernel"
    OTHER = "other"


class Tracker(StrEnum):
    """Where the bug came from."""

    LAUNCHPAD = "launchpad"
    GITHUB = "github"
    REPRODUCTION = "reproduction"


class Origin(StrEnum):
    """How faithful a channel's text is to the original report.

    Occlusion conclusions about a channel whose corpus is mostly ``synthesised`` must carry that
    caveat, so the value is recorded per channel rather than per bug.
    """

    VERBATIM = "verbatim"
    PARAPHRASED = "paraphrased"
    SYNTHESISED = "synthesised"


class Difficulty(StrEnum):
    """Maintainer's guess. Never shown to the agent."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Provenance(StrEnum):
    """How the ground truth was produced. ``inferred`` means agent-derived and needing review."""

    AUTHORED = "authored"
    INFERRED = "inferred"


class _Strict(BaseModel):
    """Base for every corpus model: reject unknown keys, strip incidental whitespace."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, use_enum_values=False)


class BugSource(_Strict):
    """Provenance of the report itself (plan.md section 3.2, ``source``)."""

    tracker: Tracker
    url: str | None = None
    reported_at: dt.datetime = Field(
        description="Cutoff date for git truncation: the mirror serves nothing newer than this."
    )

    @model_validator(mode="after")
    def _url_required_unless_reproduction(self) -> BugSource:
        if self.tracker is not Tracker.REPRODUCTION and not self.url:
            raise ValueError(
                f"source.url is required when source.tracker is {self.tracker.value!r}; "
                f"only 'reproduction' bugs may have a null url"
            )
        return self


class BugRepo(_Strict):
    """The target repository and the commit the agent is allowed to see up to."""

    url: str
    pre_fix_ref: str = Field(min_length=7, description="Parent of the fix commit.")
    build_setup: str | None = None


class ChannelSpec(_Strict):
    """One entry in ``bug.yaml``'s ``channels`` list."""

    id: ChannelId
    file: Path
    origin: Origin


class BugManifest(_Strict):
    """``bug.yaml`` (plan.md section 3.2)."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    title: str = Field(min_length=1)
    product: Product
    source: BugSource
    repo: BugRepo
    difficulty_hint: Difficulty | None = None
    tags: list[str] = Field(default_factory=list)
    channels: list[ChannelSpec] = Field(min_length=1)
    notes: str | None = None

    @model_validator(mode="after")
    def _channels_unique(self) -> BugManifest:
        seen: set[ChannelId] = set()
        duplicates: set[str] = set()
        for channel in self.channels:
            if channel.id in seen:
                duplicates.add(channel.id.value)
            seen.add(channel.id)
        if duplicates:
            raise ValueError(
                f"channels must be unique; repeated channel id(s): {', '.join(sorted(duplicates))}"
            )
        return self


class FixInfo(_Strict):
    """The commit that fixed the bug.

    ``files`` and ``symbols`` are populated by ``segbench corpus derive`` from the commit itself
    and must never be hand-maintained (plan.md section 3.3). ``symbols`` is ``None`` — as distinct
    from empty — when the language has no supported symbol extractor.
    """

    commit: str = Field(min_length=7)
    url: str | None = None
    files: list[str] = Field(default_factory=list)
    symbols: list[str] | None = None


class GroundTruth(_Strict):
    """``ground_truth.yaml`` (plan.md section 3.3)."""

    root_cause: str = Field(min_length=1)
    component: str = Field(min_length=1)
    fix: FixInfo
    acceptable_components: list[str] = Field(default_factory=list)
    also_acceptable_root_causes: list[str] = Field(default_factory=list)
    provenance: Provenance = Provenance.AUTHORED
    reviewed_by: str | None = None

    @property
    def components(self) -> list[str]:
        """``component`` plus its accepted aliases, for the grader's component match."""
        return [self.component, *self.acceptable_components]


class LoadedChannel(BaseModel):
    """A channel spec together with the text actually on disk."""

    model_config = ConfigDict(extra="forbid")

    id: ChannelId
    origin: Origin
    path: Path
    text: str


class Bug(BaseModel):
    """A fully loaded bug directory: manifest, ground truth, and channel text.

    ``ready`` gates inclusion in a campaign. A bug whose ground truth has not been reviewed still
    loads — the maintainer needs to be able to work on it — but is excluded unless the caller
    passes ``--include-unreviewed``.
    """

    model_config = ConfigDict(extra="forbid")

    directory: Path
    manifest: BugManifest
    ground_truth: GroundTruth
    channels: list[LoadedChannel]

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def ready(self) -> bool:
        """True when the ground truth has a reviewer, i.e. the bug may enter a campaign."""
        return bool(self.ground_truth.reviewed_by)

    @property
    def not_ready_reason(self) -> str | None:
        if self.ready:
            return None
        return "ground_truth.reviewed_by is unset; the ground truth has not been reviewed"

    @property
    def channel_ids(self) -> list[ChannelId]:
        return [c.id for c in self.channels]

    def channel(self, channel_id: ChannelId) -> LoadedChannel | None:
        return next((c for c in self.channels if c.id == channel_id), None)
