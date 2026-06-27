"""EU-82: Tests for directive intent classification and /api/answer routing.

Covers:
  (a) classify_intent('open a ticket: X') returns 'file_ticket'
  (b) /api/answer happy-path for 'file a ticket' — create_task called once with the
      correct title; decisions.handle_reply and autopilot.unblock are NOT called.
  (c) 'close / won't do' intent routes to close (set_status), not re-run.
  (d) A genuine clarification still calls decisions.handle_reply.
"""
import sys
import tempfile
import types
from pathlib import Path

# --- Stub out the claude_agent_sdk and requests before importing orchestrator ---
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req

sys.path.insert(0, ".")

from orchestrator.intent import classify_intent
from orchestrator import server, decisions, autopilot
import orchestrator.backlog.base as backlog_base
from orchestrator.config import Config, AppConfig

# ── soft-assert helper ────────────────────────────────────────────────────────
results = []
def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))

# ── make the endpoint run its background task synchronously ──────────────────
class _SyncThread:
    """Replace threading.Thread so the _bg closure runs inline, giving deterministic asserts."""
    def __init__(s, target=None, daemon=None):
        s.t = target
    def start(s):
        if s.t:
            s.t()

server.threading = types.SimpleNamespace(Thread=_SyncThread)

# ── shared Flask test client ──────────────────────────────────────────────────
tmp = Path(tempfile.mkdtemp())
cfg = Config(
    apps=[AppConfig(
        name="automatixy",
        repo_path=str(tmp),
        base_branch="DEV",
        protected_branch="MAIN",
        backlog_backend="jira",
        backlog={"base_url": "https://toibis.atlassian.net", "project_key": "AUTO"},
    )],
    audit_path=str(tmp / "audit.jsonl"),
    use_worktree=False,
)
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()

# ── (a) classify_intent: 'open a ticket: X' → 'file_ticket' ─────────────────
chk("classify_intent: 'open a ticket: X' returns file_ticket",
    classify_intent("open a ticket: X") == "file_ticket")
chk("classify_intent: 'create a ticket for this' returns file_ticket",
    classify_intent("create a ticket for this") == "file_ticket")
chk("classify_intent: 'file a new ticket' returns file_ticket",
    classify_intent("file a new ticket") == "file_ticket")
chk("classify_intent: 'done' returns close",
    classify_intent("done") == "close")
chk("classify_intent: \"won't fix\" returns close",
    classify_intent("won't fix") == "close")
chk("classify_intent: 'later' returns defer",
    classify_intent("later") == "defer")
chk("classify_intent: plain sentence returns clarification",
    classify_intent("use the Monday date format everywhere") == "clarification")

# ── (b) /api/answer: 'file a ticket' → create_task called once, no handle_reply / unblock ──
create_task_calls: list = []
handle_reply_calls: list = []
unblock_calls: list = []

class _FakeBacklog:
    """Minimal stub that records create_task / add_comment / set_status calls."""
    def create_task(self, summary: str, description: str, labels=None, issue_type="Task"):
        create_task_calls.append({"summary": summary, "description": description, "labels": labels})
        return "AUTO-99"
    def add_comment(self, ticket, body: str):
        pass
    def set_status(self, ticket, status: str):
        pass

backlog_base.make_backlog = lambda app: _FakeBacklog()
decisions.handle_reply = lambda c, a, text: (handle_reply_calls.append(text) or False)
autopilot.unblock = lambda c, tid: (unblock_calls.append(tid) or "unblocked")

client.post("/api/answer", data={
    "ticket": "AUTO-14",
    "app": "automatixy",
    "text": "open a ticket: add CSV export to the reports page",
})

chk("file_ticket: create_task called exactly once",
    len(create_task_calls) == 1, str(create_task_calls))
chk("file_ticket: create_task summary contains the source ticket id",
    "AUTO-14" in (create_task_calls[0]["summary"] if create_task_calls else ""),
    str(create_task_calls))
chk("file_ticket: create_task summary contains the Commander's text",
    "CSV export" in (create_task_calls[0]["summary"] if create_task_calls else ""),
    str(create_task_calls))
chk("file_ticket: decisions.handle_reply NOT called",
    len(handle_reply_calls) == 0, str(handle_reply_calls))
chk("file_ticket: autopilot.unblock NOT called",
    len(unblock_calls) == 0, str(unblock_calls))

# ── (c) 'close / won't do' intent routes to set_status(Done), NOT decisions.handle_reply ──
set_status_calls: list = []
add_comment_calls: list = []
handle_reply_calls2: list = []
unblock_calls2: list = []

class _FakeBacklogClose:
    def create_task(self, *a, **k): return None
    def add_comment(self, ticket, body: str):
        add_comment_calls.append(body)
    def set_status(self, ticket, status: str):
        set_status_calls.append({"ticket": ticket.key, "status": status})

backlog_base.make_backlog = lambda app: _FakeBacklogClose()
decisions.handle_reply = lambda c, a, text: (handle_reply_calls2.append(text) or False)
autopilot.unblock = lambda c, tid: (unblock_calls2.append(tid) or "unblocked")

client.post("/api/answer", data={
    "ticket": "AUTO-20",
    "app": "automatixy",
    "text": "won't fix — no longer needed",
})

chk("close: set_status called with Done",
    any(c["status"] == "Done" for c in set_status_calls), str(set_status_calls))
chk("close: ticket key passed correctly to set_status",
    any(c["ticket"] == "AUTO-20" for c in set_status_calls), str(set_status_calls))
chk("close: decisions.handle_reply NOT called",
    len(handle_reply_calls2) == 0, str(handle_reply_calls2))
chk("close: autopilot.unblock NOT called",
    len(unblock_calls2) == 0, str(unblock_calls2))

# ── (d) A genuine clarification still calls decisions.handle_reply ────────────
handle_reply_calls3: list = []
unblock_calls3: list = []

decisions.handle_reply = lambda c, a, text: (handle_reply_calls3.append(text) or True)
autopilot.unblock = lambda c, tid: (unblock_calls3.append(tid) or "unblocked")

client.post("/api/answer", data={
    "ticket": "AUTO-9",
    "app": "automatixy",
    "text": "use the DD/MM format everywhere",
})

chk("clarification: decisions.handle_reply IS called",
    len(handle_reply_calls3) == 1, str(handle_reply_calls3))
chk("clarification: handle_reply receives 'TICKET: answer' form",
    handle_reply_calls3[0] == "AUTO-9: use the DD/MM format everywhere"
    if handle_reply_calls3 else False,
    str(handle_reply_calls3))
chk("clarification: autopilot.unblock NOT called (handle_reply returned True)",
    len(unblock_calls3) == 0, str(unblock_calls3))

# ── print tally ──────────────────────────────────────────────────────────────
print("\n======== EU-82 DIRECTIVE INTENT QA ========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
