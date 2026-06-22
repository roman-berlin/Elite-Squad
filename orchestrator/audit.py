"""Append-only JSONL audit log.

Every meaningful step is recorded so a run can be reconstructed and reviewed
after the fact — essential when the pipeline writes code unattended.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

try:  # POSIX advisory file locking; absent on Windows.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

# Module-level lock serialises appends from threads sharing this process (cockpit
# run thread, autopilot, decisions poller). The fcntl.flock below serialises across
# separate processes that may also append to the same file.
_WRITE_LOCK = threading.Lock()


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **fields: Any) -> None:
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event}
        row.update({k: _coerce(v) for k, v in fields.items()})
        line = json.dumps(row, default=str) + "\n"
        with _WRITE_LOCK:
            with self.path.open("a", encoding="utf-8") as f:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(line)
                    f.flush()
                finally:
                    if fcntl is not None:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def diff_hash(diff: str) -> str:
        return hashlib.sha256(diff.encode("utf-8")).hexdigest()[:12]


def _coerce(v: Any) -> Any:
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return dataclasses.asdict(v)
    return v
