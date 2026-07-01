"""Auto model selection — pick the cheapest model that fits the task.

Two design rules keep this safe:
  • **Never above the ceiling.** `auto` only ever picks a model ≤ the one Roman configured
    (`builder_model` / `reviewer_model`), so it can only SAVE tokens, never spend more than he chose.
  • **Never below the floor.** Code work (builder, reviewer) never drops under Sonnet — a too-weak
    model just fails review and burns MORE tokens on retries.

Then, within those bounds: a small ticket runs cheaper than a big one, and when the day's token budget
is tight the tier drops a notch to stretch what's left. Off by default (`auto_model: false`) — when off,
every officer uses exactly the model it always did.

EU-108 Sonnet-cap fallback:
  When the Sonnet weekly bucket exhausts while All-models still has headroom, the unit escalates
  to Opus (which only draws from All-models) and keeps building. The fallback stays active until
  the weekly reset (Fri 09:00 UTC). This is the inverse of auto_model's Opus→Sonnet cost-saving drop.
"""
from __future__ import annotations

# canonical model strings, cheapest → dearest
HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"
OPUS = "claude-opus-4-8"
LADDER = [HAIKU, SONNET, OPUS]

_TOP = len(LADDER) - 1


def tier_of(model: str) -> int:
    """0 (haiku) · 1 (sonnet) · 2 (opus). An unknown model is treated as top so an explicit
    override is never silently downgraded."""
    m = (model or "").lower()
    if "haiku" in m:
        return 0
    if "sonnet" in m:
        return 1
    if "opus" in m:
        return 2
    return _TOP


def model_at(tier: int) -> str:
    return LADDER[max(0, min(tier, _TOP))]


def _short(model: str) -> str:
    parts = (model or "").split("-")
    return parts[1] if len(parts) > 1 else (model or "?")


# task size → the tier it "wants"
_SIZE_TIER = {"XS": 0, "S": 1, "M": 1, "S/M": 1, "L": 2, "XL": 2}


def optimize(ceiling_model: str, *, size: str = "", effort: str = "", budget_pct: float = 0.0,
             floor_tier: int = 0, conserve_only: bool = False) -> tuple[str, str]:
    """Choose a model in [floor_tier .. tier_of(ceiling)].

    `conserve_only=True` (used for **code** — builder & reviewer): stay at the ceiling (Opus) for
    quality, and only drop a tier when the day's token budget is tight. Opus is the better coder, so
    code never trades quality for size — only to avoid blowing the budget and hard-stopping the unit.

    `conserve_only=False` (non-code officers): also size by the task — a trivial item can run cheaper.
    """
    ceiling = tier_of(ceiling_model)
    if conserve_only:
        want = ceiling
        reasons = [f"{_short(ceiling_model)} for code"]
    else:
        want = _SIZE_TIER.get((size or "").upper(), None)
        if want is None:   # no size signal — read complexity off the effort instead
            want = 2 if effort in ("high", "xhigh", "max") else (0 if effort == "low" else 1)
        reasons = [f"sized {size or effort or '?'}"]
    if budget_pct >= 1.0:
        want -= 2
        reasons.append("budget over → conserve")
    elif budget_pct >= 0.8:
        want -= 1
        reasons.append("budget tight → conserve")
    chosen = max(floor_tier, min(want, ceiling))
    return model_at(chosen), f"{_short(model_at(chosen))} ({'; '.join(reasons)})"


def _budget_pct(cfg) -> float:
    try:
        from . import usage
        return float(usage.budget_status(cfg).get("pct", 0.0) or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


# effort levels that warrant Opus from the *first* pass. Deliberately narrow: only an explicit
# top-end pin ('max'/'maximum'/'ultra') starts the Builder on Opus. Auto-sizing routinely emits
# 'high' for ordinary tickets, so 'high'/'xhigh' must NOT live here — that was the bug that pinned
# almost everything to Opus and made the ladder inert. Those efforts now start Sonnet-first and
# escalate to Opus only if a cheap pass is rejected.
_HEAVY_EFFORT = {"ultra", "max", "maximum"}


def _escalating(ceiling_model: str, *, base_tier: int, iteration: int, budget_pct: float,
                floor_tier: int, why: str) -> tuple[str, str]:
    """Cheap-first-with-escalation ladder for CODE (builder & reviewer) when auto_model is on.

    Economical AND effective: start at the tier the task size warrants, then climb one tier per retry
    — a rejected cheap pass re-runs on a stronger model, so a wrong cheap attempt is *corrected*, not
    repeated. Never exceeds the configured ceiling, never drops below the floor (Sonnet for code —
    a too-weak coder fails review and burns MORE on retries). A tight daily budget lowers the ceiling
    so the unit keeps shipping on a cheaper model instead of hard-pausing."""
    ceiling = tier_of(ceiling_model)
    drop = 2 if budget_pct >= 1.0 else (1 if budget_pct >= 0.8 else 0)
    eff_ceiling = max(floor_tier, ceiling - drop)
    want = base_tier + max(0, iteration - 1)          # escalate one tier per retry
    chosen = max(floor_tier, min(want, eff_ceiling))
    bits = [why]
    if chosen > base_tier and iteration > 1:
        bits.append(f"retry {iteration}→escalated")
    if drop:
        bits.append("budget tight→conserve")
    return model_at(chosen), f"{_short(model_at(chosen))} ({'; '.join(bits)})"


def for_builder(cfg, ticket, effort: str, iteration: int = 1) -> tuple[str, str]:
    """The Builder's model. Off: the configured ceiling, unchanged. On (economical): a routine
    ticket — including auto-sized 'high' effort — attempts Sonnet first and escalates to Opus only
    if that pass is rejected. Only an *explicit* top-end pin ('max'/'maximum'/'ultra' effort) starts
    on Opus from pass one. Floor = Sonnet (never Haiku for code). A tight budget pins it to Sonnet to
    keep shipping rather than hard-pausing."""
    ceiling = getattr(cfg, "builder_model", OPUS)
    if not getattr(cfg, "auto_model", False):
        return ceiling, "fixed"

    # EU-108: If Sonnet-cap fallback is active, use Opus (skip the cheap-first ladder)
    if sonnet_fallback_active(cfg):
        reset_str = fallback_reset_time_str()
        reason = f"Sonnet cap hit → Opus fallback (resets {reset_str})" if reset_str else "Sonnet cap hit → Opus fallback"
        return OPUS, f"Opus ({reason})"

    base = 2 if (effort or "").lower() in _HEAVY_EFFORT else 1   # explicit max pin→Opus, else Sonnet-first
    return _escalating(ceiling, base_tier=base, iteration=iteration,
                       budget_pct=_budget_pct(cfg), floor_tier=1, why=f"{effort or '?'} effort")


def for_soldier_build(cfg, *, effort: str, iteration: int = 1) -> tuple[str, str]:
    """The model for a soldier's code-build turn.

    Soldiers execute delegated build work (write/patch code) at the direction of an officer.
    They must use the same cheap-first escalation ladder as the Builder — start on Sonnet
    (pass 1) and climb one tier per retry — rather than ``for_officer``'s size-once approach.
    ``for_officer`` picks the *one* cheapest model that fits and never revisits that choice;
    that is correct for advisory officers whose output is accepted or discarded whole.  But a
    soldier build is exactly like a builder pass: cheap enough on the first attempt, then
    escalated to a stronger model when a cheap pass is rejected, so the unit neither wastes
    Opus tokens on trivial work nor gets stuck retrying the same cheap model forever.

    Rules (same as ``for_builder``):
    - Floor = Sonnet (Haiku is too weak for code; failing cheap costs more retries).
    - Ceiling = ``cfg.builder_model`` (never exceed what the Commander configured).
    - Pass 1: Sonnet, *unless* effort is 'max'/'maximum'/'ultra', which pins to Opus.
    - Each subsequent retry escalates one tier until the ceiling is reached.
    - A tight daily budget lowers the effective ceiling to stretch remaining quota.
    - EU-108: Sonnet-cap fallback overrides to Opus when active.
    """
    ceiling = getattr(cfg, "builder_model", OPUS)
    if not getattr(cfg, "auto_model", False):
        return ceiling, "fixed"

    # EU-108: If Sonnet-cap fallback is active, use Opus (skip the cheap-first ladder)
    if sonnet_fallback_active(cfg):
        reset_str = fallback_reset_time_str()
        reason = f"Sonnet cap hit → Opus fallback (resets {reset_str})" if reset_str else "Sonnet cap hit → Opus fallback"
        return OPUS, f"Opus ({reason})"

    base = 2 if (effort or "").lower() in _HEAVY_EFFORT else 1   # explicit max pin→Opus, else Sonnet-first
    return _escalating(ceiling, base_tier=base, iteration=iteration,
                       budget_pct=_budget_pct(cfg), floor_tier=1, why=f"{effort or '?'} effort")


def for_officer(cfg, *, size: str = "", effort: str = "", ceiling_model: str | None = None,
                ) -> tuple[str, str]:
    """A non-Builder officer's model (scout, council chair, drillmaster, PM, QM, security review…).

    Off: the configured ceiling, unchanged. On (economical): size the task down a tier when it's
    light and conserve harder when the day's budget is tight — but never drop below Sonnet (floor),
    and never exceed the configured ceiling. Unlike the Builder/Reviewer this does NOT escalate per
    retry (officer turns aren't a code review→retry loop), so there's no `iteration` knob — it just
    picks the cheapest model that fits, once.

    `ceiling_model` lets the caller pass that officer's own configured ceiling; it defaults to
    `reviewer_model`, the ceiling most non-Builder officers run under."""
    ceiling = ceiling_model or getattr(cfg, "reviewer_model", OPUS)
    if not getattr(cfg, "auto_model", False):
        return ceiling, "fixed"
    return optimize(ceiling, size=size, effort=effort, budget_pct=_budget_pct(cfg), floor_tier=1)


def for_reviewer(cfg, diff: str = "", iteration: int = 1) -> tuple[str, str]:
    """The Reviewer's model. Off: the configured ceiling. On: sized by the diff — a small diff is
    reviewed on Sonnet, a large/complex one on Opus — escalating on re-review and conserving under a
    tight budget. Floor = Sonnet. EU-108: Sonnet-cap fallback overrides to Opus when active."""
    ceiling = getattr(cfg, "reviewer_model", OPUS)
    if not getattr(cfg, "auto_model", False):
        return ceiling, "fixed"

    # EU-108: If Sonnet-cap fallback is active, use Opus (skip the cheap-first ladder)
    if sonnet_fallback_active(cfg):
        reset_str = fallback_reset_time_str()
        reason = f"Sonnet cap hit → Opus fallback (resets {reset_str})" if reset_str else "Sonnet cap hit → Opus fallback"
        return OPUS, f"Opus ({reason})"

    big = len(diff or "") >= 12000 or (diff or "").count("\n") >= 300   # large/complex diff → Opus
    return _escalating(ceiling, base_tier=2 if big else 1, iteration=iteration,
                       budget_pct=_budget_pct(cfg), floor_tier=1, why="large diff" if big else "small diff")


# ── EU-108: Sonnet-cap fallback state ───────────────────────────────────────────────────────────────
# When the Sonnet weekly bucket hits but All-models still has headroom, we escalate to Opus and
# keep building. This state tracks the fallback window (active until the weekly reset).

_sonnet_cap_fallback_active: bool = False
_fallback_until: float = 0.0    # epoch timestamp when fallback expires (weekly reset)
_fallback_notified: bool = False   # one-time Telegram notification flag


def sonnet_fallback_active(cfg) -> bool:
    """Check if Sonnet→Opus fallback is currently active.

    Returns True only when:
      1. Config flag `opus_fallback_on_sonnet_cap` is True
      2. Fallback was activated (Sonnet cap hit, Opus succeeded)
      3. Current time is before the weekly reset

    Auto-clears the fallback state after the reset time passes.
    """
    global _sonnet_cap_fallback_active, _fallback_until, _fallback_notified

    if not _sonnet_cap_fallback_active:
        return False

    # Check if the config knob is still enabled
    if not getattr(cfg, "opus_fallback_on_sonnet_cap", True):
        # Config disabled, clear the fallback
        _sonnet_cap_fallback_active = False
        _fallback_until = 0.0
        _fallback_notified = False
        return False

    # Check if we've passed the reset time
    import time
    now = time.time()
    if now >= _fallback_until:
        # Reset time passed, clear the fallback
        _sonnet_cap_fallback_active = False
        _fallback_until = 0.0
        _fallback_notified = False
        return False

    return True


def activate_sonnet_fallback(until_epoch: float) -> None:
    """Activate Sonnet→Opus fallback until the weekly reset.

    Args:
        until_epoch: Epoch timestamp when the fallback should expire (usually weekly reset).
    """
    global _sonnet_cap_fallback_active, _fallback_until

    _sonnet_cap_fallback_active = True
    _fallback_until = until_epoch


def reset_sonnet_fallback() -> None:
    """Clear the Sonnet fallback state (e.g. at midnight or after manual reset)."""
    global _sonnet_cap_fallback_active, _fallback_until, _fallback_notified

    _sonnet_cap_fallback_active = False
    _fallback_until = 0.0
    _fallback_notified = False


def _get_next_friday_0900_utc() -> float:
    """Calculate the epoch timestamp for the next Friday 09:00 UTC.

    Anthropic's weekly buckets reset on Friday 09:00 UTC. This helper computes the next reset.
    """
    import time
    from datetime import datetime, timedelta, timezone

    now = time.time()
    utc_now = datetime.fromtimestamp(now, tz=timezone.utc)

    # Find Friday (weekday=4 in Python)
    days_ahead = 4 - utc_now.weekday()
    if days_ahead <= 0:  # Today is Friday or later in the week
        days_ahead += 7  # Next Friday

    next_friday = utc_now + timedelta(days=days_ahead)

    # Set time to 09:00 UTC
    reset_time = next_friday.replace(hour=9, minute=0, second=0, microsecond=0)

    return reset_time.timestamp()


def fallback_reset_time_str() -> str:
    """Human-readable reset time for the fallback (e.g. 'Fri 09:00 UTC')."""
    import time
    from datetime import datetime, timezone

    ts = _fallback_until if _sonnet_cap_fallback_active else 0.0
    if ts <= 0:
        return ""

    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%a %H:%M UTC")
    except (ValueError, OSError):
        return ""


def sonnet_fallback_notification_sent() -> bool:
    """Check if we've already sent the fallback activation notification."""
    return _fallback_notified


def mark_sonnet_fallback_notified() -> None:
    """Mark that the fallback notification has been sent (one-time flag)."""
    global _fallback_notified
    _fallback_notified = True


def reset_sonnet_fallback_notification() -> None:
    """Clear the notification flag (e.g. at midnight or after weekly reset)."""
    global _fallback_notified
    _fallback_notified = False
