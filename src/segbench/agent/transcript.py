"""Normalises an ``opencode export`` session dump into ``transcript.jsonl`` (plan.md section 10).

opencode's session export (``opencode export <id> --sanitize``) is a documented, purpose-built
command for exactly this: dumping one session's messages as JSON. Its precise shape is not
something this repository can pin against a real binary, so this module treats the shape it
expects as a **contract**, checked explicitly, rather than something to parse permissively. A
session that does not match — an unrecognised top-level structure, a message with no role — fails
loudly with :class:`TranscriptError` instead of silently producing an empty or truncated
transcript, per plan.md section 10's requirement that a format change must not go unnoticed.

Expected shape (opencode's internal session/message/part model):

```json
{
  "info": {"id": "...", ...},
  "messages": [
    {
      "info": {"id": "...", "role": "user" | "assistant", "time": {"created": 1700000000000},
                "tokens": {"input": 0, "output": 0} },
      "parts": [
        {"type": "text", "text": "..."},
        {"type": "tool", "tool": "bash", "state": {"input": {...}, "output": "..."}}
      ]
    }
  ]
}
```

Unrecognised *part* types are skipped (opencode adds new ones over time and that alone is not a
sign anything is broken); a missing ``messages`` list, or a message missing ``info.role``, is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class TranscriptError(Exception):
    """The session export does not match the shape this extractor understands."""


@dataclass
class ToolCall:
    tool: str
    input: Any = None
    output: Any = None


@dataclass
class TranscriptMessage:
    """One normalised message: ``role``, text ``content``, any tool calls, tokens, timestamp."""

    role: str
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)
    timestamp_ms: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": [
                {"tool": tc.tool, "input": tc.input, "output": tc.output} for tc in self.tool_calls
            ],
            "tokens": self.tokens,
            "timestamp_ms": self.timestamp_ms,
        }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TranscriptError(
            f"opencode session export did not match the expected shape: {message}"
        )


def _extract_message(raw: Any, index: int) -> TranscriptMessage:
    _require(isinstance(raw, dict), f"message {index} is not an object")
    info = raw.get("info")
    _require(isinstance(info, dict), f"message {index} has no 'info' object")
    role = info.get("role")
    _require(isinstance(role, str) and bool(role), f"message {index} 'info.role' is missing")

    parts = raw.get("parts", [])
    _require(isinstance(parts, list), f"message {index} 'parts' is not a list")

    text_chunks: list[str] = []
    tool_calls: list[ToolCall] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text", "")
            if isinstance(text, str):
                text_chunks.append(text)
        elif part_type == "tool":
            tool_name = part.get("tool")
            if isinstance(tool_name, str):
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                tool_calls.append(
                    ToolCall(tool=tool_name, input=state.get("input"), output=state.get("output"))
                )
        # Any other part type (step-start, file, patch, ...) is intentionally ignored: new part
        # types appearing over time is not evidence the export format itself has changed.

    time_info = info.get("time") if isinstance(info.get("time"), dict) else {}
    timestamp = time_info.get("created")
    tokens_raw = info.get("tokens") if isinstance(info.get("tokens"), dict) else {}
    tokens = {k: v for k, v in tokens_raw.items() if isinstance(v, int)}

    return TranscriptMessage(
        role=role,
        content="".join(text_chunks),
        tool_calls=tool_calls,
        tokens=tokens,
        timestamp_ms=timestamp if isinstance(timestamp, int) else None,
    )


def extract_transcript(export_text: str) -> list[TranscriptMessage]:
    """Parse ``opencode export``'s stdout into a list of :class:`TranscriptMessage`.

    Raises :class:`TranscriptError` on anything that does not match the documented module
    contract — never returns an empty list as a way of swallowing a parse failure.
    """
    if not export_text.strip():
        raise TranscriptError("opencode session export produced no output")
    try:
        raw = json.loads(export_text)
    except json.JSONDecodeError as exc:
        raise TranscriptError(f"opencode session export is not valid JSON: {exc}") from exc

    _require(isinstance(raw, dict), "top level is not a JSON object")
    messages = raw.get("messages")
    _require(isinstance(messages, list), "no 'messages' list at the top level")

    return [_extract_message(message, i) for i, message in enumerate(messages)]


def write_transcript(messages: list[TranscriptMessage], path: Path) -> None:
    """Write the normalised transcript, one JSON object per line."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for message in messages:
            fh.write(json.dumps(message.to_json(), default=str) + "\n")
