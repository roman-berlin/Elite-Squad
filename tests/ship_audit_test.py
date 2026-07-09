"""EU-204: Verify that hazardous audit helpers _audit_ship and _audit_promote no longer exist."""
import sys, types
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import server

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --- EU-204: these functions were removed ---
chk("_audit_ship function removed", not hasattr(server, "_audit_ship"), "still exists: server._audit_ship")
chk("_audit_promote function removed", not hasattr(server, "_audit_promote"), "still exists: server._audit_promote")
chk("promote_api endpoint removed", not hasattr(server, "promote_api"), "still exists: server.promote_api")
chk("ship_main_api endpoint removed", not hasattr(server, "ship_main_api"), "still exists: server.ship_main_api")

print("\n============ EU-204 AUDIT HELPER REMOVAL QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
