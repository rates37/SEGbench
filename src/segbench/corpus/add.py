"""Scaffold a new bug directory from a Launchpad or GitHub tracker entry.

What this does: fetch the title, description, comments and attachment list; drop them verbatim into
``corpus/bugs/<slug>/raw/``; and write skeleton ``bug.yaml`` and ``ground_truth.yaml`` files whose
unknown fields are marked ``TODO`` and whose ground truth is stamped ``provenance: inferred``.

What this deliberately does **not** do: split the raw text into channels. plan.md section 3.4 is
explicit that the split is a human-supervised judgement call — tracker text mixes channels freely,
and a developer comment stating the resolved cause looks exactly like triage notes to a machine.
Auto-splitting would be the single easiest way to leak the answer into ``engineer_notes`` and
invalidate the benchmark, so the command ends by printing what the human has to do next.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

LAUNCHPAD_API = "https://api.launchpad.net/1.0"
GITHUB_API = "https://api.github.com"

GITHUB_REF_RE = re.compile(r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)#(?P<number>\d+)$")

_TODO = "TODO"


class AddError(Exception):
    """The tracker reference is malformed, or the tracker could not be reached."""


@dataclass
class FetchedBug:
    """Tracker metadata, normalised across Launchpad and GitHub."""

    slug: str
    title: str
    url: str
    tracker: str
    reported_at: str
    description: str
    comments: list[dict[str, Any]] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def _get_json(client: httpx.Client, url: str, **kwargs: Any) -> Any:
    try:
        response = client.get(url, **kwargs)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise AddError(f"{url}: tracker returned HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise AddError(f"{url}: could not reach the tracker: {exc}") from exc
    return response.json()


def fetch_launchpad(bug_number: str, *, client: httpx.Client | None = None) -> FetchedBug:
    """Fetch one Launchpad bug via the public API."""
    if not bug_number.isdigit():
        raise AddError(f"--launchpad expects a bug number, got {bug_number!r}")

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    try:
        bug = _get_json(client, f"{LAUNCHPAD_API}/bugs/{bug_number}")
        messages = _get_json(client, f"{LAUNCHPAD_API}/bugs/{bug_number}/messages")
        attachments = _get_json(client, f"{LAUNCHPAD_API}/bugs/{bug_number}/attachments")
    finally:
        if owns_client:
            client.close()

    entries = messages.get("entries", [])
    return FetchedBug(
        slug=f"lp-{bug_number}",
        title=bug.get("title", _TODO),
        url=f"https://bugs.launchpad.net/bugs/{bug_number}",
        tracker="launchpad",
        reported_at=bug.get("date_created", _TODO),
        description=bug.get("description", ""),
        # entries[0] is the description repeated as the first message; the rest are the discussion.
        comments=[
            {
                "author": entry.get("owner_link", "").rsplit("/~", 1)[-1],
                "created_at": entry.get("date_created"),
                "body": entry.get("content", ""),
            }
            for entry in entries[1:]
        ],
        attachments=[
            {"title": a.get("title"), "type": a.get("type"), "url": a.get("data_link")}
            for a in attachments.get("entries", [])
        ],
        raw={"bug": bug, "messages": messages, "attachments": attachments},
    )


def fetch_github(reference: str, *, client: httpx.Client | None = None) -> FetchedBug:
    """Fetch one GitHub issue, given ``OWNER/REPO#N``."""
    match = GITHUB_REF_RE.match(reference.strip())
    if not match:
        raise AddError(f"--github expects OWNER/REPO#NUMBER, got {reference!r}")
    owner, repo, number = match.group("owner"), match.group("repo"), match.group("number")

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    headers = {"Accept": "application/vnd.github+json"}
    try:
        issue = _get_json(
            client, f"{GITHUB_API}/repos/{owner}/{repo}/issues/{number}", headers=headers
        )
        comments = _get_json(
            client, f"{GITHUB_API}/repos/{owner}/{repo}/issues/{number}/comments", headers=headers
        )
    finally:
        if owns_client:
            client.close()

    return FetchedBug(
        slug=f"gh-{owner}-{repo}-{number}".lower(),
        title=issue.get("title", _TODO),
        url=issue.get("html_url", f"https://github.com/{owner}/{repo}/issues/{number}"),
        tracker="github",
        reported_at=issue.get("created_at", _TODO),
        description=issue.get("body") or "",
        comments=[
            {
                "author": (c.get("user") or {}).get("login"),
                "created_at": c.get("created_at"),
                "body": c.get("body") or "",
            }
            for c in comments
        ],
        attachments=[],
        raw={"issue": issue, "comments": comments},
    )


def _skeleton_bug_yaml(fetched: FetchedBug) -> str:
    """The manifest skeleton. Everything the tracker cannot tell us is a literal TODO.

    Written as text rather than dumped from a dict on purpose: the comments explaining what each
    TODO wants are the useful part of the file, and yaml.safe_dump would discard them.
    """
    return f"""\
# Scaffolded by `segbench corpus add`. Every TODO below needs a human.
# The raw tracker text is in raw/ — split it into channels/ by hand (see the printed next steps).
id: {fetched.slug}
# A neutral symptom-level title. It must NOT name the root cause: it is shown to the agent.
# The tracker's own title, for reference: {fetched.title!r}
title: {_TODO}
product: {_TODO}   # sunbeam | charmed-openstack | juju | microk8s | kernel | other
source:
  tracker: {fetched.tracker}
  url: {fetched.url}
  reported_at: {fetched.reported_at}   # the git-truncation cutoff; verify it
repo:
  url: {_TODO}            # e.g. https://opendev.org/openstack/nova
  pre_fix_ref: {_TODO}    # parent of the fix commit
  build_setup: null
difficulty_hint: {_TODO}  # low | medium | high — never shown to the agent
tags: []
# One entry per channel you actually create under channels/. Omit channels this bug lacks;
# do not create an empty file for them. origin: verbatim | paraphrased | synthesised.
channels: []
notes: |
  {_TODO}: maintainer notes. Never shown to the agent.
"""


def _skeleton_ground_truth_yaml(fetched: FetchedBug) -> str:
    return f"""\
# Scaffolded by `segbench corpus add`. provenance is `inferred` until a human authors and reviews
# this file. Do not invent root-cause prose for a fix you have not read (CLAUDE.md).
root_cause: |
  {_TODO}: what is actually wrong, and why. This is what the judge compares against.
component: {_TODO}
fix:
  commit: {_TODO}
  url: {_TODO}
  # files and symbols are generated by `segbench corpus derive`. Never hand-maintain them.
  files: []
  symbols: null
acceptable_components: []
also_acceptable_root_causes: []
provenance: inferred
# reviewed_by must be set before this bug enters a campaign.
reviewed_by: null
"""


def _raw_dump(fetched: FetchedBug) -> str:
    """A single readable transcript of the tracker entry, for the human doing the split."""
    parts = [
        f"# {fetched.title}",
        f"\nSource: {fetched.url}",
        f"Reported: {fetched.reported_at}",
        "\n## Description\n",
        fetched.description,
    ]
    for index, comment in enumerate(fetched.comments, start=1):
        parts.append(
            f"\n## Comment {index} — {comment.get('author')} — {comment.get('created_at')}\n"
        )
        parts.append(comment.get("body", ""))
    if fetched.attachments:
        parts.append("\n## Attachments\n")
        for attachment in fetched.attachments:
            parts.append(
                f"- {attachment.get('title')} ({attachment.get('type')}) {attachment.get('url')}"
            )
    return "\n".join(parts) + "\n"


def scaffold(fetched: FetchedBug, corpus_root: Path, *, force: bool = False) -> Path:
    """Write the bug directory. Refuses to overwrite an existing one unless ``force``."""
    directory = Path(corpus_root) / "bugs" / fetched.slug
    if directory.exists() and not force:
        raise AddError(
            f"{directory} already exists; pass --force to overwrite the scaffold "
            f"(this will discard hand-written channels)"
        )

    (directory / "raw").mkdir(parents=True, exist_ok=True)
    (directory / "channels").mkdir(exist_ok=True)
    (directory / "attachments").mkdir(exist_ok=True)

    (directory / "raw" / "tracker.md").write_text(_raw_dump(fetched), encoding="utf-8")
    (directory / "raw" / "tracker.json").write_text(
        json.dumps(fetched.raw, indent=2, sort_keys=True), encoding="utf-8"
    )
    (directory / "bug.yaml").write_text(_skeleton_bug_yaml(fetched), encoding="utf-8")
    (directory / "ground_truth.yaml").write_text(
        _skeleton_ground_truth_yaml(fetched), encoding="utf-8"
    )
    return directory


def next_steps(directory: Path) -> str:
    """The instructions printed after scaffolding. Explicit, because the split is the risky part."""
    return f"""\
Scaffolded {directory}

The raw tracker text is in {directory / "raw"}. Splitting it into channels is a supervised step and
is deliberately not automated. Next:

  1. Read raw/tracker.md and decide which channels this bug actually has. Omit the ones it lacks —
     leave-one-out only generates cells for channels that exist.
  2. Write each channel to channels/<id>.<ext> and list it in bug.yaml with its origin
     (verbatim | paraphrased | synthesised). Channel ids are a closed vocabulary; see plan.md §4.
  3. While splitting, enforce the three rules from plan.md §3.4:
       - No channel contains the answer. A developer comment stating the resolved cause belongs in
         ground_truth.root_cause, not in engineer_notes.
       - No channel cites the fix. Strip review URLs, patch links, "fixed by" references.
       - Keep the version realistic. A vague reported version stays vague.
  4. Fill in the TODOs in bug.yaml (product, repo.url, repo.pre_fix_ref, title, difficulty_hint).
  5. Author ground_truth.yaml from the fix commit, then set provenance: authored.
  6. Run:  segbench corpus derive --bug {directory.name} --repo <path to a local clone>
  7. Set reviewed_by once a human has checked the ground truth. Until then the bug is not-ready and
     is excluded from campaigns.
  8. Run:  segbench corpus validate --bug {directory.name}
"""
