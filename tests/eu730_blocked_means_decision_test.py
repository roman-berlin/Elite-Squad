"""EU-730 — "Blocked" must mean a decision only the Commander can make.

Commander 2026-07-26, looking at EU-697 (whose own comment reads "No files changed — empty diff …
All 4 ACs are already satisfied … 527/527 harnesses ALL GREEN") and then at a Blocked column of 10:

    "697 said the feature is already implemented no? so if it implemented and satisfied — why he
     needs me? just PM can close it … please fix the 'blocked' algorithm, there were not even one
     product decision in those 10."

He is right, and the ledger proves it. Of 128 needs_human events in the unit's entire history:
    92  red base — gate fails on the clean base tree   (infrastructure, self-clearing)
    29  max passes — PM escalated                      (reviewer disagreement)
     4  turn-limit                                     (too big -> split)
     1  per-ticket budget exceeded                     (too big -> split)
     1  product blocker — escalated to Commander       (<- the ONLY real decision)

The principle this pins: a build that WROTE NOTHING cannot break anything, so it belongs in QA —
the column the Commander sweeps — not in Blocked. Two paths were dumping there:

  (a) no-changes: the Builder produced an empty diff. Nothing was written, nothing merged. The only
      open question is "is it really already done?", which is exactly a QA check.
  (b) planner CLOSE/ANSWER: the Planner read the ticket and judged it not-work-to-build, with a
      reason. EU-375's safety properties are preserved verbatim — nothing is auto-closed (QA still
      means the Commander confirms) and no build is burned — only the column changes. REFILE still
      parks, because rewriting a ticket IS his call.
"""
from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, ".")

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


LOOP = pathlib.Path("orchestrator/loop.py").read_text(encoding="utf-8")

# ── (a) the no-changes path routes to QA ─────────────────────────────────────
_nc = LOOP[LOOP.find("# EU-116: a no-changes build leaves the ticket stuck In Progress"):]
_nc = _nc[:_nc.find("builder done —")] if "builder done —" in _nc else _nc[:4000]
chk("(1) a no-changes build lands in QA",
    'backlog.set_status(ticket, "QA")' in _nc, _nc[:200])
chk("(1a) …and no longer parks it in the human-handoff column",
    'backlog.set_status(ticket, "Needs Human")' not in _nc)
chk("(1b) …telling the Commander plainly that nothing was built",
    "Nothing was built, so nothing can break" in _nc)
# NB: assert on a phrase that is contiguous IN THE SOURCE — "back to To Do" is split across two
# adjacent string literals, so grepping for it fails even though the rendered comment contains it.
chk("(1c) …and how to reverse it if the unit is wrong",
    "To Do with a note" in _nc)
chk("(1d) …and it flags when the claim was NOT independently verified",
    "NOT independently verified" in _nc)

# ── (b) the planner CLOSE/ANSWER verdict routes to QA; REFILE still parks ────
_pk = LOOP[LOOP.find("audit.record(\"planner_verdict_park\""):]
_pk = _pk[:3000]
chk("(2) a Planner CLOSE/ANSWER lands in QA",
    'if _pres.verdict in ("CLOSE", "ANSWER"):' in _pk and 'set_status(ticket, "QA")' in _pk)
chk("(2a) …REFILE is NOT routed to QA (rewriting a ticket is the Commander's call)",
    '"REFILE"' not in _pk.split('if _pres.verdict in ("CLOSE", "ANSWER"):')[1][:400],
    "REFILE leaked into the QA branch")
chk("(2b) …the Planner's own reason travels with it",
    "_pres.answer or _pres.approach" in _pk)
chk("(2c) …and it still states nothing was closed automatically",
    "nothing was closed" in _pk.lower())

# EU-375's safety properties must survive untouched
chk("(3) EU-375 preserved: the park is still recorded (park-once loop guard intact)",
    'audit.record("planner_verdict_park"' in LOOP)
chk("(3a) EU-375 preserved: a non-BUILD verdict still does NOT run a build",
    "Planner verdict {_pres.verdict} — parked for the Commander" in LOOP
    or 'notes=f"Planner verdict {_pres.verdict} — parked for the Commander"' in LOOP)
chk("(3b) EU-375 preserved: nothing is auto-CLOSED by this path",
    'set_status(ticket, "Done")' not in _pk)

# ── the principle, stated where the next reader will find it ────────────────
chk("(4) the reasoning is recorded in the code, not just in a commit message",
    "EU-730" in LOOP and "Blocked is reserved for a genuine ask" in LOOP)

# ── guard against regression: no NEW no-changes/planner park to Needs Human ──
_parks = re.findall(r'set_status\(ticket, "Needs Human"\)', LOOP)
chk("(5) the remaining 'Needs Human' sites are the genuine parks only (<= 4)",
    len(_parks) <= 4, f"{len(_parks)} sites")

print("\n========== EU-730 BLOCKED MEANS A DECISION ==========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
