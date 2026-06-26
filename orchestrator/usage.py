"""Token ledger — what the unit is actually burning against the Max subscription.

Roman is on the Claude **Max** plan: there is no per-call dollar charge, so the meaningful budget is
**token throughput** against the subscription. Every agent call (officer, builder, soldier, council,
chat) records one line here from a single choke-point — `agent.run_agent` — so the cockpit can show
*today / last 7d / last 30d* burn, and the autopilot can **pause itself** before a runaway loop eats
the subscription.

Append-only, best-effort (never raises into a run), self-pruning. Configured once at startup with
`configure(audit_path)`; if never configured (e.g. a unit test that doesn't care) every call no-ops.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from . import locking
from .config import Config

_PATH: Optional[Path] = None
_DAY = 86400.0


def configure(audit_path: str | os.PathLike) -> None:
    """Point the ledger at `<audit dir>/usage_ledger.jsonl`. Call once at process start."""
    global _PATH
    _PATH = Path(audit_path).with_name("usage_ledger.jsonl")


def _path(cfg: Config | None = None) -> Optional[Path]:
    if _PATH is not None:
        return _PATH
    if cfg is not None:
        return Path(cfg.audit_path).with_name("usage_ledger.jsonl")
    return None


def record(model: str, input_tokens: int, output_tokens: int,
           cost_usd: float = 0.0, tag: str = "",
           ticket_id: str | None = None, pass_number: int | None = None) -> None:
    """Log one agent call's token burn. Best-effort; silent on any failure.

    EU-38: a build/soldier pass also stamps its ticket id (`k`) + pass number (`p`) so per-pass
    INPUT tokens are sliceable by ticket — the real cost lever. Both are optional and only written
    when present, so officer/chat lines stay lean and the old ledger shape is unchanged.
    See scripts/ledger_analysis.py for the per-pass rollup."""
    p = _path()
    if p is None:
        return
    try:
        row = {
            "t": round(time.time(), 1),
            "m": (model or "").split("-")[1] if model and "-" in model else (model or "?"),
            "i": int(input_tokens or 0),
            "o": int(output_tokens or 0),
            "c": round(float(cost_usd or 0.0), 6),
            "g": tag or "",
        }
        if ticket_id:
            row["k"] = str(ticket_id)
        if pass_number is not None:
            try:
                row["p"] = int(pass_number)
            except (TypeError, ValueError):
                pass
        # Locked append: every agent call records here from many threads/processes at once; an
        # unlocked write can interleave and split a row mid-line (the F8 lost-write bug, generalised).
        locking.locked_append(p, json.dumps(row))
    except OSError:
        pass


def _rows(cfg: Config | None, since: float) -> list[dict]:
    p = _path(cfg)
    if p is None or not p.exists():
        return []
    out: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if float(r.get("t", 0)) >= since:
                out.append(r)
    except OSError:
        return []
    return out


def _day_start() -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def rollup(cfg: Config | None, since: float) -> dict:
    """Aggregate the ledger from `since` → now: totals + a per-model breakdown."""
    rows = _rows(cfg, since)
    total_in = sum(int(r.get("i", 0)) for r in rows)
    total_out = sum(int(r.get("o", 0)) for r in rows)
    by_model: dict[str, dict] = {}
    for r in rows:
        m = r.get("m", "?")
        b = by_model.setdefault(m, {"calls": 0, "in": 0, "out": 0, "cost": 0.0})
        b["calls"] += 1
        b["in"] += int(r.get("i", 0))
        b["out"] += int(r.get("o", 0))
        b["cost"] += float(r.get("c", 0.0))
    return {
        "calls": len(rows),
        "input": total_in,
        "output": total_out,
        "total": total_in + total_out,
        "cost": round(sum(float(r.get("c", 0.0)) for r in rows), 4),
        "by_model": by_model,
    }


def windows(cfg: Config | None = None) -> dict:
    """The three control windows the cockpit shows: today / last 7d / last 30d."""
    now = time.time()
    return {
        "today": rollup(cfg, _day_start()),
        "week": rollup(cfg, now - 7 * _DAY),
        "month": rollup(cfg, now - 30 * _DAY),
    }


def today_tokens(cfg: Config | None = None) -> int:
    return rollup(cfg, _day_start())["total"]


_CODE_TAGS = ("builder", "reviewer", "soldier")


def code_mix(cfg: Config | None, since: float) -> dict:
    """How the cost-driving CODE work (builder, reviewer, soldiers) split across model tiers since
    `since` — the readout that shows whether the economical ladder is actually shifting builds off Opus.
    `cheap_pct` = share of code calls that ran below Opus (Sonnet/Haiku); higher = more savings."""
    rows = [r for r in _rows(cfg, since)
            if any(str(r.get("g", "")).startswith(t) for t in _CODE_TAGS)]
    by = {"opus": 0, "sonnet": 0, "haiku": 0, "other": 0}
    for r in rows:
        m = str(r.get("m", "")).lower()
        by[m if m in by else "other"] += 1
    total = len(rows)
    cheap = by["sonnet"] + by["haiku"]
    return {"total": total, "by_tier": by, "cheap": cheap,
            "cheap_pct": (cheap / total) if total else 0.0,
            "opus_pct": (by["opus"] / total) if total else 0.0}


def budget_status(cfg: Config) -> dict:
    """Today's burn against the daily token ceiling. cap<=0 disables the budget (status 'off')."""
    cap = int(getattr(cfg, "daily_token_budget", 0) or 0)
    used = today_tokens(cfg)
    if cap <= 0:
        return {"on": False, "used": used, "cap": 0, "pct": 0.0, "over": False, "alert": False}
    pct = used / cap if cap else 0.0
    alert_pct = float(getattr(cfg, "budget_alert_pct", 0.8) or 0.8)
    return {"on": True, "used": used, "cap": cap, "pct": pct,
            "over": used >= cap, "alert": pct >= alert_pct}


def over_budget(cfg: Config) -> bool:
    """True when today's token burn has hit the daily ceiling (autopilot should pause)."""
    return budget_status(cfg)["over"]


def prune(cfg: Config | None = None, keep_days: int = 35) -> None:
    """Drop ledger lines older than keep_days so the file can't grow without bound."""
    p = _path(cfg)
    if p is None or not p.exists():
        return
    try:
        if p.stat().st_size < 400_000:
            return
        cutoff = time.time() - keep_days * _DAY
        kept = [ln for ln in p.read_text(encoding="utf-8").splitlines()
                if _safe_t(ln) >= cutoff]
        p.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    except OSError:
        pass


def _safe_t(line: str) -> float:
    try:
        return float(json.loads(line).get("t", 0))
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0.0
