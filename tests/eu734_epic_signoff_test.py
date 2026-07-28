"""EU-734 — a finished Epic goes to the Commander for sign-off, and NO Epic can become a zombie.

Commander 2026-07-28, choosing between the two designs I put to him: "ok do it - no zombies."

DESIGN 1 (his acceptance model). The Epic is the human's acceptance unit for a whole feature, so a
finished Epic is routed to QA rather than closing itself — Jira must never assert an acceptance no
human performed. The hand-off lists every child with its status and, crucially, flags the
EXCEPTIONS (children sitting in QA rather than Done). That is the point of reviewing the parent:
one look says what is finished and what still wants his eyes.

DESIGN 2 (the "no zombies" guarantee). The roll-up is EVENT-driven — it fires when a child
completes — so it can only ever help an Epic whose last child finishes after the roll-up exists.
An Epic that finished earlier, or whose hand-off lost a race with a board hiccup, has nothing left
to trigger it: 16 were found in exactly that state on 2026-07-27, one of them 5 children complete.
`sweep_ready_epics` re-derives readiness from the children at an idle boundary, so the guarantee
does not depend on any single event ever firing.

Both halves are deliberately conservative: an Epic with no children is hand-made and never touched;
an unreadable child leaves the Epic open; any error skips that Epic rather than the sweep.
"""
from __future__ import annotations

import pathlib
import sys
import types

_sdk = types.ModuleType("claude_agent_sdk")
_sdk.__getattr__ = lambda _n: (lambda *a, **k: None)
sys.modules.setdefault("claude_agent_sdk", _sdk)
_req = types.ModuleType("requests")
_req.Session = lambda *a, **k: None
_req.RequestException = Exception
sys.modules.setdefault("requests", _req)

sys.path.insert(0, ".")

from orchestrator import autopilot  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# ── DESIGN 1: the land-path roll-up hands off instead of closing ─────────────
LOOP = pathlib.Path("orchestrator/loop.py").read_text(encoding="utf-8")
_fn = LOOP[LOOP.find("def _maybe_close_epic"):]
_fn = _fn[:_fn.find("\ndef _already_landed")]

chk("(1) a finished Epic is routed to QA, not closed",
    'backlog.set_status(_epic_ticket, "QA")' in _fn)
chk("(1a) …it no longer auto-closes (Jira never asserts an acceptance nobody performed)",
    "ok = close(epic_key" not in _fn)
chk("(2) the hand-off lists every child with its status",
    "_rows.append(" in _fn and "c.get('summary')" in _fn)
chk("(2a) …and flags the EXCEPTIONS — children not Done — as the thing to look at",
    "_exceptions" in _fn and "Still wanting your eyes" in _fn)
chk("(3) a failed hand-off leaves the Epic OPEN (safe direction, retried by the sweep)",
    "hand-off failed" in _fn and "left open" in _fn)
chk("(3a) …and only marks it rolled-up on SUCCESS, so a failure can be retried",
    _fn.find("_EPIC_ROLLED_UP.add(epic_key)") > _fn.find("if ok:") > 0)
chk("(4) the audit says ready-for-signoff, not auto-closed",
    'audit.record("epic_ready_for_signoff"' in _fn)

# ── DESIGN 2: the sweep — behavioural, against a fake backlog ────────────────
class _BL:
    def __init__(s, epics, kids):
        s._e, s._k = epics, kids
        s.moved, s.comments = [], []
    def open_epics(s): return list(s._e)
    def epic_children(s, k): return s._k.get(k, [])
    def get_task(s, k): return types.SimpleNamespace(key=k, id=k)
    def set_status(s, t, st): s.moved.append((t.key, st))
    def add_comment(s, t, b): s.comments.append((t.key, b))


D = {"status": "Done"}
def C(k, st="Done", summ="x"): return {"key": k, "status": st, "summary": summ}

bl = _BL(["E-1", "E-2", "E-3", "E-4"], {
    "E-1": [C("a"), C("b")],                       # all Done      -> hand off
    "E-2": [C("c"), C("d", "In Progress")],        # one open      -> leave
    "E-3": [],                                      # no children   -> never touch
    "E-4": [C("e"), C("f", "QA")],                 # done-ish + QA -> hand off, flag exception
})
moved = autopilot.sweep_ready_epics(types.SimpleNamespace(apps=[]), {"EU": bl}, None)

chk("(5) an Epic whose children are ALL finished is handed off", "E-1" in moved, str(moved))
chk("(5a) …an Epic with an open child is left alone", "E-2" not in moved)
chk("(5b) …an Epic with NO children is never touched (hand-made, not a container)",
    "E-3" not in moved)
chk("(5c) …a QA child still counts as finished but is FLAGGED as an exception",
    "E-4" in moved
    and any("f (QA)" in b for k, b in bl.comments if k == "E-4"),
    str([b[:80] for k, b in bl.comments if k == "E-4"]))
chk("(6) the hand-off transitions to QA (never Done)",
    all(st == "QA" for _, st in bl.moved) and bl.moved, str(bl.moved))
chk("(6a) …and every handed-off Epic gets its child list on the ticket",
    all(any(k == e for k, _ in bl.comments) for e in moved))
chk("(7) a clean Epic says so rather than implying a problem",
    any("nothing flagged" in b for k, b in bl.comments if k == "E-1"))

# a backend without Epic support, and a raising one, must both be no-ops rather than crashes
class _NoEpics:
    pass
class _Boom:
    def open_epics(s): raise RuntimeError("board down")
    def epic_children(s, k): return []
chk("(8) a backend without Epic helpers is skipped, not crashed",
    autopilot.sweep_ready_epics(types.SimpleNamespace(apps=[]), {"x": _NoEpics()}, None) == [])
chk("(8a) an unreachable board skips that app instead of aborting the sweep",
    autopilot.sweep_ready_epics(types.SimpleNamespace(apps=[]), {"x": _Boom()}, None) == [])

# ── the sweep must actually be wired at the idle boundary ───────────────────
AUTO = pathlib.Path("orchestrator/autopilot.py").read_text(encoding="utf-8")
chk("(9) the sweep runs at the idle boundary beside the branch retirement",
    "sweep_ready_epics(cfg, _retire_backlogs, audit)" in AUTO)
chk("(9a) …and the adapter can list open Epics for it",
    "def open_epics(self)" in pathlib.Path("orchestrator/backlog/jira.py").read_text(encoding="utf-8"))

print("\n========== EU-734 EPIC SIGN-OFF + NO ZOMBIES ==========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
