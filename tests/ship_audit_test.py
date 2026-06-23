"""Ship/promote audit QA: a cockpit ship (app DEV→MAIN) and a unit promote (dev→main) now leave an
audit trail, so 'check the logs' actually shows the ship — previously the only trace was the in-memory
banner. Logging is best-effort: an audit failure never breaks the ship."""
import sys, types
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

from orchestrator import server

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

class FakeAudit:
    def __init__(s): s.events = []
    def record(s, event, **kw): s.events.append((event, kw))
class BoomAudit:
    def record(s, *a, **k): raise RuntimeError("audit down")

# --- a successful app ship is recorded with the branches + commit count ---
a = FakeAudit()
server._audit_ship(a, "automatixy", {"ok": True, "base": "DEV", "prot": "MAIN", "ahead_before": 9})
ev, kw = a.events[-1]
chk("ship event recorded", ev == "ship")
chk("ship carries app/base/prot/ahead/ok",
    kw["app"] == "automatixy" and kw["base"] == "DEV" and kw["prot"] == "MAIN" and kw["ahead"] == 9 and kw["ok"] is True)

# --- a failed ship is recorded too (ok=False + the reason) — the point is visibility ---
a2 = FakeAudit()
server._audit_ship(a2, "automatixy", {"ok": False, "error": "non-ff push rejected"})
chk("failed ship records ok=False + error", a2.events[-1][1]["ok"] is False and "non-ff" in a2.events[-1][1]["error"])

# --- a unit promote (Update unit) is recorded as its own event ---
a3 = FakeAudit()
server._audit_promote(a3, {"ok": True, "ahead_before": 2})
ev3, kw3 = a3.events[-1]
chk("promote event recorded (unit)", ev3 == "promote" and kw3["target"] == "unit" and kw3["ahead"] == 2 and kw3["ok"] is True)

# --- missing ahead -> 0 (no crash on a sparse result dict) ---
a4 = FakeAudit()
server._audit_ship(a4, "x", {"ok": True})
chk("missing ahead defaults to 0", a4.events[-1][1]["ahead"] == 0)

# --- a logging failure is swallowed: the ship must never break on an audit hiccup ---
try:
    server._audit_ship(BoomAudit(), "x", {"ok": True})
    server._audit_promote(BoomAudit(), {"ok": True})
    chk("audit failure is swallowed (ship survives)", True)
except Exception:  # noqa: BLE001
    chk("audit failure is swallowed (ship survives)", False)

print("\n============ SHIP AUDIT QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
