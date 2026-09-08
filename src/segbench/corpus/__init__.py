"""Corpus layer.

Responsibility: load bug directories under ``corpus/bugs/<bug_id>/``, validate ``bug.yaml`` and
``ground_truth.yaml`` against their schemas, enforce the closed channel vocabulary, run the
leakage and scrubber checks, and scaffold new bug directories from a tracker URL.

Implemented in phase 1.
"""
