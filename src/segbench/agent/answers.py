"""Polls ``/workspace/answer.json`` during a run and keeps every distinct version (plan.md
section 10: "do snapshot it — cheap now, impossible to add retroactively").

Runs on a background thread concurrently with :func:`segbench.agent.opencode.invoke_agent`'s
blocking wait, so a run that gets killed at the wall clock still has whatever the agent last
managed to save. Each *distinct* version (by content hash) is kept as ``answers/NN.json``, in
order, so answer evolution — did it improve on its first draft? — is analysable later; identical
consecutive polls are not re-saved.
"""

from __future__ import annotations

import hashlib
import tempfile
import threading
from pathlib import Path

from segbench.logging import get_logger
from segbench.runtime.base import Handle, Runtime, RuntimeFailure

log = get_logger(__name__)

DEFAULT_INTERVAL_S = 5.0


class AnswerSnapshotter:
    """Background poller. Use as a context manager around the agent invocation."""

    def __init__(
        self,
        runtime: Runtime,
        handle: Handle,
        *,
        remote_path: str,
        out_dir: Path,
        interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        self._runtime = runtime
        self._handle = handle
        self._remote_path = remote_path
        self._out_dir = Path(out_dir)
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_hash: str | None = None
        self._count = 0
        self._lock = threading.Lock()

    @property
    def snapshot_count(self) -> int:
        with self._lock:
            return self._count

    def _poll_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="segbench-answer-poll-") as tmp:
            local = Path(tmp) / "answer.json"
            try:
                self._runtime.pull(self._handle, self._remote_path, local)
            except RuntimeFailure:
                return  # file does not exist yet, or the container is mid-teardown
            if not local.is_file():
                return
            data = local.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            with self._lock:
                if digest == self._last_hash:
                    return
                self._last_hash = digest
                self._count += 1
                index = self._count
            self._out_dir.mkdir(parents=True, exist_ok=True)
            (self._out_dir / f"{index:02d}.json").write_bytes(data)
            log.debug(
                "answer snapshot captured",
                extra={"container": self._handle.name, "index": index, "bytes": len(data)},
            )

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            self._poll_once()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name=f"segbench-answer-poll-{self._handle.name}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop polling and take one final snapshot, in case the agent's last write landed
        between two polling intervals."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval_s + 5.0)
        self._poll_once()

    def __enter__(self) -> AnswerSnapshotter:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
