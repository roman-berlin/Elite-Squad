"""Ticket-readiness gate — hand back an under-specified ticket BEFORE the Builder guesses.

A ticket with no acceptance criteria AND only a thin description is the #1 cause of a build that halts
mid-way, or guesses the scope and gets rejected in review — wasted Opus. This cheap, **deterministic**
check (no model call) flags those up front, so the unit hands them back with a note on what's missing
instead of building blind. Anything with acceptance criteria, OR a description of real substance, sails
through untouched. Opt-in (`readiness_gate`).
"""
from __future__ import annotations


def assess(ticket, *, min_desc_chars: int = 80) -> tuple[bool, list[str]]:
    """Is this ticket ready to build? Returns ``(ready, missing)`` — ``missing`` lists what to add when
    it isn't. Ready = it has acceptance criteria, OR a description of real substance (longer than
    ``min_desc_chars`` and not just the title restated)."""
    ac = [str(c).strip() for c in (getattr(ticket, "acceptance_criteria", None) or []) if str(c).strip()]
    desc = (getattr(ticket, "description", "") or "").strip()
    summary = (getattr(ticket, "summary", "") or "").strip()
    # a description that merely restates the title carries no real spec
    meaningful_desc = bool(desc) and desc.lower() != summary.lower() and len(desc) >= int(min_desc_chars)
    if ac or meaningful_desc:
        return True, []
    missing: list[str] = []
    if not ac:
        missing.append("no acceptance criteria — add a short checklist of what “done” looks like")
    if not meaningful_desc:
        missing.append(f"thin description ({len(desc)} chars) — add the goal, context and constraints")
    return False, missing
