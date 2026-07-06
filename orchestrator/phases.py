"""Single source of truth for the pipeline's phase bar (EU-55 / F12).

Both progress bars render from this one ordered list, so they can never drift apart
again: the terminal checklist in ``loop._bar`` and the War Room web ``phasebar``
(``warroom.active_run``). The order mirrors the real pipeline in ``loop.run``:

    Build → Gate → Review → Land

(Phase-2 §2, 2026-07-06: the ``Security`` phase — the per-diff security gate — was
removed; it was off by default and folded into the deterministic secret/dep scan plus a
Reviewer checklist section. The ``Tests`` phase — the separate Test Engineer coverage
pass — was removed: the Planner now hands the Builder testable acceptance criteria, the
Builder writes the tests itself, and the deterministic gate runs them.)
"""
from __future__ import annotations

# Ordered pipeline phases, rendered left → right in both bars. One definition only.
PHASES: tuple[str, ...] = ("Build", "Gate", "Review", "Land")

# Stable indices into PHASES so the bar call sites can read by name and never drift if
# the order is ever changed again.
BUILD, GATE, REVIEW, LAND = range(len(PHASES))
