"""Append-only JSONL audit log.

Every meaningful step is recorded so a run can be reconstructed and reviewed
after the fact — essential when the pipeline writes code unattended.

EU-363: the live file no longer grows forever (7.86MB / 13,262 lines at the
2026-07-16 total audit, re-parsed by every hot path). When an append finds the
file over ``ROTATE_MAX_BYTES``, events older than ``LIVE_WINDOW_DAYS`` move to
``<parent>/audit/archive-YYYY-MM.jsonl`` (month-keyed by each event's own ts)
and the live file is shrunk to the trailing window IN PLACE — truncate+rewrite
of the SAME inode, never rename-and-recreate, because every cross-process
writer flocks this inode via its own fd: a rename would strand a waiter's
append on the unlinked old file. Readers are already rotation-safe: dashboard's
EU-345 incremental reader names this exact case (dashboard.py:122) and falls
back to a full re-read on a size decrease / anchor mismatch. Hot paths thus
scale with the window, not all-time history; forensics and delta audits keep
the full log in the archive files.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
import time
from datetime import datetime
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

# EU-363 rotation policy. Size-triggered (no scheduler to install or drift): the first append past
# the threshold pays the one-off split. At the measured growth (~1.1k lines/day ≈ 7.4MB/month at
# filing) the 5MB default fires roughly monthly. The 14-day live window matches what the hot
# readers actually need — EU-358 bounded the widest loop reader to 48h, and the two deliberately
# unbounded guards (_already_pm_triaged, _planner_verdict_parked_before) both fail toward
# building/one extra triage by their own docstrings when an old event ages out.
ROTATE_MAX_BYTES = 5 * 1024 * 1024
LIVE_WINDOW_DAYS = 14.0
# After a pass that found nothing old enough to move (an over-size file of purely in-window
# events), don't re-parse the whole file on every append — retry after this long.
_ROTATE_RETRY_S = 3600.0


class AuditLog:
    def __init__(self, path: str | Path, *,
                 rotate_max_bytes: int | None = None,
                 live_window_days: float | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # None → module defaults, resolved at use so tests may patch the constants too.
        self._rotate_max_bytes = rotate_max_bytes
        self._live_window_days = live_window_days
        self._rotate_backoff_until = 0.0

    def record(self, event: str, **fields: Any) -> None:
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event}
        row.update({k: _coerce(v) for k, v in fields.items()})
        line = json.dumps(row, default=str) + "\n"
        with _WRITE_LOCK:
            self._maybe_rotate()
            with self.path.open("a", encoding="utf-8") as f:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(line)
                    f.flush()
                finally:
                    if fcntl is not None:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def _maybe_rotate(self) -> None:
        """Rotate the live file if it crossed the size threshold (EU-363).

        Called under ``_WRITE_LOCK``; takes the flock itself for the whole
        read→archive→truncate sequence so a concurrent process's append can
        never interleave with (or be lost to) the rewrite. NEVER raises —
        rotation is housekeeping and must not cost the append that tripped it.
        """
        try:
            limit = self._rotate_max_bytes if self._rotate_max_bytes is not None else ROTATE_MAX_BYTES
            if limit <= 0:  # explicit off-switch
                return
            if time.time() < self._rotate_backoff_until:
                return
            try:
                if self.path.stat().st_size <= limit:
                    return
            except OSError:
                return
            window = self._live_window_days if self._live_window_days is not None else LIVE_WINDOW_DAYS
            cutoff = time.time() - window * 86400.0
            with self.path.open("r+", encoding="utf-8") as f:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    keep: list[str] = []
                    buckets: dict[str, list[str]] = {}
                    for ln in f.read().splitlines():
                        s = ln.strip()
                        if not s:
                            continue
                        month = _archive_month(s, cutoff)
                        if month is None:
                            keep.append(s)
                        else:
                            buckets.setdefault(month, []).append(s)
                    if not buckets:
                        # Everything is in-window: back off so a permanently-busy fortnight
                        # doesn't re-parse the whole file on every single append.
                        self._rotate_backoff_until = time.time() + _ROTATE_RETRY_S
                        return
                    arch_dir = self.path.parent / "audit"
                    arch_dir.mkdir(parents=True, exist_ok=True)
                    # Archive FIRST, truncate AFTER: a crash between the two steps leaves the
                    # moved events in BOTH files (readers dedup exact lines; forensics tolerates
                    # a dupe) — the reverse order would silently lose history.
                    for month, rows in sorted(buckets.items()):
                        with (arch_dir / f"archive-{month}.jsonl").open("a", encoding="utf-8") as af:
                            af.write("\n".join(rows) + "\n")
                            af.flush()
                    f.seek(0)
                    f.truncate()
                    if keep:
                        f.write("\n".join(keep) + "\n")
                    f.flush()
                finally:
                    if fcntl is not None:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:  # noqa: BLE001 — a failed rotation must never block the append
            return

    @staticmethod
    def diff_hash(diff: str) -> str:
        return hashlib.sha256(diff.encode("utf-8")).hexdigest()[:12]


def _archive_month(line: str, cutoff: float) -> str | None:
    """The archive month key ('YYYY-MM') for an event line older than ``cutoff``, else None.

    Undatable lines (malformed JSON, missing/garbled ts) return None — a line we can't date
    stays in the live file: a few stragglers in the tail are cheaper than misfiled forensics.
    """
    try:
        ts = str(json.loads(line).get("ts", ""))
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return None
    epoch = _ts_epoch(ts)
    if epoch is None or epoch >= cutoff:
        return None
    month = ts[:7]
    if len(month) == 7 and month[4] == "-" and month[:4].isdigit() and month[5:].isdigit():
        return month
    return None


def _ts_epoch(ts: str) -> float | None:
    # record() writes local time with %z; tolerate a naive ts too (treated as local).
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt).timestamp()
        except ValueError:
            continue
    return None


def _coerce(v: Any) -> Any:
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return dataclasses.asdict(v)
    return v
