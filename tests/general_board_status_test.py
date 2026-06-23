"""The General sees ALL projects QA: when the Commander asks about status/tickets/boards/'all projects',
the General chat is grounded with a LIVE snapshot of EVERY configured Jira board (not just AUTO) — so it
can never again claim "we only track AUTO". A greeting does not trigger a Jira fetch. One unreachable
board degrades to a note instead of blanking the others."""
import sys, types, asyncio, tempfile
from pathlib import Path

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

from orchestrator import council, intake
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --- the status-query gate fires on real board questions, not on greetings ---
fires = ["What's the status of Jira tickets on all the projects?", "And EU board?",
         "what's left to do?", "any tickets in progress?", "where are we on the backlog?"]
quiet = ["hey, how's it going?", "thanks, that's great", "good morning", "you're awesome"]
chk("gate fires on every status/board question", all(council._BOARD_Q.search(m) for m in fires))
chk("gate stays quiet on greetings / small talk", not any(council._BOARD_Q.search(m) for m in quiet))

# --- _board_status enumerates EVERY configured Jira app (EU + AUTO), skips non-Jira apps ---
def tk(i): return types.SimpleNamespace(id=i, key=i, summary=i)
appEU = AppConfig(name="Elite-Unit", repo_path="/x/eu", base_branch="dev", protected_branch="main",
                  backlog_backend="jira", backlog={"project_key": "EU"})
appAUTO = AppConfig(name="automatixy", repo_path="/x/auto", base_branch="DEV", protected_branch="MAIN",
                    backlog_backend="jira", backlog={"project_key": "AUTO"})
appLanding = AppConfig(name="landing", repo_path="/x/land", base_branch="main", protected_branch="main",
                       backlog_backend="none")
cfg = Config(apps=[appEU, appAUTO, appLanding],
             audit_path=str(Path(tempfile.mkdtemp()) / "a.jsonl"), use_worktree=False)

DATA = {"Elite-Unit": [(appEU, tk("EU-20")), (appEU, tk("EU-24"))], "automatixy": [(appAUTO, tk("AUTO-9"))]}
intake.from_drain = lambda c, name, limit: DATA.get(name, [])
brief = asyncio.run(council._board_status(cfg))
chk("EU board is in scope (not only AUTO)", "Elite-Unit" in brief and "Jira EU" in brief and "EU-20" in brief)
chk("AUTO board is also present", "automatixy" in brief and "Jira AUTO" in brief and "AUTO-9" in brief)
chk("open counts are shown", "2 open" in brief and "1 open" in brief)
chk("non-Jira app is skipped (no backlog)", "landing" not in brief)

# --- one board down -> a per-app note, the others still reported ---
def half_down(c, name, limit):
    if name == "automatixy":
        raise RuntimeError("503 from Jira")
    return DATA.get(name, [])
intake.from_drain = half_down
brief2 = asyncio.run(council._board_status(cfg))
chk("a down board degrades to a note, doesn't blank the rest",
    "board unreachable" in brief2 and "EU-20" in brief2)

print("\n============ GENERAL SEES ALL PROJECTS QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
