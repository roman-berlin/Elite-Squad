"""Outcome ↔ audit-event single-source QA (EU-51).

Run outcomes used to live in two disjoint vocabularies: the `Outcome` enum on the programmatic
TicketReport path, and a parallel set of hand-typed audit-event strings ("merged"/"pr_opened"/…)
that the dashboard/forensics reconstruct outcomes from. Drift between them silently mis-classifies a
run. This harness locks them together: the enum is the single source (`Outcome.audit_event` +
`AUDIT_EVENT_OUTCOME`), and every canonical event must be one the dashboard already recognises."""
import sys, types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import contracts, dashboard as D, forensics as F
from orchestrator.contracts import Outcome, AUDIT_EVENT_OUTCOME

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --- every Outcome resolves to a canonical, non-empty audit event ---
chk("every Outcome has a non-empty audit_event",
    all(isinstance(o.audit_event, str) and o.audit_event for o in Outcome),
    str({o.name: getattr(o, "audit_event", None) for o in Outcome}))

# --- the canonical names are exactly the strings the loop records (locks the values) ---
EXPECTED = {Outcome.MERGED: "merged", Outcome.PR_OPENED: "pr_opened", Outcome.ESCALATED: "needs_human",
            Outcome.ERRORED: "ticket_exception", Outcome.SKIPPED: "dryrun_land", Outcome.REQUEUED: "pm_triage"}
chk("canonical audit_event matches the loop's recorded event names",
    all(o.audit_event == ev for o, ev in EXPECTED.items()),
    str({o.name: o.audit_event for o in Outcome}))

# --- the reverse map round-trips every canonical event back to its Outcome ---
chk("AUDIT_EVENT_OUTCOME round-trips every Outcome",
    all(AUDIT_EVENT_OUTCOME.get(o.audit_event) is o for o in Outcome),
    str(AUDIT_EVENT_OUTCOME))

# --- sub-cause aliases reconstruct to the right coarse Outcome ---
chk("alias 'no_changes' reconstructs to ERRORED", AUDIT_EVENT_OUTCOME.get("no_changes") is Outcome.ERRORED)
chk("alias 'escalated' reconstructs to ESCALATED", AUDIT_EVENT_OUTCOME.get("escalated") is Outcome.ESCALATED)

# --- every terminal event the loop actually records must be reconstructable (no orphan vocabulary) ---
LOOP_TERMINAL_EVENTS = {"merged", "pr_opened", "dryrun_land", "ticket_exception", "no_changes",
                        "pm_triage", "needs_human"}
missing = LOOP_TERMINAL_EVENTS - set(AUDIT_EVENT_OUTCOME)
chk("every loop-recorded terminal event is in AUDIT_EVENT_OUTCOME", not missing, f"missing={missing}")

# --- the two vocabularies AGREE: every reconstructable event is one the dashboard recognises ---
unknown_terminal = set(AUDIT_EVENT_OUTCOME) - D._TERMINAL
chk("every outcome event is in dashboard._TERMINAL", not unknown_terminal, f"not terminal: {unknown_terminal}")
unknown_outcome = set(AUDIT_EVENT_OUTCOME) - set(D._OUTCOME)
chk("every outcome event is mapped in dashboard._OUTCOME", not unknown_outcome, f"unmapped: {unknown_outcome}")

# --- forensics' enum-value bridge stays aligned with the Outcome enum ---
chk("forensics fail-values match the failing Outcome enum values",
    F._REPORT_FAIL_VALUES == {Outcome.ESCALATED.value, Outcome.ERRORED.value, Outcome.PR_OPENED.value},
    str(F._REPORT_FAIL_VALUES))

print("\n============ OUTCOME ↔ AUDIT-EVENT SINGLE-SOURCE QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
