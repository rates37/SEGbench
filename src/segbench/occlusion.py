"""Channel set generation for the run matrix (plan.md sections 6, 8, 12).

v1 generates, for one bug, exactly ``{full}`` plus ``{loo:c for c in bug.channels}`` — restricted
to channels the bug actually has, per plan.md section 6. This is deliberately the only thing this
module does today; the richer variants named as future work in plan.md section 12 (single-channel
-only baselines, minimal baselines, sampled powersets, degraded channels) are not implemented, but
the data model is shaped so they slot in without reshaping anything downstream: a channel set is
just a *name* (what ends up on the run record, and what
:func:`segbench.agent.run.resolve_channel_set` parses) plus the *visible channel ids* it resolves
to. A future variant only needs to add another name scheme and another branch in
``resolve_channel_set`` — the orchestrator, the manifest and the grading/export layers never look
past the name.
"""

from __future__ import annotations

from dataclasses import dataclass

from segbench.corpus.models import Bug, ChannelId

FULL = "full"
LOO_PREFIX = "loo:"


@dataclass(frozen=True)
class ChannelSetSpec:
    """One channel set generated for a specific bug: its name and the channels it exposes."""

    name: str
    visible: tuple[ChannelId, ...]


def loo_name(channel: ChannelId) -> str:
    """The channel-set name for "everything but ``channel``"."""
    return f"{LOO_PREFIX}{channel.value}"


def channel_sets_for(bug: Bug) -> list[ChannelSetSpec]:
    """``{full}`` plus ``{loo:c for c in bug.channels}``, in a stable order: ``full`` first,
    then leave-one-out sets in the bug's declared channel order."""
    ids = tuple(bug.channel_ids)
    sets = [ChannelSetSpec(name=FULL, visible=ids)]
    sets.extend(
        ChannelSetSpec(name=loo_name(channel_id), visible=tuple(c for c in ids if c != channel_id))
        for channel_id in ids
    )
    return sets


def channel_set_names(bug: Bug) -> list[str]:
    """The names only, in the same order as :func:`channel_sets_for` — what the orchestrator
    iterates over and what a ``--channel-sets`` filter is matched against."""
    return [spec.name for spec in channel_sets_for(bug)]
