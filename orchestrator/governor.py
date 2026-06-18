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

from .config import Config

_WINDOW = 3600.0   # one rolling hour


def _file(cfg: Config) -> Path:
    return Path(cfg.audit_path).with_name("usage.jsonl")


def note_call(cfg: Config, n: int = 1) -> None:
    """Record n model calls happening now."""
    try:
        now = time.time()
        with _file(cfg).open("a", encoding="utf-8") as f:
            for _ in range(max(1, int(n))):
                f.write(json.dumps({"t": now}) + "\n")
    except OSError:
        pass


def calls_last_hour(cfg: Config) -> int:
    """Calls recorded in the last hour (prunes the file when it grows large)."""
    p = _file(cfg)
    if not p.exists():
        return 0
    cutoff = time.time() - _WINDOW
    kept: list[float] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                t = float(json.loads(line).get("t", 0))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if t >= cutoff:
                kept.append(t)
    except OSError:
        return 0
    try:
        if p.stat().st_size > 200_000:
            p.write_text("".join(json.dumps({"t": t}) + "\n" for t in kept), encoding="utf-8")
    except OSError:
        pass
    return len(kept)


def under_budget(cfg: Config) -> bool:
    """True if a discretionary discussion is allowed right now. cap<=0 disables the governor."""
    cap = int(getattr(cfg, "usage_cap_per_hour", 0) or 0)
    if cap <= 0:
        return True
    return calls_last_hour(cfg) < cap
