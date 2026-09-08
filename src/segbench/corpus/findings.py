"""The common finding record produced by the scrubber and the leakage checks.

Both check families report the same shape so ``segbench corpus validate`` can render one table and
apply one exit-code rule: any finding at :attr:`Severity.ERROR` fails the corpus.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class Severity(StrEnum):
    """``ERROR`` fails validation; ``WARNING`` is surfaced for a human to judge."""

    ERROR = "error"
    WARNING = "warning"


class Finding(BaseModel):
    """One problem found in one place.

    ``excerpt`` is a redacted rendering of the offending text: the scrubber must never print a
    credential it just found into a terminal or a log file, so the middle of every match is
    replaced with an ellipsis by :func:`redact`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    bug_id: str
    detector: str
    severity: Severity
    file: Path
    line: int | None = None
    message: str
    excerpt: str | None = None

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}" if self.line is not None else str(self.file)

    def __str__(self) -> str:
        return (
            f"[{self.severity.value}] {self.bug_id} {self.location} {self.detector}: {self.message}"
        )


def redact(value: str, *, keep: int = 3) -> str:
    """Render a matched secret safely: keep a few leading characters, elide the rest.

    Short values are elided entirely — keeping three characters of a four-character token is not
    redaction.
    """
    value = value.strip()
    if len(value) <= keep * 2:
        return "***"
    return f"{value[:keep]}...{value[-keep:]}"
