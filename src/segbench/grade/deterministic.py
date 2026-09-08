"""Deterministic grading checks (plan.md section 7.1).

These never call a model and never touch the network: they are pure functions over the agent's
:class:`~segbench.agent.schema.Answer` and the bug's :class:`~segbench.corpus.models.GroundTruth`.
That is what lets ``segbench grade --regrade`` change scores for free after a weight change in
config — nothing here is re-derived from inference.

Path normalisation is the fiddly part. Agents emit file paths in at least four shapes for what is
really the same file:

- repo-relative, as asked: ``neutron/agent/ovn/metadata/agent.py``
- absolute inside the container: ``/root/repo/neutron/agent/ovn/metadata/agent.py``
- git-diff style: ``a/neutron/agent/ovn/metadata/agent.py``
- with a line number or link fragment tacked on: ``neutron/agent/ovn/metadata/agent.py:142`` or
  ``...agent.py#L142-L150``

:func:`normalize_path` strips all of that down to a clean, ``/``-joined relative path with known
container mount prefixes (``/workspace``, ``/root``, ``repo``, ``home/<user>``) removed.
:func:`paths_equivalent` then compares two normalised paths by their trailing path components, so
neither side has to guess the other's exact prefix depth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from segbench.agent.schema import Answer
from segbench.corpus.models import GroundTruth

_LINE_FRAGMENT_RE = re.compile(r"(#L\d+(-L?\d+)?|:\d+(:\d+)?)$")
#: Leading path segments that are container/repo mount artefacts, not part of the real path.
_DROP_ONE_SEGMENT = {"workspace", "root", "repo", "src", "a", "b"}


def normalize_path(raw: str) -> str:
    """Reduce a messy agent-supplied path to a clean, relative, ``/``-separated form.

    Never raises: an empty or unparseable input normalises to ``""``, which simply never matches
    anything.
    """
    s = raw.strip().strip("`\"' ")
    if not s:
        return ""
    s = s.replace("\\", "/")
    # Line-number / link-fragment suffixes can stack (rare but cheap to handle): strip repeatedly.
    while True:
        stripped = _LINE_FRAGMENT_RE.sub("", s)
        if stripped == s:
            break
        s = stripped
    s = s.strip()
    if s.startswith("./"):
        s = s[2:]
    parts = [p for p in s.split("/") if p not in ("", ".")]
    while parts:
        head = parts[0].lower()
        if head == "home" and len(parts) >= 2:
            parts = parts[2:]  # drop "home" and the username segment
            continue
        if head in _DROP_ONE_SEGMENT:
            parts = parts[1:]
            continue
        break
    return "/".join(parts)


def paths_equivalent(a: str, b: str) -> bool:
    """Whether two raw paths refer to the same file, after normalisation.

    Compares by trailing path components rather than requiring an exact match, since the two
    sides will rarely agree on how many leading directories to include. A single bare filename
    only matches another single bare filename component-for-component (matching ``agent.py``
    against every file named ``agent.py`` in a large repo would be a false-positive machine), but
    two-or-more-component suffixes matching is a strong signal.
    """
    na, nb = normalize_path(a), normalize_path(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    pa, pb = na.split("/"), nb.split("/")
    k = min(len(pa), len(pb))
    return pa[-k:] == pb[-k:]


def _normalize_component(raw: str) -> str:
    """Case- and separator-insensitive normalisation for component/package names."""
    return re.sub(r"[\s_-]+", "-", raw.strip().lower())


def component_match(answer: Answer, ground_truth: GroundTruth) -> bool:
    """Whether ``answer.component`` matches ``ground_truth.component`` or one of its aliases."""
    given = _normalize_component(answer.component)
    accepted = {_normalize_component(c) for c in ground_truth.components}
    return given in accepted


def file_hit(answer: Answer, ground_truth: GroundTruth) -> bool:
    """Whether any suspect file intersects the fix's file set."""
    return any(
        paths_equivalent(suspect, fixed)
        for suspect in answer.suspect_files
        for fixed in ground_truth.fix.files
    )


@dataclass(frozen=True)
class FileF1:
    precision: float
    recall: float
    f1: float


def file_f1(answer: Answer, ground_truth: GroundTruth) -> FileF1:
    """Precision/recall/F1 of ``answer.suspect_files`` against ``ground_truth.fix.files``.

    Matching a file counts it once on each side regardless of how many suspects or fix files it
    is equivalent to — this is what stops an agent from padding ``suspect_files`` with forty
    guesses to guarantee a recall hit (plan.md section 7.1).
    """
    predicted = answer.suspect_files
    truth = ground_truth.fix.files

    matched_pred = sum(1 for p in predicted if any(paths_equivalent(p, t) for t in truth))
    matched_truth = sum(1 for t in truth if any(paths_equivalent(p, t) for p in predicted))

    precision = matched_pred / len(predicted) if predicted else 0.0
    recall = matched_truth / len(truth) if truth else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return FileF1(precision=precision, recall=recall, f1=f1)


def _normalize_symbol(raw: str) -> str:
    s = raw.strip()
    s = s.replace("::", ".")
    s = re.sub(r"\(\s*\)$", "", s)  # trailing "()"
    return s.strip().lower()


def symbol_hit(answer: Answer, ground_truth: GroundTruth) -> bool | None:
    """Whether any suspect symbol intersects ``ground_truth.fix.symbols``.

    ``None`` — distinct from ``False`` — when the ground truth has no symbols at all, i.e. the
    bug's language has no supported symbol extractor (plan.md section 7.1).
    """
    truth = ground_truth.fix.symbols
    if truth is None:
        return None
    truth_norm = {_normalize_symbol(t) for t in truth}
    truth_last = {_normalize_symbol(t).rsplit(".", 1)[-1] for t in truth}
    for given in answer.suspect_symbols:
        norm = _normalize_symbol(given)
        if norm in truth_norm:
            return True
        if norm.rsplit(".", 1)[-1] in truth_last:
            return True
    return False


@dataclass(frozen=True)
class DeterministicResult:
    component_match: bool
    file_hit: bool
    file_f1: float
    symbol_hit: bool | None


def score_deterministic(answer: Answer, ground_truth: GroundTruth) -> DeterministicResult:
    """Run every deterministic check in one pass."""
    return DeterministicResult(
        component_match=component_match(answer, ground_truth),
        file_hit=file_hit(answer, ground_truth),
        file_f1=file_f1(answer, ground_truth).f1,
        symbol_hit=symbol_hit(answer, ground_truth),
    )
