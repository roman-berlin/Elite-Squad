"""Squad mode — which working formation builds a ticket (2026-07-19 Commander order).

Two formations plus an automatic router:

  · 'full'  — the full squad: today's pipeline exactly as it is (Planner → Builder → Gate →
    Review → Land with the PM/Scrum Master ceremony). Best for streams of small,
    well-specified tickets where the ceremony is amortized.
  · 'elite' — the small elite squad: the same choke points but a tighter, more careful loop —
    the Analyst (Planner + a decompose-first addendum) lays out ordered steps and surfaces
    assumptions up front; the Builder (+ the iterative methodology layer) executes ONE
    step at a time, runs the repo's own tests after every step and reports each iteration; the
    Checker (the unchanged deterministic gate + one independent review) verifies. Bigger turn
    budget (effort floor 'xhigh') because one careful pass replaces retry churn.
  · 'auto'  — route per ticket by the existing sizer: L/XL → elite, S/M → full.

Deliberately NOT per-app (the per-app model override silently forced whole runs onto GLM for
weeks — one global knob, visible in the cockpit, is the honest surface). Stored in its own
state file so a squad choice can never corrupt the model-backend store.
"""
from __future__ import annotations

from pathlib import Path

from . import locking

_MODES = ("full", "elite", "auto")


def _file(cfg=None) -> Path:
    audit = getattr(cfg, "audit_path", None) if cfg is not None else None
    return Path(audit or "./state/audit.jsonl").with_name("squad_mode.json")


def get_mode(cfg=None) -> str:
    """The persisted squad mode — 'full' (default) | 'elite' | 'auto'."""
    import json
    try:
        data = json.loads(_file(cfg).read_text(encoding="utf-8"))
        m = str(data.get("mode") or "").strip().lower()
        return m if m in _MODES else "full"
    except (OSError, ValueError):
        return "full"


def set_mode(mode: str, cfg=None) -> None:
    """Persist the squad mode. Best-effort; ignores unknown values."""
    m = str(mode or "").strip().lower()
    if m not in _MODES:
        return

    def _mutate(current: dict) -> dict:
        data = dict(current) if isinstance(current, dict) else {}
        data["mode"] = m
        return data

    try:
        locking.locked_rmw(_file(cfg), _mutate, default={}, corrupt_to_default=True)
    except (OSError, ValueError):
        pass


def resolve_for_ticket(cfg, ticket) -> tuple[str, str]:
    """('full'|'elite', human-readable reason) for THIS ticket. 'auto' consults the existing
    ticket sizer (builder.size_ticket): L/XL → elite, S/M → full. Pure read — never writes."""
    mode = get_mode(cfg)
    if mode in ("full", "elite"):
        return mode, f"squad mode pinned to {mode}"
    try:
        from . import builder
        size, _eff, why = builder.size_ticket(ticket)
    except Exception:  # noqa: BLE001 - a sizer hiccup must never block a build; full is the default
        return "full", "auto — sizer unavailable, defaulting to full"
    if size in ("L", "XL"):
        return "elite", f"auto — sized {size} ({why}); big/risky work gets the careful loop"
    return "full", f"auto — sized {size} ({why}); small work takes the standard pipeline"
