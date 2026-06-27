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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from . import locking
from .config import Config

_PATH: Optional[Path] = None
_DAY = 86400.0

# EU-76 daily-burn series cache. The cockpit board renders a 14-day token-burn sparkline on every SSE
# frame / open tab; re-reading + JSON-parsing the whole ledger each render repeats work the ledger's
# (size, mtime_ns) signature shows is unchanged. Cache the computed series keyed on that signature (plus
# the day window) so a burst of frames shares ONE read, invalidating the instant the ledger is appended
# to — the same shape as dashboard._audit_cache keeps for the audit.
_burn_series_cache: dict[str, tuple] = {}   # ledger path -> (sig, day_keys, series)


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
    # EU-84: a model call is a liveness signal — bump last_activity for every active run.
    # During soldier-build delegation the soldier's stdout never flows through bump_log_seq
    # for the parent, so last_activity freezes at delegation time and the cockpit liveness
    # chip falsely shows "⚠ no step for Nm — may be stuck".  A model call proves the run
    # is alive at least as strongly as a printed step, so we propagate the heartbeat here.
    #
    # KNOWN LIMITATION — cross-run heartbeat contamination (EU-84 review, iter 2): the ledger
    # line records WHICH model call happened but not WHICH run owns it (run_agent is a shared
    # choke-point with no app attribution), so when two or more runs are active at once we bump
    # last_activity for ALL of them.  A model call belonging to run A therefore keeps run B's
    # liveness chip green even if B is genuinely stuck.  This is deliberately accepted for now:
    # the common case is a single active run (max_parallel_runs defaults to 1), and the failure
    # mode is a *false-green* on a co-running stuck run — strictly less harmful than the
    # false-"stuck" this ticket fixes.  The clean fix (thread an `app_key` from run_agent so the
    # heartbeat bumps only the owning run) is deferred to a follow-up; test 8 in
    # tests/liveness_heartbeat_test.py pins this current "bump all active runs" behaviour so the
    # contamination is documented and any future change to it is a conscious one.
    try:
        from . import cockpit_state as _cs  # local import — avoids any circular-import risk
        _now = time.time()
        for _app_key in _cs.active_runs():
            _cs.get_state(_app_key)["last_activity"] = _now
    except Exception:  # noqa: BLE001 — heartbeat must never break a run
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


def tokens_today_for_tag(cfg: Config | None, prefix: str) -> int:
    """Today's total (input+output) tokens for ledger lines whose tag starts with `prefix`.

    Lets a sub-channel (e.g. the EU-65 liaison) meter its OWN burn against a dedicated cap
    without touching the unit's global daily budget."""
    if not prefix:
        return 0
    return sum(int(r.get("i", 0)) + int(r.get("o", 0))
               for r in _rows(cfg, _day_start())
               if str(r.get("g", "")).startswith(prefix))


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


def plan_usage(cfg: Config | None = None) -> dict:
    """Session (today) and weekly token totals from the local ledger.

    EU-77 spike result: the Max plan exposes no public machine-readable API for
    subscription quota — Anthropic's billing endpoints are scoped to pay-per-call
    API keys only, not Max seats. The local ``usage_ledger.jsonl`` is therefore the
    authoritative source for all plan-usage metrics. 'Session' is the rolling
    calendar day (same window as ``budget_status``); 'weekly' is a 7-day rolling
    window. Returns zero counts gracefully when the ledger is absent or unconfigured.
    """
    w = windows(cfg)
    today = w["today"]
    week = w["week"]
    return {
        "session": today["total"],        # input + output tokens for today
        "session_calls": today["calls"],
        "weekly": week["total"],           # rolling 7-day window
        "weekly_calls": week["calls"],
    }


def daily_burn_series(cfg: Config | None = None, days: int = 14) -> list[float]:
    """Per-calendar-day token COST (USD) for the last `days` days, oldest→newest (EU-76 board sparkline).

    Reads ``usage_ledger.jsonl`` at most once per (size, mtime_ns) change — the computed series is cached
    on that signature, so repeat cockpit frames cost a ``stat()`` rather than a full ledger parse. Day
    buckets are built with calendar-date arithmetic (``date.today() - timedelta(days=k)``) so a DST
    transition can't collapse or skip a day. Returns all-zeros when the ledger is absent or unreadable.
    """
    today = date.today()
    day_keys = [(today - timedelta(days=k)).isoformat() for k in range(days - 1, -1, -1)]
    p = _path(cfg)
    if p is None or not p.exists():
        return [0.0] * days
    key = str(p)
    try:
        st = p.stat()
        sig = (st.st_size, st.st_mtime_ns)
    except OSError:
        return [0.0] * days
    hit = _burn_series_cache.get(key)
    if hit is not None and hit[0] == sig and hit[1] == day_keys:
        return list(hit[2])             # copy so callers can't mutate the cached series
    day_set = set(day_keys)
    totals: dict[str, float] = {k: 0.0 for k in day_keys}
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                ds = datetime.fromtimestamp(float(r.get("t", 0) or 0)).strftime("%Y-%m-%d")
            except (OverflowError, OSError, ValueError):
                continue
            if ds in day_set:
                totals[ds] += float(r.get("c", 0.0) or 0.0)
    except OSError:
        return [0.0] * days
    series = [totals[k] for k in day_keys]
    _burn_series_cache[key] = (sig, day_keys, series)
    return series


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
