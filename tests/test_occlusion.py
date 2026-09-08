"""Channel-set generation for the run matrix (plan.md sections 6, 8, 12)."""

from __future__ import annotations

from segbench.corpus.loader import load_bug
from segbench.occlusion import FULL, channel_set_names, channel_sets_for, loo_name


def test_channel_sets_for_is_full_plus_one_loo_per_channel(good_corpus) -> None:
    bug = load_bug(good_corpus / "bugs" / "lp-9000001")

    sets = channel_sets_for(bug)

    assert len(sets) == 1 + len(bug.channel_ids)
    assert sets[0].name == FULL
    assert sets[0].visible == tuple(bug.channel_ids)
    for channel_id, spec in zip(bug.channel_ids, sets[1:], strict=True):
        assert spec.name == loo_name(channel_id)
        assert channel_id not in spec.visible
        assert len(spec.visible) == len(bug.channel_ids) - 1


def test_channel_set_names_only_covers_channels_the_bug_has(good_corpus) -> None:
    bug = load_bug(good_corpus / "bugs" / "lp-9000001")

    names = channel_set_names(bug)

    assert names[0] == "full"
    assert set(names[1:]) == {loo_name(c) for c in bug.channel_ids}
    # every generated loo: name refers to a channel this bug actually has
    for name in names[1:]:
        assert name[len("loo:") :] in {c.value for c in bug.channel_ids}
