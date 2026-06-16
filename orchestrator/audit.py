"""Append-only JSONL audit log.

Every meaningful step is recorded so a run can be reconstructed and reviewed
after the fact — essential when the pipeline writes code unattended.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **fields: Any) -> None:
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event}
        row.update({k: _coerce(v) for k, v in fields.items()})
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

    @staticmethod
    def diff_hash(diff: str) -> str:
        return hashlib.sha256(diff.encode("utf-8")).hexdigest()[:12]


def _coerce(v: Any) -> Any:
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return dataclasses.asdict(v)
    return v
