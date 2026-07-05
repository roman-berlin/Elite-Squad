"""Usage governor — keep the always-on server frugal so it never hits the Max limit.

Officer discussions already run on cheap models (Sonnet/Haiku); this also caps the *discretionary*
chatter (corridor small-talk, spontaneous meetings) to a rolling-hour budget. The necessary daily
muster and your interactive chats always run — only the optional extras defer when the hour is already
busy. Tracked in a tiny append-only file beside the audit log; fully best-effort (never raises).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import locking
from .config import Config

_WINDOW = 3600.0   # one rolling hour


def _file(cfg: Config) -> Path:
    return Path(cfg.audit_path).with_name("usage.jsonl")


def note_call(cfg: Config, n: int = 1) -> None:
    """Record n model calls happening now."""
    try:
        now = time.time()
        # One locked append for all n lines: concurrent corridor/meeting bursts share this file, so an
        # unlocked write can interleave and corrupt a row (the F8 pattern, now via the shared helper).
        block = "".join(json.dumps({"t": now}) + "\n" for _ in range(max(1, int(n))))
        locking.locked_append(_file(cfg), block)
    except OSError:
        pass


def _row_t(line: str) -> float:
    try:
        return float(json.loads(line).get("t", 0))
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0.0


def calls_last_hour(cfg: Config) -> int:
    """Calls recorded in the last hour (prunes the file when it grows large)."""
    p = _file(cfg)
    if not p.exists():
        return 0
    cutoff = time.time() - _WINDOW
    kept: list[float] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            t = _row_t(line)
            if t >= cutoff:
                kept.append(t)
    except OSError:
        return 0
    try:
        if p.stat().st_size > 200_000:
            # 2026-07-05 audit §7.4: the old unlocked write_text raced note_call's locked_append —
            # a row landing between this function's read and its rewrite was truncated away, and
            # the rewrite could split an in-flight append. locked_rewrite re-reads and filters
            # INSIDE the same data-file flock the appenders take, so nothing lands in the gap.
            locking.locked_rewrite(p, lambda lines: [ln for ln in lines if _row_t(ln) >= cutoff])
    except OSError:
        pass
    return len(kept)


def under_budget(cfg: Config) -> bool:
    """True if a discretionary discussion is allowed right now. cap<=0 disables the governor."""
    cap = int(getattr(cfg, "usage_cap_per_hour", 0) or 0)
    if cap <= 0:
        return True
    return calls_last_hour(cfg) < cap
