"""``/workspace/answer.json`` schema (plan.md section 2) and its validator.

The agent's entire graded output is one file. This module is the single source of truth for its
shape: the pydantic model, the JSON Schema handed to the agent in the prompt (see
:mod:`segbench.agent.prompt`), and the validator that turns "a file exists in the container" into
either a parsed :class:`Answer` or a structured reason it is not gradeable.

The reason matters as much as the yes/no: plan.md section 7.3 scores ``no_answer`` (the agent
never wrote the file) and ``invalid_answer`` (it wrote something that does not parse or does not
conform) both as zero, but they are different failure modes and the dashboard's failure-mode
breakdown needs to tell them apart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ANSWER_FILENAME = "answer.json"


class Answer(BaseModel):
    """``/workspace/answer.json`` (plan.md section 2).

    Strict: an extra key almost always means the agent did not read the schema, and it is better
    to fail loudly at grading time than to silently drop it. ``confidence`` and
    ``uncertain_about`` are not scored in v1 but are recorded (plan.md section 2) — calibration
    analysis is cheap to add later and impossible to add retroactively.
    """

    model_config = ConfigDict(extra="forbid")

    root_cause: str = Field(
        min_length=1, description="Prose, 1-6 sentences: what is actually wrong and why."
    )
    component: str = Field(
        min_length=1,
        description="Package or project the defect lives in, e.g. 'nova' or 'juju/juju'.",
    )
    suspect_files: list[str] = Field(default_factory=list, description="Paths from the repo root.")
    suspect_symbols: list[str] = Field(
        default_factory=list, description="e.g. 'ClassName.method_name'."
    )
    proposed_fix: str = Field(min_length=1, description="Prose: what change would resolve it.")
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(
        default_factory=list, description="Which visible channels or files led you here."
    )
    uncertain_about: list[str] = Field(
        default_factory=list,
        description="What you could not determine, and what would resolve it.",
    )


#: The JSON Schema handed to the agent in the prompt (plan.md section 10, point 4).
ANSWER_JSON_SCHEMA: dict[str, Any] = Answer.model_json_schema()


class AnswerOutcome(StrEnum):
    """The subset of plan.md section 7.3's ``outcome`` enum this module can determine on its own.

    ``timeout``, ``cost_exceeded`` and ``harness_error`` are decided by the run loop
    (:mod:`segbench.agent.run`), not here — this module only ever sees a file (or its absence).
    """

    OK = "ok"
    NO_ANSWER = "no_answer"
    INVALID_ANSWER = "invalid_answer"


@dataclass
class AnswerResult:
    """Either a parsed, schema-valid :class:`Answer`, or a structured reason it is not gradeable."""

    outcome: AnswerOutcome
    answer: Answer | None = None
    raw_text: str | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is AnswerOutcome.OK


def _format_validation_error(exc: ValidationError) -> str:
    lines = [f"{exc.error_count()} schema error(s):"]
    for error in exc.errors():
        field = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  - {field}: {error['msg']}")
    return "\n".join(lines)


def validate_answer_text(text: str | None) -> AnswerResult:
    """Validate raw file contents (or ``None`` for a missing file) against the schema."""
    if text is None:
        return AnswerResult(outcome=AnswerOutcome.NO_ANSWER, reason="answer.json does not exist")
    if not text.strip():
        return AnswerResult(outcome=AnswerOutcome.NO_ANSWER, reason="answer.json is empty")

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        return AnswerResult(
            outcome=AnswerOutcome.INVALID_ANSWER,
            raw_text=text,
            reason=f"answer.json is not valid JSON: {exc}",
        )
    if not isinstance(raw, dict):
        return AnswerResult(
            outcome=AnswerOutcome.INVALID_ANSWER,
            raw_text=text,
            reason=f"answer.json must be a JSON object, found {type(raw).__name__}",
        )

    try:
        answer = Answer.model_validate(raw)
    except ValidationError as exc:
        return AnswerResult(
            outcome=AnswerOutcome.INVALID_ANSWER,
            raw_text=text,
            reason=_format_validation_error(exc),
        )
    return AnswerResult(outcome=AnswerOutcome.OK, answer=answer, raw_text=text)


def validate_answer_file(path: Path) -> AnswerResult:
    """Validate a file on disk. A missing file is :attr:`AnswerOutcome.NO_ANSWER`, not an error."""
    path = Path(path)
    if not path.is_file():
        return AnswerResult(outcome=AnswerOutcome.NO_ANSWER, reason=f"{path} does not exist")
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        return AnswerResult(
            outcome=AnswerOutcome.INVALID_ANSWER, reason=f"{path} could not be read: {exc}"
        )
    return validate_answer_text(text)
