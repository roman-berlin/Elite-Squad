"""Auto model selection — pick the cheapest model that fits the task.

Two design rules keep this safe:
  • **Never above the ceiling.** `auto` only ever picks a model ≤ the one Roman configured
    (`builder_model` / `reviewer_model`), so it can only SAVE tokens, never spend more than he chose.
  • **Never below the floor.** Code work (builder, reviewer) never drops under Sonnet — a too-weak
    model just fails review and burns MORE tokens on retries.

Then, within those bounds: a small ticket runs cheaper than a big one, and when the day's token budget
is tight the tier drops a notch to stretch what's left. Off by default (`auto_model: false`) — when off,
every officer uses exactly the model it always did.
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


def for_builder(cfg, ticket, effort: str) -> tuple[str, str]:
    """The builder's model. Opus is the better coder, so it stays Opus for ALL code — dropping to
    Sonnet only when the day's token budget is tight (to keep working, not to save on easy tickets).
    Floor = Sonnet (never Haiku for code)."""
    ceiling = getattr(cfg, "builder_model", OPUS)
    if not getattr(cfg, "auto_model", False):
        return ceiling, "fixed"
    return optimize(ceiling, budget_pct=_budget_pct(cfg), floor_tier=1, conserve_only=True)


def for_reviewer(cfg, diff: str = "") -> tuple[str, str]:
    """The reviewer's model — same policy as the builder: Opus for code, Sonnet only under budget
    pressure. Floor = Sonnet."""
    ceiling = getattr(cfg, "reviewer_model", OPUS)
    if not getattr(cfg, "auto_model", False):
        return ceiling, "fixed"
    return optimize(ceiling, budget_pct=_budget_pct(cfg), floor_tier=1, conserve_only=True)
