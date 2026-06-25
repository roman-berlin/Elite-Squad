"""Needs-you answer box QA: /api/answer resolves a pending decision + re-runs (handle_reply); with no
pending decision it records the answer as a ticket comment + unblocks; the Needs-you cards render the
Ship-answer form on parked tickets."""
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

from orchestrator import server, decisions, autopilot, needs, dashboard
import orchestrator.backlog.base as backlog_base
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# run the endpoint's background thread INLINE so asserts are deterministic
class _SyncThread:
    def __init__(s, target=None, daemon=None): s.t = target
    def start(s):
        if s.t:
            s.t()
server.threading = types.SimpleNamespace(Thread=_SyncThread)

tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="jira",
                             backlog={"base_url": "https://toibis.atlassian.net", "project_key": "AUTO"})],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()

# --- Case 1: a pending decision -> handle_reply (resolve + re-run with the answer) ---
calls = {}
decisions.handle_reply = lambda c, a, text: (calls.__setitem__("reply", text) or True)
client.post("/api/answer", data={"ticket": "AUTO-14", "app": "automatixy", "text": "use the 8/5 IA"})
chk("answer -> handle_reply called as 'TICKET: answer'", calls.get("reply") == "AUTO-14: use the 8/5 IA")
chk("answer -> success message", "re-running" in server._state.get("last_msg", ""))

# --- Case 2: NO pending decision -> record as a ticket comment + unblock ---
decisions.handle_reply = lambda c, a, t: False
posted = {}
class _FakeBL:
    def add_comment(s, ticket, body):
        posted["key"] = ticket.key
        posted["body"] = body
backlog_base.make_backlog = lambda app: _FakeBL()
unblocked = {}
autopilot.unblock = lambda c, tid: (unblocked.__setitem__("tid", tid) or "unblocked")
client.post("/api/answer", data={"ticket": "AUTO-9", "app": "automatixy", "text": "go with option A"})
chk("no decision -> answer posted as a ticket comment", posted.get("key") == "AUTO-9" and posted.get("body") == "go with option A")
chk("no decision -> ticket unblocked for retry", unblocked.get("tid") == "AUTO-9")
chk("no decision -> message says sent + cleared + re-running",
    "cleared from Needs-you" in server._state.get("last_msg", "")
    and "re-running" in server._state.get("last_msg", ""))

# --- Case 3: empty ticket/answer -> no-op ---
calls.clear()
decisions.handle_reply = lambda c, a, text: (calls.__setitem__("reply", text) or True)
client.post("/api/answer", data={"ticket": "", "text": ""})
chk("empty -> no handle_reply, no crash", "reply" not in calls)

# --- Case 4: the Needs-you page renders the Ship-answer form on parked tickets ---
dashboard.needs_detail_html = lambda t: "detail"
dashboard.needs_chat_summary = lambda t: "summary"
dashboard._short = lambda s, n=120: (s or "")[:n]
needs.summary = lambda c: {"total": 2,
                           "decisions": [{"id": "AUTO-1", "app": "automatixy", "question": "pick a date"}],
                           "approvals": [],
                           "tasks": [{"ticket_id": "AUTO-14", "app": "automatixy",
                                      "outcome": "awaiting decision", "note": "product blocker"}]}
body = client.get("/needs").get_data(as_text=True)
chk("/needs decisions form ships to /api/answer", "action=/api/answer" in body)
chk("/needs parked card has a Ship-answer box", "Ship answer" in body and "Answer the unit" in body)
chk("/needs answer form carries the app", "value='automatixy'" in body)
chk("/needs still offers Discuss + Dismiss", "Discuss with the CTO" in body and "Dismiss" in body)

# --- Case 5: the confirmation banner renders on /needs (one-shot), so the answer visibly "took" ---
server._state["last_msg"] = "✓ Answer sent to AUTO-77 — cleared from Needs-you; re-running."
body2 = client.get("/needs").get_data(as_text=True)
chk("/needs shows the confirmation banner", "nbanner" in body2 and "Answer sent to AUTO-77" in body2)
chk("banner is one-shot (cleared after showing)", not server._state.get("last_msg"))

print("\n============ NEEDS-YOU ANSWER QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
