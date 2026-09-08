"""Corpus layer.

Responsibility: load bug directories under ``corpus/bugs/<bug_id>/``, validate ``bug.yaml`` and
``ground_truth.yaml`` against their schemas, enforce the closed channel vocabulary, run the
leakage and scrubber checks, derive the fix's files and symbols from the fix commit, and scaffold
new bug directories from a tracker URL.

Module map:

* :mod:`~segbench.corpus.models` — the schemas (plan.md sections 3.2, 3.3, 4).
* :mod:`~segbench.corpus.loader` — directory to :class:`~segbench.corpus.models.Bug`, loudly.
* :mod:`~segbench.corpus.scrub` — credentials, addresses, cloud ids, customer hostnames.
* :mod:`~segbench.corpus.leakage` — the anti-leak checks (plan.md section 3.4).
* :mod:`~segbench.corpus.validate` — composes the three into one pass with one exit rule.
* :mod:`~segbench.corpus.derive` — fix files and symbols from the commit.
* :mod:`~segbench.corpus.add` — tracker scaffolding.
"""

from segbench.corpus.findings import Finding, Severity
from segbench.corpus.loader import CorpusError, load_bug, load_corpus
from segbench.corpus.models import (
    Bug,
    BugManifest,
    ChannelId,
    GroundTruth,
    LoadedChannel,
    Origin,
    Product,
    Provenance,
    Tracker,
)
from segbench.corpus.scrub import ScrubPolicy
from segbench.corpus.validate import BugReport, CorpusReport, validate_bug, validate_corpus

__all__ = [
    "Bug",
    "BugManifest",
    "BugReport",
    "ChannelId",
    "CorpusError",
    "CorpusReport",
    "Finding",
    "GroundTruth",
    "LoadedChannel",
    "Origin",
    "Product",
    "Provenance",
    "ScrubPolicy",
    "Severity",
    "Tracker",
    "load_bug",
    "load_corpus",
    "validate_bug",
    "validate_corpus",
]
