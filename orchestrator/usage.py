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
import shutil
import subprocess
import tempfile
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

# ── EU-77: live Claude Max subscription-limit probe ─────────────────────────────────────────────
# The Max plan's REAL ceiling (the rolling 5-hour "session", the 7-day "weekly · all models", and the
# per-model weekly windows) is NOT in any public billing API — those are scoped to pay-per-call API
# keys, not Max seats. It IS, however, emitted by Claude Code itself: a headless
# `claude -p . --output-format stream-json --verbose` run prints one `rate_limit_event` per active
# unified limit, each carrying {utilization 0-1, resetsAt epoch, rateLimitType, status}. That is the
# exact source Claude Code's own `/usage` panel reads, so we read it the same way — with a throwaway
# 1-token probe.
#
# Cost & safety (per the EU-77 product decision):
#   • the probe fires ONLY from an actual `/usage` cockpit render — never on a background timer — so an
#     idle cockpit burns zero quota (the board KPI cards read the local ledger via windows(), below);
#   • results are cached for 5 minutes (a failed/empty read for 1 minute) so a burst of refreshes
#     shares ONE call (≤~12 probes/hour worst case, far fewer in practice);
#   • everything is best-effort — any failure returns {"available": False} and the cockpit falls back
#     to the EU-75 own-ledger gauge with a "not machine-readable" note. The probe never raises.
_PLAN_PROBE_TIMEOUT = 45.0      # hard cap on the throwaway CLI call (it normally returns in <10s)
_PLAN_TTL_OK = 300.0           # cache a good read for 5 minutes
_PLAN_TTL_ERR = 60.0           # re-try a failed/empty probe sooner
_plan_cache: dict = {}         # {"ts": <epoch>, "data": <plan_usage() result>}

# rateLimitType → (stable key, brand label). Unknown/future types are prettified on the fly.
_PLAN_LIMIT_LABELS = {
    "five_hour": ("session", "Current session"),
    "seven_day": ("weekly", "Weekly · All models"),
    "seven_day_opus": ("weekly_opus", "Weekly · Opus"),
    "seven_day_sonnet": ("weekly_sonnet", "Weekly · Sonnet"),
    "seven_day_haiku": ("weekly_haiku", "Weekly · Haiku"),
}
# Stable display order: session first, then all-models weekly, then per-model.
_PLAN_LIMIT_ORDER = ["session", "weekly", "weekly_opus", "weekly_sonnet", "weekly_haiku"]


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
           ticket_id: str | None = None, pass_number: int | None = None,
           provider: str = "") -> None:
    """Log one agent call's token burn. Best-effort; silent on any failure.

    EU-38: a build/soldier pass also stamps its ticket id (`k`) + pass number (`p`) so per-pass
    INPUT tokens are sliceable by ticket — the real cost lever. Both are optional and only written
    when present, so officer/chat lines stay lean and the old ledger shape is unchanged.
    See scripts/ledger_analysis.py for the per-pass rollup.
    EU-123: also records provider information ("Anthropic" or "GLM")."""
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
        if provider:
            row["prv"] = str(provider)
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


def _fmt_reset_in(resets_at, now: float) -> str:
    """'6d 4h' / '3h 20m' / '12m' / '<1m' from an epoch-seconds reset time, '' if unknown."""
    try:
        secs = int(resets_at) - int(now)
    except (TypeError, ValueError):
        return ""
    if secs <= 0:
        return "now"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m"
    return "<1m"


def _plan_tone(util: float, status: str) -> str:
    """green→amber→red for a 0-1 utilisation, honouring an explicit reject/warning status."""
    s = (status or "").lower()
    if s in ("rejected", "blocked", "exceeded") or util >= 0.95:
        return "bad"
    if "warning" in s or util >= 0.8:
        return "warn"
    return "ok"


def _plan_label_for(limit_type: str) -> tuple[str, str]:
    """(stable key, brand label) for a rateLimitType; prettify unknown/future types."""
    key, label = _PLAN_LIMIT_LABELS.get(limit_type, ("", ""))
    if key:
        return key, label
    pretty = (limit_type or "limit").replace("_", " ").strip().capitalize()
    return (limit_type or "limit"), pretty


def _parse_plan_limits(infos: list[dict], now: float) -> list[dict]:
    """Turn raw ``rate_limit_info`` dicts into ordered, display-ready limit rows (de-duped by type)."""
    by_key: dict[str, dict] = {}
    for info in infos or []:
        if not isinstance(info, dict):
            continue
        ltype = str(info.get("rateLimitType") or "")
        try:
            util = max(0.0, float(info.get("utilization") or 0.0))
        except (TypeError, ValueError):
            continue
        key, label = _plan_label_for(ltype)
        resets_at = info.get("resetsAt")
        status = str(info.get("status") or "")
        by_key[key] = {                       # last event of a given type wins (the freshest reading)
            "key": key,
            "label": label,
            "type": ltype,
            "utilization": util,
            "pct": int(round(util * 100)),    # may exceed 100 when in overage (bar clamps; number is honest)
            "resets_at": resets_at,
            "resets_in": _fmt_reset_in(resets_at, now),
            "status": status,
            "tone": _plan_tone(util, status),
            "overage": bool(info.get("isUsingOverage")),
        }
    ordered = [by_key[k] for k in _PLAN_LIMIT_ORDER if k in by_key]
    ordered += [v for k, v in by_key.items() if k not in _PLAN_LIMIT_ORDER]  # unknown types, first-seen
    return ordered


def _probe_plan_limits() -> list[dict]:
    """Fire ONE throwaway ``claude -p`` and return the raw ``rate_limit_info`` dicts it emits.

    Isolated so tests can stub it without spawning a real CLI. Uses the cheapest model and a neutral
    cwd (so it doesn't load the repo's CLAUDE.md / MCP context), bounded by a hard timeout."""
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("claude CLI not on PATH")
    proc = subprocess.run(
        [claude, "-p", ".", "--output-format", "stream-json", "--verbose", "--model", "haiku"],
        capture_output=True, text=True, timeout=_PLAN_PROBE_TIMEOUT,
        cwd=tempfile.gettempdir(),
    )
    infos: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line or "rate_limit" not in line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "rate_limit_event":
            info = ev.get("rate_limit_info")
            if isinstance(info, dict):
                infos.append(info)
    return infos


def plan_usage(cfg: Config | None = None, *, now: float | None = None, force: bool = False) -> dict:
    """The REAL Claude Max subscription ceiling — session / weekly / per-model — read live (EU-77).

    Probes Claude Code (see ``_probe_plan_limits``) and returns, on success::

        {"available": True,
         "limits": [ {key, label, type, utilization, pct, resets_at, resets_in, status, tone, overage}, … ],
         "probed_at": <epoch>}

    and on any failure / when the CLI exposes nothing::

        {"available": False, "reason": "<short>", "probed_at": <epoch>}

    Cached for 5 minutes (a failed read for 1 minute). Best-effort — never raises. Fire it ONLY from a
    user-driven ``/usage`` render so an idle cockpit costs nothing; the cockpit board reads the local
    ledger via ``windows()``/``budget_status()``, never this probe.
    """
    t = now if now is not None else time.time()
    cached = _plan_cache.get("data")
    if cached is not None and not force:
        ttl = _PLAN_TTL_OK if cached.get("available") else _PLAN_TTL_ERR
        if t - float(_plan_cache.get("ts", 0.0)) < ttl:
            return cached
    try:
        limits = _parse_plan_limits(_probe_plan_limits(), now=t)
        if limits:
            data = {"available": True, "limits": limits, "probed_at": t}
        else:
            data = {"available": False, "reason": "no subscription-limit data returned", "probed_at": t}
    except Exception as e:  # noqa: BLE001 — a probe failure must never break the /usage render
        data = {"available": False, "reason": str(e)[:140], "probed_at": t}
    _plan_cache["ts"] = t
    _plan_cache["data"] = data
    return data


# EU-118: cache plan-limit hit state (utilization >= 1.0) so autopilot doesn't spin on repeated checks.
_plan_limit_hit_cache: dict = {"hit": False, "ts": 0.0, "ttl": 60.0}  # cache for 1 minute


def plan_limit_hit(cfg: Config | None = None, *, now: float | None = None, force: bool = False) -> dict:
    """Check if ANY plan limit (session/weekly/per-model) has utilization >= 1.0 — EU-118.

    Returns::

        {"hit": True/False, "over_limits": [ {key, label, utilization, ...}, ... ], "checked_at": <epoch>}

    Cached for 1 minute (a failed check for 30 seconds). Best-effort — never raises. When a limit
    is hit, autopilot halts new tickets and records an audit event instead of spinning silently.
    """
    global _plan_limit_hit_cache
    t = now if now is not None else time.time()
    if not force and _plan_limit_hit_cache.get("hit", False):
        ttl = 60.0 if _plan_limit_hit_cache.get("hit") else 30.0
        if t - _plan_limit_hit_cache.get("ts", 0.0) < ttl:
            # Return cached result, but include the checked_at timestamp
            return {
                "hit": _plan_limit_hit_cache.get("hit", False),
                "over_limits": _plan_limit_hit_cache.get("over_limits", []),
                "checked_at": _plan_limit_hit_cache.get("ts", t)
            }

    over_limits = []
    try:
        usage_data = plan_usage(cfg, now=t, force=force)
        if usage_data.get("available"):
            for limit in usage_data.get("limits", []):
                util = float(limit.get("utilization", 0.0))
                if util >= 1.0:
                    over_limits.append(limit)
    except Exception:  # noqa: BLE001 — plan-limit detection must never break the loop
        pass

    hit = len(over_limits) > 0
    _plan_limit_hit_cache = {
        "hit": hit,
        "over_limits": over_limits,
        "ts": t,
        "ttl": 60.0
    }

    return {
        "hit": hit,
        "over_limits": over_limits,
        "checked_at": t
    }


def plan_limit_reset_cache() -> None:
    """Clear the plan-limit hit cache — e.g. after switching providers or at midnight."""
    global _plan_limit_hit_cache
    _plan_limit_hit_cache["hit"] = False
    _plan_limit_hit_cache["over_limits"] = []
    _plan_limit_hit_cache["ts"] = 0.0


def available_fallback_provider(cfg: Config, current_model: str) -> tuple[str, str] | None:
    """EU-108/118: Check if multi-provider fallback is configured and any provider has capacity.

    Returns (model, provider_id) of the first available fallback, or None if:
      - No fallback providers are configured
      - All fallback providers also have utilization >= 1.0
      - The current model is already in the fallback list (avoid self-switch)

    The autopilot calls this before halting on plan limits; if a fallback is available,
    it switches models instead of parking tickets.
    """
    if not cfg or not hasattr(cfg, "fallback_providers"):
        return None

    fallbacks = getattr(cfg, "fallback_providers", None)
    if not fallbacks or not isinstance(fallbacks, list):
        return None

    # Normalize the current model name to avoid switching to the same provider
    current_normalized = current_model.lower().replace("_", "-").replace(" ", "")

    for model, provider_id in fallbacks:
        # Skip if this is the same provider/model we're already using
        model_normalized = model.lower().replace("_", "-").replace(" ", "")
        if model_normalized == current_normalized or str(provider_id or "").lower() in current_normalized:
            continue

        # Check if this provider has available capacity
        # For now, we assume fallback providers don't have the same real-time limit info
        # as the primary Claude Max subscription, so we conservatively assume they're available
        # unless explicitly blocked. A future enhancement could probe each provider's limits.
        return (model, provider_id)

    return None


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


# ── EU-122: Dual-provider budget monitor ───────────────────────────────────────────────────────────
# Track BOTH Claude Max plan usage AND GLM (Z.ai) usage, stopping gracefully before either runs out.


def glm_budget_status(cfg: Config) -> dict:
    """GLM (Z.ai) budget status: read GLM usage from the ledger and calculate utilization.

    Returns::
        {"on": True/False, "used": <tokens>, "cap": <quota>, "pct": 0-1, "alert": bool, "bad": bool}

    GLM calls are tagged with "glm" in the ledger. If glm_quota_tokens is 0, monitoring is disabled.
    """
    quota = int(getattr(cfg, "glm_quota_tokens", 0) or 0)
    if quota <= 0:
        return {"on": False, "used": 0, "cap": 0, "pct": 0.0, "alert": False, "bad": False}

    # Read today's GLM usage from the ledger (tag prefix "glm")
    used = tokens_today_for_tag(cfg, "glm")
    pct = used / quota if quota else 0.0

    alert_pct = float(getattr(cfg, "budget_alert_pct", 0.8) or 0.8)
    bad_threshold = float(getattr(cfg, "budget_bad_threshold", 0.95) or 0.95)

    return {
        "on": True,
        "used": used,
        "cap": quota,
        "pct": pct,
        "alert": pct >= alert_pct,
        "bad": pct >= bad_threshold,
    }


def dual_provider_budget_status(cfg: Config) -> dict:
    """Dual-provider budget status: Claude plan usage + GLM usage together.

    Returns::
        {
            "claude": {"healthy": bool, "limits": [...], "available": bool, ...},
            "glm": {"on": bool, "used": int, "cap": int, "pct": float, "alert": bool, "bad": bool},
            "healthy": bool,  # True if neither provider is in bad state
            "bad_provider": str | None,  # "claude" | "glm" | None
        }

    This is the single source of truth for cockpit gauges and pre-flight checks.
    """
    claude_data = plan_usage(cfg)
    glm_data = glm_budget_status(cfg)

    # Determine Claude health: any limit with utilization >= budget_bad_threshold is bad
    claude_bad = False
    if claude_data.get("available"):
        bad_threshold = float(getattr(cfg, "budget_bad_threshold", 0.95) or 0.95)
        for limit in claude_data.get("limits", []):
            if float(limit.get("utilization", 0.0)) >= bad_threshold:
                claude_bad = True
                break

    healthy = not claude_bad and not glm_data.get("bad", False)

    bad_provider = None
    if claude_bad:
        bad_provider = "claude"
    elif glm_data.get("bad", False):
        bad_provider = "glm"

    return {
        "claude": {**claude_data, "healthy": not claude_bad},
        "glm": glm_data,
        "healthy": healthy,
        "bad_provider": bad_provider,
    }


def pre_flight_check(cfg: Config, ticket_estimate_pct: float = 0.08) -> dict:
    """Pre-flight check: should we skip starting a new ticket?

    Returns::
        {"should_skip": bool, "provider": str | None, "reason": str, "status": dict}

    The check evaluates the ACTIVE provider (the one we'd use for the next ticket).
    If that provider's remaining budget is below the ticket estimate plus a safety margin,
    we skip starting the ticket.

    Args:
        cfg: Config object
        ticket_estimate_pct: Estimated fraction of provider budget a typical ticket consumes (default 8%)
    """
    status = dual_provider_budget_status(cfg)
    should_skip = False
    provider = None
    reason = ""

    # For now, assume Claude is the primary active provider
    # In the future with EU-121 routing, we'd check the active provider from routing logic
    claude_available = status["claude"].get("available", False)
    claude_limits = status["claude"].get("limits", [])

    if claude_available and claude_limits:
        bad_threshold = float(getattr(cfg, "budget_bad_threshold", 0.95) or 0.95)
        for limit in claude_limits:
            util = float(limit.get("utilization", 0.0))
            if util >= bad_threshold - ticket_estimate_pct:
                should_skip = True
                provider = "claude"
                label = limit.get("label", limit.get("key", "limit"))
                pct_rem = max(0.0, 1.0 - util)
                reason = f"Claude {label} at {util:.1%} capacity (~{pct_rem:.1%} remaining)"
                break

    # Also check GLM if enabled
    if not should_skip and status["glm"].get("on", False):
        glm_pct = status["glm"].get("pct", 0.0)
        bad_threshold = float(getattr(cfg, "budget_bad_threshold", 0.95) or 0.95)
        if glm_pct >= bad_threshold - ticket_estimate_pct:
            should_skip = True
            provider = "glm"
            pct_rem = max(0.0, 1.0 - glm_pct)
            reason = f"GLM quota at {glm_pct:.1%} used (~{pct_rem:.1%} remaining)"

    return {
        "should_skip": should_skip,
        "provider": provider,
        "reason": reason,
        "status": status,
    }


def graceful_stop_check(cfg: Config) -> dict:
    """Mid-run check: should we stop/switch after finishing the current ticket?

    Returns::
        {"should_stop": bool, "critical_provider": str | None, "reason": str, "status": dict}

    Called mid-run to detect if a provider has crossed the low-watermark (budget_bad_threshold).
    If so, we finish the current ticket cleanly, then stop or switch providers.
    """
    status = dual_provider_budget_status(cfg)
    should_stop = False
    critical_provider = status.get("bad_provider")
    reason = ""

    if critical_provider:
        should_stop = True
        if critical_provider == "claude":
            claude_limits = status["claude"].get("limits", [])
            bad_limits = [l for l in claude_limits if float(l.get("utilization", 0.0)) >= float(getattr(cfg, "budget_bad_threshold", 0.95) or 0.95)]
            if bad_limits:
                limit = bad_limits[0]
                label = limit.get("label", limit.get("key", "limit"))
                util = float(limit.get("utilization", 0.0))
                reason = f"Claude {label} at {util:.1%} capacity"
        elif critical_provider == "glm":
            glm_pct = status["glm"].get("pct", 0.0)
            reason = f"GLM quota at {glm_pct:.1%} used"

    return {
        "should_stop": should_stop,
        "critical_provider": critical_provider,
        "reason": reason,
        "status": status,
    }


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
