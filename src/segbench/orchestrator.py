"""Run orchestration.

Responsibility: build the run matrix (bugs x environments x channel sets x models x repeats),
apply subsetting filters, skip cells that already have a successful run record (resumption),
execute with bounded concurrency under the campaign cost ceiling, and append run records to
``results/runs.jsonl``.

Implemented in phase 7.
"""
