"""Backlog drain-error visibility QA: from_drain records a per-app fetch failure in LAST_DRAIN_ERRORS
(instead of only printing it), clears it on a clean fetch, and the cockpit backlog panel surfaces an
unreachable board as a loud warning — so a misconfigured/unreadable Jira can never again masquerade as
'queue clear · nothing of yours'. A good app still drains when a sibling fails."""
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

from orchestrator import intake, warroom

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

class OkBL:
    def get_ready_tasks(s, limit):
        return [types.SimpleNamespace(id="AUTO-9", key="AUTO-9", summary="Lead webhook")]
class BoomBL:
    def get_ready_tasks(s, limit):
        raise RuntimeError("403 Forbidden: EU project not browsable by this token")

appEU = types.SimpleNamespace(name="Elite-Unit", backlog_backend="jira")
appAUTO = types.SimpleNamespace(name="automatixy", backlog_backend="jira")
cfg = types.SimpleNamespace(apps=[appEU, appAUTO])

# --- one board fails, the other still drains; the failure is RECORDED, not just printed ---
intake.make_backlog = lambda app: BoomBL() if app.name == "Elite-Unit" else OkBL()
intake.LAST_DRAIN_ERRORS.clear()
items = intake.from_drain(cfg, None, 10)
chk("the healthy board still drained", any(t.id == "AUTO-9" for _, t in items))
chk("the failed board is recorded in LAST_DRAIN_ERRORS", "Elite-Unit" in intake.LAST_DRAIN_ERRORS and "403" in intake.LAST_DRAIN_ERRORS["Elite-Unit"])
chk("the healthy board is NOT flagged as an error", "automatixy" not in intake.LAST_DRAIN_ERRORS)

# --- a clean fetch clears the prior error (no stale warning) ---
intake.make_backlog = lambda app: OkBL()
intake.from_drain(cfg, None, 10)
chk("a clean fetch clears the prior error", "Elite-Unit" not in intake.LAST_DRAIN_ERRORS)

# --- the cockpit panel surfaces an unreachable board even when the result is empty ---
intake.LAST_DRAIN_ERRORS.clear()
intake.LAST_DRAIN_ERRORS["Elite-Unit"] = "403 Forbidden: EU not browsable"
warroom._backlog_items = lambda cfg, app: ([], None)   # isolate the warning rendering
html_all = warroom._backlog_html(cfg, "*")
chk("all-projects panel shows the unreachable-board warning", "Elite-Unit backlog unreachable" in html_all and "403" in html_all)
chk("warning replaces the misleading silent 'nothing open'", "unreachable" in html_all)
html_auto = warroom._backlog_html(cfg, "automatixy")
chk("a scoped panel hides other apps' errors", "Elite-Unit backlog unreachable" not in html_auto)

print("\n============ DRAIN-ERROR VISIBILITY QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
