"""EU-344 — the drain must pick To-Do tickets in board priority order, and never skip silently.

Roman 2026-07-15: "it takes tickets according to priority?" — measured live: no. The window WAS
priority-ordered (the Jira adapter honours ORDER BY priority DESC, Rank ASC) and intake did no
re-sort, but the observed pick sequence ran near-ascending-key while 6+ Highest tickets sat in To
Do. The ticket localised the deviation to the picker layer — which had no regression guard because
the tier split was inlined in autopilot()'s loop.

This pins the now-extracted pure picker `_assemble_worklist`:
  (1) To Do is preserved in the adapter's (priority) order — a Highest ticket ranked first in the
      window is picked before a Medium ranked later, with empty skip state (the core AC);
  (2) the cap bounds the To-Do slice AFTER the priority order, so a small cap takes the HIGHEST
      tickets, never an ascending-key prefix (the live symptom);
  (3) In Progress (tier-1) leads and is never capped; answered resumes (tier-2) sit between;
  (4) blocked tickets are excluded.
Plus a source pin that the base-hold pick-time drop now emits a `pick_skip` audit event (AC: every
pick-time exclusion is logged, no silent skips).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
sdk.__getattr__ = lambda n: (lambda *a, **k: None)
sys.modules.setdefault("claude_agent_sdk", sdk)
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers={}, update=lambda *a, **k: None)
sys.modules.setdefault("requests", req)

sys.path.insert(0, ".")

from orchestrator import autopilot  # noqa: E402

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


def T(tid, status=None):
    return types.SimpleNamespace(id=tid, status=status)


APP = types.SimpleNamespace(name="EU")


def ids(worklist):
    return [t.id for _, t in worklist]


# Board order as the adapter returns it: priority DESC, Rank ASC. HIGH-1 outranks MED-1 which the
# board places later; ascending-KEY order would (wrongly) put MED-1 first because 'MED' < 'HIGH'
# is irrelevant — what matters is the picker must NOT re-sort by key.
window = [
    (APP, T("EU-900")),  # Highest, ranked first by the board
    (APP, T("EU-100")),  # Medium, ranked later
    (APP, T("EU-500")),  # High, ranked between
]

# (1) empty skip state → adapter (priority) order is preserved; Highest picked first
wl = autopilot._assemble_worklist(window, blocked=set(), resumed={}, cap=10)
ok("(1) To Do preserved in board priority order (Highest first)",
   ids(wl) == ["EU-900", "EU-100", "EU-500"], f"got {ids(wl)}")
ok("(1b) the first pick is the Highest ticket, not the lowest key",
   ids(wl)[0] == "EU-900", f"got {ids(wl)[0]}")

# (2) a small cap takes the HIGHEST slice, never an ascending-key prefix
wl = autopilot._assemble_worklist(window, blocked=set(), resumed={}, cap=1)
ok("(2) cap=1 keeps the top-priority ticket (cap applied AFTER priority order)",
   ids(wl) == ["EU-900"], f"got {ids(wl)}")

# (3) In Progress leads and is uncapped; answered resumes sit between IP and To Do
mixed = [
    (APP, T("EU-700", status="In Progress")),
    (APP, T("EU-900")),   # Highest To Do
    (APP, T("EU-100")),   # Medium To Do
]
resumed = {"EU-050": (APP, T("EU-050"))}  # answered on Jira, not in the drain window
wl = autopilot._assemble_worklist(mixed, blocked=set(), resumed=resumed, cap=1)
ok("(3) order is In Progress → answered → To Do(capped)",
   ids(wl) == ["EU-700", "EU-050", "EU-900"], f"got {ids(wl)}")
ok("(3b) In Progress is not bounded by the To-Do cap",
   "EU-700" in ids(wl), f"got {ids(wl)}")

# (4) blocked tickets are excluded entirely
wl = autopilot._assemble_worklist(window, blocked={"EU-900"}, resumed={}, cap=10)
ok("(4) blocked ticket excluded", "EU-900" not in ids(wl) and ids(wl) == ["EU-100", "EU-500"],
   f"got {ids(wl)}")

# Source pin: the base-hold pick-time drop now emits a pick_skip audit event (no silent skips).
src = Path("orchestrator/autopilot.py").read_text()
ok("(5) base-hold drop emits a pick_skip audit event with tickets + reason",
   'audit.record("pick_skip"' in src and "base-hold" in src,
   "the picker layer still drops held tickets silently")

print(f"\n{checks}/{checks} passed")
