"""QA for EU-25: a read-only cockpit (GENERAL_COCKPIT_PROMOTE unset) must return a graceful
403 from POST /api/promote and POST /api/ship-main — never a 500 from a NameError on Response."""
import os, sys, tempfile, types

# The server imports pull in the agent SDK transitively; stub it (same trick as testurl_test.py).
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

# Read-only cockpit: deploy explicitly disabled.
os.environ.pop("GENERAL_COCKPIT_PROMOTE", None)

from orchestrator.server import create_app
from orchestrator.config import Config, AppConfig

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))

audit_path = os.path.join(tempfile.mkdtemp(), "audit.jsonl")
cfg = Config(apps=[AppConfig(name="automatixy", repo_path="/tmp/x",
                             base_branch="DEV", protected_branch="MAIN",
                             backlog_backend="none")],
             audit_path=audit_path)
client = create_app(cfg).test_client()

r1 = client.post("/api/promote")
check("/api/promote -> 403 (not 500)", r1.status_code == 403, f"got {r1.status_code}")
check("/api/promote disabled message", b"disabled" in r1.data.lower(), r1.data[:80])

r2 = client.post("/api/ship-main")
check("/api/ship-main -> 403 (not 500)", r2.status_code == 403, f"got {r2.status_code}")
check("/api/ship-main disabled message", b"disabled" in r2.data.lower(), r2.data[:80])

print("\n========== PROMOTE READ-ONLY QA (EU-25) ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
