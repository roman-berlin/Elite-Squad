"""Brief escalations + reliable answer QA: dashboard.brief() turns a wall of PM text into a scannable
ask, needs_detail_html leads with it (full text behind a toggle), and /api/answer clears the Needs-you
row when there's no pending decision so answering visibly 'takes'."""
import sys, types, tempfile
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

from orchestrator import dashboard as D, server, decisions, autopilot
import orchestrator.backlog.base as backlog_base
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --- brief(): structured PM escalation -> just the ask lines, capped ---
pm_wall = ("BLOCKER: the canonical console IA is not in the repo.\n"
           "DECISION: which nav grouping is canonical?\n"
           "OPTIONS: keep v1 draft vs the new 8/5 scheme\n"
           "RECOMMENDATION: adopt the 8/5 scheme — reversible.\n\n"
           "Here is my full reasoning, re-deriving the entire ticket: " + "blah " * 400)
b = D.brief(pm_wall)
chk("brief: keeps the structured ask lines", "BLOCKER" in b and "DECISION" in b and "RECOMMENDATION" in b)
chk("brief: drops the reasoning wall", "blah blah blah" not in b)
chk("brief: capped short", len(b) <= 400, str(len(b)))

# --- brief(): plain prose -> first sentences only ---
prose = "Alpha happened. Beta is the cause. Gamma is one option. " + "noise " * 300
bp = D.brief(prose)
chk("brief: prose -> first sentences", bp.startswith("Alpha happened.") and "noise noise noise" not in bp)
chk("brief: prose capped", len(bp) <= 400)
chk("brief: empty -> empty", D.brief("") == "" and D.brief(None) == "")

# --- needs_detail_html leads with the brief, full text contained ---
t = {"note": pm_wall, "passes_list": []}
h = D.needs_detail_html(t)
chk("detail: leads with 'The ask:'", "The ask:" in h and "BLOCKER" in h)
chk("detail: full message behind a toggle", "full message" in h and "<details" in h)
chk("detail: full note is capped, not an unbounded wall", h.count("blah") < 350)

# --- /api/answer clears the row when there's no pending decision ---
class _SyncThread:
    def __init__(s, target=None, daemon=None): s.t = target
    def start(s):
        if s.t:
            s.t()
server.threading = types.SimpleNamespace(Thread=_SyncThread)

tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="jira",
                             backlog={"base_url": "https://x", "project_key": "AUTO"})],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()

decisions.handle_reply = lambda c, a, text: False          # no pending decision on file
class _FakeBL:
    def add_comment(s, ticket, body): pass
backlog_base.make_backlog = lambda app: _FakeBL()
autopilot.unblock = lambda c, tid: "unblocked"
dismissed = {}
D.dismiss = lambda audit_path, tid: dismissed.__setitem__("tid", tid)

client.post("/api/answer", data={"ticket": "AUTO-9", "app": "automatixy", "text": "go with the 8/5 scheme"})
chk("answer (no decision) -> dismisses the Needs-you row", dismissed.get("tid") == "AUTO-9")
chk("answer (no decision) -> says it cleared + re-running",
    "cleared from Needs-you" in server._state.get("last_msg", "")
    and "re-running" in server._state.get("last_msg", ""))

print("\n============ BRIEF + ANSWER QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
