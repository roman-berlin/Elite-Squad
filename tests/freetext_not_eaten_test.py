"""F12 regression: while a decision is parked, a BARE free-text message must NOT be
swallowed as the answer to the oldest pending decision. Only an EXPLICIT reply — the
`TICKET-ID: <decision>` form or a leading reply marker (↩️ / re:) — resolves a parked
decision; everything else routes to the CTO chat (commander_msg + council reply)."""
import sys, types, tempfile
from pathlib import Path

# --- stub heavy/optional deps so the module imports cleanly ---
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

from orchestrator import decisions
from orchestrator.contracts import Ticket
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# never start a real build / council thread; just record which path was taken
resumed = []
decisions.handle_reply = lambda cfg, audit, text: (resumed.append(text) or True)

council = types.ModuleType("orchestrator.council")
freetext = []
async def _respond(cfg, text):
    freetext.append(text)
council.respond_to_commander = _respond
council.add_commander_note = lambda cfg, text: None
sys.modules["orchestrator.council"] = council

decisions.notify = types.SimpleNamespace(send=lambda *a, **k: None, configured=lambda: False)
audit = types.SimpleNamespace(record=lambda *a, **k: None)

tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="jira",
                             backlog={"base_url": "https://x.atlassian.net", "project_key": "AUTO"})],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)

def seed():
    decisions._save(cfg, [])
    decisions.add(cfg, Ticket(id="AUTO-1", key="AUTO-1", summary="do thing", description="d",
                              acceptance_criteria=["x"], app="automatixy", ephemeral=True),
                  "automatixy", "pick a date")

# 1) bare free text while a decision is parked -> CTO chat, decision NOT touched
seed(); resumed.clear()
decisions.route_message(cfg, audit, "hey what's the eta on the dashboard?")
chk("bare free text does NOT resolve a parked decision", not resumed)
chk("bare free text still parked", len(decisions.load(cfg)) == 1)

# 2) explicit id: form -> resolves
seed(); resumed.clear()
decisions.route_message(cfg, audit, "AUTO-1: use DD/MM")
chk("id: form resolves the decision", resumed == ["AUTO-1: use DD/MM"])

# 3) leading marker (emoji) -> resolves, marker stripped
seed(); resumed.clear()
decisions.route_message(cfg, audit, "↩️ use DD/MM")
chk("emoji marker resolves the oldest pending", resumed == ["use DD/MM"])

# 4) 're:' marker -> resolves, marker stripped
seed(); resumed.clear()
decisions.route_message(cfg, audit, "re: use DD/MM")
chk("re: marker resolves the oldest pending", resumed == ["use DD/MM"])

# 5) detector itself
chk("is_explicit_reply: bare text False", decisions.is_explicit_reply("just a note") is False)
chk("is_explicit_reply: id form True", decisions.is_explicit_reply("AUTO-1: x") is True)
chk("is_explicit_reply: marker True", decisions.is_explicit_reply("↩️ x") is True)

print("\n============ F12 FREE-TEXT-NOT-EATEN QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
