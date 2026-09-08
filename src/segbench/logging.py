"""Structured logging.

Two sinks, configured once at CLI startup by :func:`configure_logging`:

* a **file** sink writing one JSON object per line, for machine consumption and post-hoc debugging
  of a campaign;
* a **console** sink using rich, for a human watching a run.

Convention: every module does ``log = get_logger(__name__)`` at import time and never configures
handlers itself. Extra structured fields go through the ``extra`` mapping, e.g.
``log.info("run finished", extra={"run_id": rid, "outcome": "ok"})`` — they are merged into the
JSON line and are not repeated in the console message.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.logging import RichHandler

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}

console = Console(stderr=True)


def get_logger(name: str) -> logging.Logger:
    """Return the logger for a module. Call as ``get_logger(__name__)``."""
    return logging.getLogger(name)


class JsonLinesFormatter(logging.Formatter):
    """Render a record as a single JSON object, including any ``extra`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(*, log_file: Path | None = None, verbose: bool = False) -> None:
    """Install the console and file handlers on the ``segbench`` root logger.

    Idempotent: existing handlers are removed first, so repeated calls (tests, nested commands)
    do not duplicate output.
    """
    root = logging.getLogger("segbench")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.propagate = False

    console_handler = RichHandler(
        console=console,
        show_path=verbose,
        rich_tracebacks=True,
        markup=False,
    )
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(message)s", datefmt="%H:%M:%S"))
    root.addHandler(console_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(JsonLinesFormatter())
        root.addHandler(file_handler)
