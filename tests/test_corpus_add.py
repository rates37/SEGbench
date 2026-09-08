"""`segbench corpus add`: tracker fetch, scaffold, and the instructions it prints.

No network. The tracker fetch is exercised against an httpx mock transport, which is enough to pin
the shape of what we ask for and what we do with the answer.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from segbench.corpus.add import (
    AddError,
    FetchedBug,
    fetch_github,
    fetch_launchpad,
    next_steps,
    scaffold,
)
from segbench.corpus.loader import CorpusError, load_bug

LAUNCHPAD_RESPONSES = {
    "/1.0/bugs/9000001": {
        "title": "Instances lose metadata access after upgrade",
        "description": "Our VMs stopped getting their keys.",
        "date_created": "2024-03-11T09:20:00Z",
    },
    "/1.0/bugs/9000001/messages": {
        "entries": [
            {"content": "Our VMs stopped getting their keys.", "owner_link": "https://x/~reporter"},
            {
                "content": "Reproduced in the lab.",
                "owner_link": "https://x/~engineer",
                "date_created": "2024-03-12T10:00:00Z",
            },
        ]
    },
    "/1.0/bugs/9000001/attachments": {
        "entries": [
            {"title": "sosreport.tar.xz", "type": "Unspecified", "data_link": "https://x/a"}
        ]
    },
}

GITHUB_RESPONSES = {
    "/repos/juju/juju/issues/16712": {
        "title": "Controller upgrade stalls",
        "body": "It hangs at 40%.",
        "created_at": "2024-04-01T00:00:00Z",
        "html_url": "https://github.com/juju/juju/issues/16712",
    },
    "/repos/juju/juju/issues/16712/comments": [
        {"user": {"login": "maintainer"}, "body": "Can you attach the logs?", "created_at": "x"}
    ],
}


def _client(responses: dict[str, object]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path not in responses:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=responses[request.url.path])

    return httpx.Client(transport=httpx.MockTransport(handler))


# --- fetching ----------------------------------------------------------------------------------


def test_fetch_launchpad_normalises_the_entry() -> None:
    fetched = fetch_launchpad("9000001", client=_client(LAUNCHPAD_RESPONSES))

    assert fetched.slug == "lp-9000001"
    assert fetched.tracker == "launchpad"
    assert fetched.reported_at == "2024-03-11T09:20:00Z"
    assert fetched.description == "Our VMs stopped getting their keys."
    # The first message repeats the description; only the discussion becomes comments.
    assert [c["body"] for c in fetched.comments] == ["Reproduced in the lab."]
    assert fetched.attachments[0]["title"] == "sosreport.tar.xz"


def test_fetch_launchpad_rejects_a_non_numeric_id() -> None:
    with pytest.raises(AddError, match="expects a bug number"):
        fetch_launchpad("https://bugs.launchpad.net/x/+bug/1")


def test_fetch_github_normalises_the_entry() -> None:
    fetched = fetch_github("juju/juju#16712", client=_client(GITHUB_RESPONSES))

    assert fetched.slug == "gh-juju-juju-16712"
    assert fetched.tracker == "github"
    assert fetched.title == "Controller upgrade stalls"
    assert [c["author"] for c in fetched.comments] == ["maintainer"]


@pytest.mark.parametrize(
    "reference", ["juju/juju", "juju#16712", "https://github.com/a/b/issues/1"]
)
def test_fetch_github_rejects_a_malformed_reference(reference: str) -> None:
    with pytest.raises(AddError, match="expects OWNER/REPO#NUMBER"):
        fetch_github(reference)


def test_tracker_errors_are_reported_with_the_status() -> None:
    with pytest.raises(AddError, match="HTTP 404"):
        fetch_launchpad("1", client=_client(LAUNCHPAD_RESPONSES))


# --- scaffolding ---------------------------------------------------------------------------


@pytest.fixture
def scaffolded(tmp_path: Path) -> Path:
    fetched = fetch_launchpad("9000001", client=_client(LAUNCHPAD_RESPONSES))
    return scaffold(fetched, tmp_path / "corpus")


def test_scaffold_stages_the_raw_tracker_text(scaffolded: Path) -> None:
    raw = (scaffolded / "raw" / "tracker.md").read_text(encoding="utf-8")

    assert "Our VMs stopped getting their keys." in raw
    assert "Reproduced in the lab." in raw
    assert "sosreport.tar.xz" in raw
    assert json.loads((scaffolded / "raw" / "tracker.json").read_text(encoding="utf-8"))


def test_scaffold_creates_the_directory_layout(scaffolded: Path) -> None:
    assert (scaffolded / "channels").is_dir()
    assert (scaffolded / "attachments").is_dir()
    assert (scaffolded / "bug.yaml").is_file()
    assert (scaffolded / "ground_truth.yaml").is_file()


def test_scaffold_does_not_split_channels(scaffolded: Path) -> None:
    """plan.md section 3.4: the split is supervised. Auto-splitting is how the answer leaks."""
    manifest = yaml.safe_load((scaffolded / "bug.yaml").read_text(encoding="utf-8"))

    assert manifest["channels"] == []
    assert list((scaffolded / "channels").iterdir()) == []


def test_skeleton_marks_unknown_fields_as_todo(scaffolded: Path) -> None:
    manifest = yaml.safe_load((scaffolded / "bug.yaml").read_text(encoding="utf-8"))

    assert manifest["product"] == "TODO"
    assert manifest["repo"]["url"] == "TODO"
    assert manifest["repo"]["pre_fix_ref"] == "TODO"
    assert manifest["title"] == "TODO"


def test_skeleton_ground_truth_is_marked_inferred_and_unreviewed(scaffolded: Path) -> None:
    truth = yaml.safe_load((scaffolded / "ground_truth.yaml").read_text(encoding="utf-8"))

    assert truth["provenance"] == "inferred"
    assert truth["reviewed_by"] is None
    assert truth["fix"]["files"] == []
    assert truth["fix"]["symbols"] is None


def test_a_scaffold_does_not_yet_load_as_a_valid_bug(scaffolded: Path) -> None:
    """It is a staging area, not a bug: validate must reject it until a human finishes it."""
    with pytest.raises(CorpusError):
        load_bug(scaffolded)


def test_scaffold_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    fetched = fetch_launchpad("9000001", client=_client(LAUNCHPAD_RESPONSES))
    scaffold(fetched, tmp_path / "corpus")

    with pytest.raises(AddError, match="--force"):
        scaffold(fetched, tmp_path / "corpus")

    assert scaffold(fetched, tmp_path / "corpus", force=True).is_dir()


def test_next_steps_names_the_supervised_split_and_the_leak_rules(tmp_path: Path) -> None:
    text = next_steps(tmp_path / "bugs" / "lp-9000001")

    assert "supervised" in text
    assert "cites the fix" in text
    assert "corpus derive --bug lp-9000001" in text
    assert "corpus validate --bug lp-9000001" in text


def test_fetched_bug_defaults_are_empty() -> None:
    fetched = FetchedBug(
        slug="lp-1", title="t", url="u", tracker="launchpad", reported_at="x", description=""
    )

    assert fetched.comments == []
    assert fetched.attachments == []
