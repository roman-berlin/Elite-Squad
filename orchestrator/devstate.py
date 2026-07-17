"""STATE OF DEV — a deterministic, code-derived brief injected into every officer's preamble.

EU-378 closes the blank-brain class: officers re-derived (or worse, mis-assumed) facts that are
mechanically knowable — the memory audit of 2026-07-17 found memory.preamble() spending ~1,889
tok/turn on the living log while **0 of its 12 lessons were durable dev facts and 5 described
officers that do not exist** (an "Engineering Coach" owing officers/sre.md; a Test Engineer retired
by c276155 five days before the scribe wrote "deployment complete"). Root cause: the scribe's only
inputs are council transcripts and run outcomes — it can never see the code, so it is structurally
incapable of learning that a file was deleted. Live cost of the class, same week: a fresh agent
wrote a `python3 -c` test against the EU-187 terminal that bans interpreters (403'd, tested
nothing); a triage sketch ordered the deletion of the live Engineering Manager's roster row.

This module is the counterpart built the way roster.py builds ROSTER.md: **no LLM writes here** —
every line is derived from code at build time, so it cannot drift, and tests/eu378_devstate_test.py
re-derives each fact to keep it honest. The brief admits ONLY durable invariants (true of dev
regardless of which ticket is running); nothing per-ticket — Development_Status.md is history, not
invariant, and injecting history is how the living log rotted.

Rendered to memory/DEV_STATE.md (runtime, gitignored like UNIT.live.md); memory.preamble() splices
it at the HEAD of the block, because builder._trim_preamble truncates the TAIL on oversize — an
appended brief would be exactly what gets cut.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
DEVSTATE_PATH = _ROOT / "memory" / "DEV_STATE.md"

_HEADING = "## STATE OF DEV — code-derived; trust this over any remembered lesson"


def build_doc(cfg=None) -> str:
    """Derive the brief from code. Every line here must be re-derivable by the guard test —
    if a fact can't be read out of the code, it doesn't belong in the brief."""
    from . import phases, routing
    from .officers import OFFICER_NAMES
    from .roster import _OFFICER_ROWS

    lines: list[str] = [_HEADING, ""]

    # Pipeline — from phases.PHASES, the single source the loop executes.
    lines.append(f"- Pipeline: {' → '.join(phases.PHASES)}. There is no Tests phase — the "
                 "test_engineer role was retired (c276155): the Builder writes the tests, the "
                 "deterministic gate runs them.")

    # Routing dormancy — from the live flag, not from memory of a decision.
    if routing.is_routing_enabled():
        lines.append("- Routing: ROUTING_ENABLED is ON — tier dispatch is live.")
    else:
        lines.append("- Routing: ROUTING_ENABLED is OFF — the routing/multi-backend layer is "
                     "DORMANT (Commander's decision, 2026-07-07). Do not write code assuming "
                     "tier dispatch, and do not arm it.")

    # Cockpit terminal guardrails — from the server's own allowlist constants (EU-187).
    try:
        from .server import TERMINAL_ALLOWED_COMMANDS
        allowed = ", ".join(sorted(TERMINAL_ALLOWED_COMMANDS))
        lines.append(f"- Cockpit terminal (EU-187): shell=False over an argv allowlist ({allowed}). "
                     "Interpreters (python/node/bun/sh) are BANNED — `python3 -c` gets a 403; "
                     "shell metacharacters are rejected outright; path operands are confined to "
                     "the working directory.")
    except Exception:  # noqa: BLE001 — a server import failure just drops this line
        pass

    # Officer reality — live set from the roster rows (the same source ROSTER.md renders from);
    # retired = label-dictionary keys that no longer have a roster row (kept in OFFICER_NAMES so
    # historical audit records still render, per EU-260).
    live = [key for key, *_ in _OFFICER_ROWS]
    retired = sorted(set(OFFICER_NAMES) - set(live))
    lines.append(f"- Officers LIVE: {', '.join(live)}.")
    if retired:
        lines.append(f"- Officers RETIRED (label kept for historical audit rows only — do NOT "
                     f"resurrect, route to, or commission them): {', '.join(retired)}.")

    # Repo invariants — from the interpreter and the unit's own gate wiring.
    lines.append(f"- Runtime: Python {sys.version_info.major}.{sys.version_info.minor}; the unit's "
                 "own gate is `python3 tests/run_all.py`; land on dev, never main (the Commander "
                 "merges dev → main).")
    lines.append("- Builder writes are confined to the ticket's worktree (EU-188 guard hooks); "
                 "writes to .github/workflows/ are blocked.")

    return "\n".join(lines).strip() + "\n"


def refresh(cfg=None) -> Path:
    """Re-derive and write memory/DEV_STATE.md. Called wherever the code just changed or the
    daily ceremony runs (council refresh, the post-land site in loop). Best-effort by contract:
    callers wrap it — a stale brief is worse than no write failure being visible, so failures
    print rather than raise at call sites."""
    DEVSTATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEVSTATE_PATH.write_text(build_doc(cfg), encoding="utf-8")
    return DEVSTATE_PATH


def brief() -> str:
    """The rendered brief for preamble injection, '' when never built. Read-only and cheap —
    officers pay for this string every turn, so the ~600-token ceiling is enforced by the guard
    test, not by intention."""
    try:
        return DEVSTATE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
