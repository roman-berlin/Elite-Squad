"""Per-cycle memory learning QA: after a productive autopilot cycle, _learn_from_cycle runs the FREE
deterministic consolidate pass (fold recurring rejection-lessons + prune) and records a 'memory_learn'
audit event when it actually learned something — so memory compounds every cycle, not only at the
06:30 council. It is a no-op on an empty cycle and never propagates an exception (memory hygiene must
not break the worker)."""
import sys, types
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import autopilot, consolidate

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

class FakeAudit:
    def __init__(s): s.events = []
    def record(s, event, **kw): s.events.append((event, kw))

cfg = types.SimpleNamespace(audit_path="/tmp/learn_a.jsonl")
reports = [types.SimpleNamespace(ticket_id="AUTO-1"), types.SimpleNamespace(ticket_id="AUTO-2")]

# --- learned something -> returns the report + records a memory_learn event ---
consolidate.run = lambda c, **k: {"added": ["a", "b"], "pruned": 1, "kept": 10}
au = FakeAudit()
r = autopilot._learn_from_cycle(cfg, reports, au)
chk("returns the consolidate report", r.get("added") == ["a", "b"])
chk("records a memory_learn audit event", any(e == "memory_learn" for e, _ in au.events))
chk("audit event carries the count", any(kw.get("added") == 2 for e, kw in au.events if e == "memory_learn"))

# --- nothing new this cycle -> returns the report, but NO audit spam ---
consolidate.run = lambda c, **k: {"added": [], "pruned": 0, "kept": 10}
au2 = FakeAudit()
r2 = autopilot._learn_from_cycle(cfg, reports, au2)
chk("no new lessons -> no memory_learn event", not any(e == "memory_learn" for e, _ in au2.events))
chk("still returns the (empty) report", r2.get("added") == [])

# --- empty cycle (no reports) -> no-op, consolidate not even called ---
called = {"n": 0}
def _spy(c, **k):
    called["n"] += 1
    return {"added": []}
consolidate.run = _spy
au3 = FakeAudit()
r3 = autopilot._learn_from_cycle(cfg, [], au3)
chk("empty cycle -> {} and consolidate not called", r3 == {} and called["n"] == 0)

# --- consolidate blows up -> swallowed, returned as error, worker keeps running ---
def _boom(c, **k):
    raise RuntimeError("audit unreadable")
consolidate.run = _boom
au4 = FakeAudit()
r4 = autopilot._learn_from_cycle(cfg, reports, au4)
chk("consolidate error is swallowed (no raise)", isinstance(r4, dict) and "error" in r4)
chk("error not recorded as a learn event", not any(e == "memory_learn" for e, _ in au4.events))

print("\n============ AUTOPILOT PER-CYCLE LEARN QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
