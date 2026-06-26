"""PM-triage-on-exhaustion QA: parse_triage, the REQUEUED outcome (not parked), the dashboard 're-queued'
mapping (kept out of Needs-you), and the one-triage-per-ticket cap."""
import sys, types, tempfile, json
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

from orchestrator import pm, dashboard as D, loop, autopilot, filing
from orchestrator.contracts import Outcome
from orchestrator.config import Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --- parse_triage ---
r = pm.parse_triage("Revert the supabase bump in superadmin; land the a11y.\nTRIAGE: RESOLVE")
chk("parse: RESOLVE", r["action"] == "RESOLVE")
chk("parse: keeps the instruction, drops the verdict line", "supabase" in r["text"] and "TRIAGE:" not in r["text"])
chk("parse: ESCALATE", pm.parse_triage("BLOCKER: need the coordinator phone.\nTRIAGE: ESCALATE")["action"] == "ESCALATE")
chk("parse: unclear -> ESCALATE (ask)", pm.parse_triage("hmm, not sure")["action"] == "ESCALATE")
chk("parse: empty -> ESCALATE + placeholder", pm.parse_triage("")["action"] == "ESCALATE" and pm.parse_triage("")["text"])

# EU-42: an out-of-scope ===TICKETS=== block in the triage reply is kept OUT of the human-facing
# instruction `text`, but preserved in `raw` so loop._route_out_of_scope can route it to the backlog
# regardless of where the model places the block relative to the TRIAGE verdict line.
_blk = ("Land the in-scope fix; back out the stray edit.\nTRIAGE: RESOLVE\n"
        '===TICKETS===\n[{"title": "Fix unrelated crash", "type": "Bug", "severity": "HIGH", "body": "x.py"}]\n===END===')
_rt = pm.parse_triage(_blk)
chk("triage+block: action still parsed past a trailing TICKETS block", _rt["action"] == "RESOLVE")
chk("triage+block: instruction text drops the machine block",
    "===TICKETS===" not in _rt["text"] and "Fix unrelated crash" not in _rt["text"])
chk("triage+block: instruction text keeps the real instruction", "back out the stray edit" in _rt["text"])
chk("triage+block: raw preserves the block for backlog routing",
    "===TICKETS===" in _rt["raw"] and "Fix unrelated crash" in _rt["raw"])
chk("triage+block: filing.parse_tickets recovers the finding from raw", len(filing.parse_tickets(_rt["raw"])[0]) == 1)
chk("PM-triage system prompt carries the out-of-scope channel (producer side)",
    filing.TICKET_BLOCK_RULE in pm.PM_TRIAGE_SYSTEM)

# --- REQUEUED outcome exists + is NOT parked ---
chk("REQUEUED outcome value", Outcome.REQUEUED.value == "requeued")
chk("REQUEUED is NOT parked (autopilot re-runs it)", Outcome.REQUEUED not in autopilot.PARKED)
chk("ESCALATED is still parked (sanity)", Outcome.ESCALATED in autopilot.PARKED)

# --- dashboard: pm_triage -> 're-queued', kept OUT of Needs-you ---
chk("dashboard maps pm_triage -> re-queued", D._OUTCOME.get("pm_triage") == "re-queued")
chk("pm_triage is terminal (run resolves cleanly)", "pm_triage" in D._TERMINAL)
chk("re-queued is NOT a Needs-you outcome", "re-queued" not in D._NEEDS_YOU)

# a run that ends in pm_triage shows 're-queued', not a stale 'running' / needs-you row
tmp = Path(tempfile.mkdtemp())
audit = tmp / "audit.jsonl"
audit.write_text("\n".join(json.dumps(e) for e in [
    {"event": "ticket_start", "ticket_id": "AUTO-9", "app": "automatixy", "ts": "2026-06-21T14:00:00"},
    {"event": "build", "ticket_id": "AUTO-9", "iteration": 4, "ok": True, "ts": "2026-06-21T14:05:00"},
    {"event": "pm_triage", "ticket_id": "AUTO-9", "action": "RESOLVE", "ts": "2026-06-21T14:06:00"},
]) + "\n", encoding="utf-8")
runs = D.load_tasks(audit)
auto9 = next((r for r in runs if r["ticket_id"] == "AUTO-9"), None)
chk("load_tasks: pm_triage run -> outcome 're-queued'", auto9 and auto9["outcome"] == "re-queued", str(auto9 and auto9["outcome"]))

# --- the one-triage-per-ticket cap (audit-based) ---
cfg = Config(apps=[], audit_path=str(audit))
chk("already-triaged: True for a ticket with a pm_triage event", loop._already_pm_triaged(cfg, "AUTO-9"))
chk("already-triaged: False for an untriaged ticket", not loop._already_pm_triaged(cfg, "AUTO-99"))

print("\n============ PM-TRIAGE QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
