"""2026-07-20 Commander order — "develop 2 different tickets so there will not be conflicts."
Conflict-aware co-scheduling + the memory floor that makes N=2 safe after the 2026-07-17 OOM revert.

Pins:
  (1) _ticket_footprint extracts the DIRECTORY paths a ticket's text cites (files stripped,
      lowered); a ticket citing no paths has an empty (unpredictable) footprint;
  (2) _tickets_conflict: same/nested dirs → conflict; disjoint dirs (calendar vs leads) →
      parallel OK; an app-root citation conflicts with everything inside that app;
      either side unpredictable → conflict (conservative — serialize);
  (3) the concurrent picker defers a conflicting ticket (audited once as conflict_deferred)
      and keeps a disjoint one eligible — source pins on the guard wiring + cleanup;
  (4) _host_free_gb returns a positive float on this host (never raises); the memory floor
      wiring stands extra slots down below concurrent_min_free_gb (source pins);
  (5) config: max_concurrent_builders=2 armed WITH concurrent_min_free_gb=6.0 (the two must
      travel together — N=2 without the floor is the 2026-07-17 OOM incident again).
"""
import sys
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        s.__dict__.update(k)

    def __call__(s, *a, **k):
        return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.loop as loop
from orchestrator.contracts import Ticket

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


def T(id_, summary="", desc="", acs=()):
    return Ticket(id=id_, key=id_, summary=summary, description=desc,
                  acceptance_criteria=list(acs), app="automatixy")


# (1) footprint extraction
cal = T("A-1", "Fix trip highlights",
        "Where: apps/zelmero-app/src/components/calendar/CalendarGrid.tsx and "
        "apps/zelmero-app/tests/e2e/calendar-flow.spec.ts")
fp = loop._ticket_footprint(cal)
chk("(1a) directories extracted, files stripped",
    "apps/zelmero-app/src/components/calendar" in fp and "apps/zelmero-app/tests/e2e" in fp, fp)
chk("(1b) no path cited → empty footprint",
    loop._ticket_footprint(T("A-2", "Improve error copy", "make messages friendlier")) == frozenset())

# (2) conflict rules
leads = T("A-3", "Fix leads table sort",
          "Where: apps/zelmero-app/src/components/leads/LeadsTable.tsx")
cal2 = T("A-4", "Trip range e2e",
         "Where: apps/zelmero-app/src/components/calendar/DayCell.tsx")
other_app = T("A-5", "CRM dashboard fix",
              "Where: apps/zeltivo-crm/src/pages/Dashboard.tsx")
approot = T("A-6", "Rework zelmero theming", "Touches apps/zelmero-app/src broadly")
nopath = T("A-7", "Small copy fix", "somewhere")
chk("(2a) same component dir → conflict", loop._tickets_conflict(cal, cal2))
chk("(2b) disjoint components (calendar vs leads) → parallel OK",
    not loop._tickets_conflict(cal, leads))
chk("(2c) different apps → parallel OK", not loop._tickets_conflict(cal, other_app))
chk("(2d) app-root citation contains the component → conflict",
    loop._tickets_conflict(approot, leads))
chk("(2e) unpredictable footprint → conservative conflict (same app)",
    loop._tickets_conflict(nopath, cal2) and loop._tickets_conflict(cal, nopath))
other_repo = Ticket(id="B-1", key="B-1", summary="other project work",
                    description="no paths here either", app="Elite-Unit")
chk("(2f) DIFFERENT apps never conflict — separate repos cannot collide (even with no paths)",
    not loop._tickets_conflict(nopath, other_repo))

# (3) picker wiring — the guard, its one-shot audit, and the in-flight cleanup
src = Path("orchestrator/loop.py").read_text(encoding="utf-8")
chk("(3a) the picker defers on _tickets_conflict against in-flight tickets",
    "_tickets_conflict(ticket, t)" in src and 'audit.record("conflict_deferred"' in src)
chk("(3b) in_flight_tickets is registered on pick and popped in the finally",
    "in_flight_tickets[ticket.id] = ticket" in src
    and "in_flight_tickets.pop(ticket.id, None)" in src)

# (4) memory floor
free = loop._host_free_gb()
chk("(4a) _host_free_gb measures without raising", isinstance(free, float) and free > 0, free)
chk("(4b) extra slots stand down below the floor (slot 0 exempt)",
    "slot > 0 and _mem_floor > 0 and _host_free_gb() < _mem_floor" in src
    and 'audit.record("memory_deferred"' in src)

# (5) config coupling
cfgtext = Path("config.yaml").read_text(encoding="utf-8")
chk("(5) N=2 is armed together with the 6GB memory floor",
    "max_concurrent_builders: 2" in cfgtext and "concurrent_min_free_gb: 6.0" in cfgtext)

print("\n========== CONFLICT SCHEDULER QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
