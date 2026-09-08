"""The pinned LLM judge (plan.md section 7.2).

CLAUDE.md invariant 3 ("the judge is never a model under test") and invariant 5 ("grading never
sees the transcript's reasoning about the bug tracker") are both enforced here in code, not just
in the prompt:

- :func:`call_judge` raises :class:`JudgeError` if ``settings.judge.model`` appears anywhere in
  the model matrix under test, checked *before* any request is sent.
- :class:`JudgeInput` is the only thing that can be rendered into the prompt, and it has exactly
  four fields: the ground truth root cause, its acceptable alternates, and the agent's
  ``root_cause``/``proposed_fix``. There is structurally nowhere to put a transcript, a suspect
  file list, or a tracker URL — the model can't leak what it was never given.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from segbench.config import ModelConfig, Settings
from segbench.logging import get_logger

log = get_logger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
#: Bounded retries against an unparseable judge response (plan.md section 7.2).
MAX_JUDGE_ATTEMPTS = 3


class JudgeError(Exception):
    """The judge could not be called, or never returned parseable JSON within budget."""


class JudgeInput(BaseModel):
    """Exactly what the judge is allowed to see. Nothing else can reach the prompt template."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ground_truth_root_cause: str
    also_acceptable_root_causes: list[str] = Field(default_factory=list)
    agent_root_cause: str
    agent_proposed_fix: str


class JudgeResult(BaseModel):
    """The judge's strict JSON response (plan.md section 7.2)."""

    model_config = ConfigDict(extra="forbid")

    root_cause_score: int = Field(ge=0, le=3)
    root_cause_rationale: str
    fix_score: int = Field(ge=0, le=3)
    fix_rationale: str
    contradicts_ground_truth: bool
    unsupported_specifics: int = Field(ge=0)


@lru_cache(maxsize=8)
def _load_template(prompt_version: str) -> str:
    path = _TEMPLATES_DIR / f"judge_prompt_{prompt_version}.md"
    if not path.is_file():
        raise JudgeError(f"no judge rubric template for prompt_version {prompt_version!r}: {path}")
    return path.read_text(encoding="utf-8")


def render_judge_prompt(judge_input: JudgeInput, *, prompt_version: str) -> str:
    """Render the versioned rubric template against a :class:`JudgeInput`, and nothing else."""
    template = _load_template(prompt_version)
    alternates = (
        "\n".join(f"- {alt}" for alt in judge_input.also_acceptable_root_causes)
        if judge_input.also_acceptable_root_causes
        else "(none recorded)"
    )
    return template.format(
        ground_truth_root_cause=judge_input.ground_truth_root_cause,
        also_acceptable_root_causes=alternates,
        agent_root_cause=judge_input.agent_root_cause,
        agent_proposed_fix=judge_input.agent_proposed_fix,
    )


def assert_judge_not_under_test(
    settings: Settings, models: list[ModelConfig] | None = None
) -> None:
    """Hard-fail if the judge model is in the model matrix under test (CLAUDE.md invariant 3)."""
    matrix = models if models is not None else settings.models
    offenders = [m.id for m in matrix if m.id == settings.judge.model]
    if offenders:
        raise JudgeError(
            f"judge model {settings.judge.model!r} appears in the model matrix under test; "
            f"the judge must never be a model under test (CLAUDE.md invariant 3)."
        )


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json_object(text: str) -> str:
    """Best-effort extraction of a JSON object from a response that may have markdown fences or
    stray prose around it — the judge is asked for strict JSON but is still an LLM."""
    text = text.strip()
    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        return fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


def _parse_judge_response(raw_text: str) -> JudgeResult:
    candidate = _extract_json_object(raw_text)
    data = json.loads(candidate)  # may raise json.JSONDecodeError
    return JudgeResult.model_validate(data)  # may raise ValidationError


def _request_completion(client: httpx.Client, settings: Settings, prompt: str) -> str:
    response = client.post(
        f"{settings.provider.base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {settings.provider.api_key()}"},
        json={
            "model": settings.judge.model,
            "temperature": settings.judge.temperature,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60.0,
    )
    response.raise_for_status()
    body = response.json()
    return body["choices"][0]["message"]["content"]


def call_judge(
    settings: Settings,
    judge_input: JudgeInput,
    *,
    models: list[ModelConfig] | None = None,
    client: httpx.Client | None = None,
) -> JudgeResult:
    """Call the pinned judge model and return its parsed, validated response.

    Retries a bounded number of times only on an unparseable response (bad JSON, or JSON that
    fails the schema) — not on transport errors, which are raised immediately as
    :class:`JudgeError`.
    """
    assert_judge_not_under_test(settings, models)
    prompt = render_judge_prompt(judge_input, prompt_version=settings.judge.prompt_version)

    owns_client = client is None
    http_client = client or httpx.Client()
    last_error: Exception | None = None
    try:
        for attempt in range(1, MAX_JUDGE_ATTEMPTS + 1):
            try:
                raw_text = _request_completion(http_client, settings, prompt)
            except httpx.HTTPError as exc:
                raise JudgeError(f"judge request failed: {exc}") from exc
            try:
                return _parse_judge_response(raw_text)
            except (json.JSONDecodeError, ValidationError, KeyError, TypeError) as exc:
                last_error = exc
                log.warning(
                    "judge response unparseable, retrying",
                    extra={"attempt": attempt, "error": str(exc)},
                )
    finally:
        if owns_client:
            http_client.close()

    raise JudgeError(
        f"judge never returned a parseable response after {MAX_JUDGE_ATTEMPTS} attempt(s): "
        f"{last_error}"
    )
