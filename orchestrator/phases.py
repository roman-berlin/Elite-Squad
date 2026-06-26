"""Single source of truth for the pipeline's phase bar (EU-55 / F12).

Both progress bars render from this one ordered list, so they can never drift apart
again: the terminal checklist in ``loop._bar`` and the War Room web ``phasebar``
(``warroom.active_run``). The order mirrors the real pipeline in ``loop.run``:

    Build → Gate → Tests → Review → Security → Land

The ``Tests`` phase is the Test Engineer's coverage gate, which runs after the Gate
and before the Review; ``Security`` is the Security Engineer's diff gate, which runs
after a ship-ready review and just before the Land. Both phases were missing from the
two old hardcoded bars, which had also drifted out of sync with each other (the
terminal bar listed 4 phases, the web bar listed 5).
"""
from __future__ import annotations

# Ordered pipeline phases, rendered left → right in both bars. One definition only.
PHASES: tuple[str, ...] = ("Build", "Gate", "Tests", "Review", "Security", "Land")

# Stable indices into PHASES so the bar call sites can read by name and never drift if
# the order is ever changed again.
BUILD, GATE, TESTS, REVIEW, SECURITY, LAND = range(len(PHASES))
