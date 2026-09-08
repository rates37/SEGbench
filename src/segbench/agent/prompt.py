"""Assembles the task prompt from a versioned template (plan.md section 10).

The template lives in ``templates/prompt_<version>.md``, not a Python string literal, so a diff
against it is a diff against what the agent actually reads. Changing the prompt means adding a new
template file and bumping :data:`PROMPT_VERSION`; the old version stays on disk, because a run
record must be able to reproduce exactly what the agent saw (CLAUDE.md invariant 4) and a run
graded next month may still be pointing at ``v1``.

Uses :class:`string.Template` (``$identifier`` substitution) rather than ``str.format``: the
rendered answer schema is itself JSON, full of literal ``{`` and ``}``, and ``str.format`` would
try to interpret every one of them as a field reference.
"""

from __future__ import annotations

import json
from pathlib import Path
from string import Template

from segbench.agent.schema import ANSWER_FILENAME, ANSWER_JSON_SCHEMA
from segbench.corpus.models import Bug, ChannelId

PROMPT_VERSION = "v1"

TEMPLATES_DIR = Path(__file__).parent / "templates"

DEFAULT_ANSWER_PATH = f"/workspace/{ANSWER_FILENAME}"

#: plan.md section 10, point 2: what access exists, stated as fact. Keyed on the environment id
#: used throughout the harness (plan.md section 5) so a caller cannot accidentally pass a policy
#: name (:class:`segbench.netpol.policy.EgressPolicy`) that does not match a template section.
_ENVIRONMENT_DESCRIPTIONS = {
    "E0": (
        "There is no repository here. `/workspace` contains only the channel files listed below; "
        "you are working from what you already know about this codebase, not from the code "
        "itself."
    ),
    "E1": (
        "`/workspace/repo` contains the target repository, checked out exactly as it existed "
        "immediately before the fix. It has no history beyond a single commit, no tags, no "
        "branches, and no remotes — there is nothing to fetch and no timeline to mine, only the "
        "tree as it stood."
    ),
    "E2": (
        "`/workspace` starts empty of source. You may clone the target repository, and any other "
        "repository you need, using the git remotes already configured for you; every clone is "
        "served from a local mirror truncated to the time this bug was reported, so nothing from "
        "after that point is visible no matter what you ask for."
    ),
}


class PromptError(Exception):
    """The template is missing, malformed, or the caller asked for something it cannot render."""


def _load_template(version: str) -> str:
    path = TEMPLATES_DIR / f"prompt_{version}.md"
    if not path.is_file():
        raise PromptError(f"no prompt template for version {version!r} at {path}")
    return path.read_text(encoding="utf-8")


def _render_channel_block(channel_id: ChannelId, text: str) -> str:
    """One channel, clearly delimited and labelled with its channel id.

    A fenced-code-block style delimiter was considered and rejected: channel text (a pasted log,
    a stack trace) routinely contains its own triple-backtick fences, which would prematurely
    close the block. The delimiter here is chosen to be vanishingly unlikely to collide.
    """
    return (
        f"----- BEGIN CHANNEL: {channel_id.value} -----\n"
        f"{text.rstrip()}\n"
        f"----- END CHANNEL: {channel_id.value} -----\n"
    )


def render_prompt(
    bug: Bug,
    *,
    environment_name: str,
    wall_clock_s: int,
    visible_channel_ids: list[ChannelId] | None = None,
    answer_path: str = DEFAULT_ANSWER_PATH,
    version: str = PROMPT_VERSION,
) -> str:
    """Render the task prompt for one run.

    ``visible_channel_ids`` selects the subset of the bug's channels to show — a leave-one-out
    channel set. ``None`` (the default) shows every channel the bug has, i.e. the ``full`` channel
    set (plan.md section 6).
    """
    if environment_name not in _ENVIRONMENT_DESCRIPTIONS:
        raise PromptError(
            f"unknown environment {environment_name!r}; expected one of "
            f"{sorted(_ENVIRONMENT_DESCRIPTIONS)}"
        )

    wanted = set(visible_channel_ids) if visible_channel_ids is not None else None
    blocks = [
        _render_channel_block(channel.id, channel.text)
        for channel in bug.channels
        if wanted is None or channel.id in wanted
    ]
    if not blocks:
        raise PromptError(
            f"no visible channels for bug {bug.id!r} (requested {visible_channel_ids})"
        )

    template = Template(_load_template(version))
    return template.substitute(
        product=bug.manifest.product.value,
        environment_name=environment_name,
        environment_description=_ENVIRONMENT_DESCRIPTIONS[environment_name],
        channels="\n".join(blocks),
        answer_path=answer_path,
        answer_schema=json.dumps(ANSWER_JSON_SCHEMA, indent=2),
        wall_clock_s=str(wall_clock_s),
    )
