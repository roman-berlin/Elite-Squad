"""Auto model selection — pick the cheapest model that fits the task.

Two design rules keep this safe:
  • **Never above the ceiling.** `auto` only ever picks a model ≤ the one Roman configured
    (`builder_model` / `reviewer_model`), so it can only SAVE tokens, never spend more than he chose.
  • **Never below the floor.** Code work (builder, reviewer) never drops under Sonnet — a too-weak
    model just fails review and burns MORE tokens on retries.

Then, within those bounds: a small ticket runs cheaper than a big one, and when the day's token budget
is tight the tier drops a notch to stretch what's left. Off by default (`auto_model: false`) — when off,
every officer uses exactly the model it always did.

Sonnet-cap fallback (per call, no persistence):
  When a Sonnet call hits its weekly bucket while All-models still has headroom, the unit retries
  that ONE unit of work on Opus so it can finish, then goes straight back to cheap-first — it never
  pins a week of Opus (the EU-108 weekly pin was deleted in Phase-2 Task 2). The retry lives in
  agent.run_agent_with_fallback; an All-models cap surfaces as is_plan_limit → EU-82 pause.
"""
from __future__ import annotations

# canonical model strings, cheapest → dearest
HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-5"   # upgraded 2026-07-05 (Commander order) — was claude-sonnet-4-6
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
    tight budget. Floor = Sonnet."""
    ceiling = getattr(cfg, "reviewer_model", OPUS)
    if not getattr(cfg, "auto_model", False):
        return ceiling, "fixed"

    big = len(diff or "") >= 12000 or (diff or "").count("\n") >= 300   # large/complex diff → Opus
    return _escalating(ceiling, base_tier=2 if big else 1, iteration=iteration,
                       budget_pct=_budget_pct(cfg), floor_tier=1, why="large diff" if big else "small diff")
