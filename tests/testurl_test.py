"""QA for the merge -> QA 'where to test' URL resolution (builder TEST: line + app qa_url)."""
import sys, types
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))

from orchestrator.loop import _test_url
from orchestrator.config import AppConfig

def mk(url=None):
    return AppConfig(name="automatixy", repo_path="/tmp/x", base_branch="DEV",
                     protected_branch="MAIN", backlog_backend="none", qa_url=url)

BASE = "https://dev.example.com"
check("route + qa_url -> full URL",
      _test_url(mk(BASE), "did stuff.\nTEST: /leads") == "https://dev.example.com/leads",
      _test_url(mk(BASE), "TEST: /leads"))
check("full URL in TEST: wins",
      _test_url(mk(BASE), "TEST: https://staging.app/microsite/acme") == "https://staging.app/microsite/acme")
check("route without slash gets joined",
      _test_url(mk(BASE), "TEST: leads") == "https://dev.example.com/leads", _test_url(mk(BASE), "TEST: leads"))
check("trailing slash on base handled",
      _test_url(mk(BASE + "/"), "TEST: /leads") == "https://dev.example.com/leads")
check("no TEST line + qa_url -> base only", _test_url(mk(BASE), "no test line here") == BASE)
check("no TEST + no qa_url -> empty", _test_url(mk(None), "no test line") == "")
check("bare route, no qa_url -> route as-is", _test_url(mk(None), "TEST: /leads") == "/leads")
check("'(no UI ...)' passes through", _test_url(mk(BASE), "TEST: (no UI — GET /api/health)") == "(no UI — GET /api/health)")
check("case-insensitive + last-line", _test_url(mk(BASE), "x\ntest: /board\n") == "https://dev.example.com/board",
      _test_url(mk(BASE), "x\ntest: /board\n"))

print("\n================ TEST-URL QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
