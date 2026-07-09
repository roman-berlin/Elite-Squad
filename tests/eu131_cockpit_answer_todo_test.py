"""EU-131: Cockpit answer → To Do transition.

Covers the full flow of answering a Needs-you escalation from the cockpit:
  (1) Answer a pending decision → verify comment posted, decision popped,
      and ticket moves to 'To Do' (via background thread).
  (2) Answer a ticket with no pending decision → verify comment posted, unblocked,
      and status set to 'To Do'.
  (3) Verify the row disappears from Needs-you immediately.

Uses FakeBacklog to capture status transitions and comments. No network, no real models.
"""
import sys
import types
import tempfile
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
req.RequestException = type('RequestException', (Exception,), {})
class _FakeResponse:
    status_code = 200
    text = ""
req.post = lambda *a, **k: _FakeResponse()
req.get = lambda *a, **k: _FakeResponse()
sys.modules["requests"] = req

# ── make the endpoint run its background task synchronously ──────────────────
class _SyncThread:
    """Replace threading.Thread so the _bg closure runs inline, giving deterministic asserts."""
    def __init__(s, target=None, daemon=None):
        s.t = target
    def start(s):
        if s.t:
            s.t()

# Monkey-patch threading.Thread BEFORE any imports that use it
import threading as _threading_mod
_original_thread = _threading_mod.Thread
_threading_mod.Thread = _SyncThread

sys.path.insert(0, ".")

from orchestrator import decisions, autopilot, dashboard as D, needs
from orchestrator import server
import orchestrator.backlog.base as backlog_base
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket

# ── soft-assert helper ────────────────────────────────────────────────────────
results = []
def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))

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

# ── FakeBacklog: captures status transitions and comments ─────────────────────
class FakeBacklog:
    """In-memory backlog that records calls for verification."""
    def __init__(self):
        self.status_calls = []
        self.comment_calls = []

    def set_status(self, ticket, status: str):
        self.status_calls.append({"ticket": ticket.key, "status": status})

    def add_comment(self, ticket, body: str):
        self.comment_calls.append({"ticket": ticket.key, "body": body})

    def get_task(self, key):
        return Ticket(id=key, key=key, summary="Test ticket", description="Test", app="automatixy")

fake = FakeBacklog()
backlog_base.make_backlog = lambda app: fake

# ── (1) Answer a pending decision → comment posted, decision popped ─────────────
print("\n--- Test 1: Answer with pending decision ---")

# Setup: create a pending decision
decisions._save(cfg, [])
decisions.add(
    cfg,
    Ticket(id="AUTO-1", key="AUTO-1", summary="Test", description="Test", app="automatixy"),
    "automatixy",
    "Which date format should we use?"
)

# Verify the decision was parked
loaded = decisions.load(cfg)
chk("Test 1 setup: decision parked in pending_decisions.json",
    len(loaded) == 1 and "Which date format" in loaded[0].get("question", ""),
    str(loaded))

# Track the status when parked - should be 'Blocked'
parked_status = None
if fake.status_calls:
    parked_status = fake.status_calls[-1]["status"]
chk("Test 1 setup: parked decision transitions ticket to 'Blocked'",
    parked_status == "Blocked", f"Parked status: {parked_status}")

# Answer via the cockpit API
r = client.post("/api/answer", data={
    "ticket": "AUTO-1",
    "app": "automatixy",
    "text": "Use DD/MM/YYYY format",
})

chk("Test 1: /api/answer returns redirect (302)", r.status_code in (302, 303), str(r.status_code))

# Verify: comment posted (the answer was echoed back)
chk("Test 1: add_comment called with the Commander's answer",
    any("Use DD/MM/YYYY" in c.get("body", "") for c in fake.comment_calls),
    str(fake.comment_calls))

# Verify: decision popped from pending_decisions.json
loaded_after = decisions.load(cfg)
chk("Test 1: decision popped from pending_decisions.json",
    len(loaded_after) == 0,
    f"Before: {len(loaded)} decisions, After: {len(loaded_after)} decisions")

# Verify: ticket transitioned to 'To Do' (happens in _bg_clarify background thread,
# which runs synchronously due to _SyncThread)
chk("Test 1: set_status called with 'To Do' for pending decision ticket",
    any(c["status"] == "To Do" and c["ticket"] == "AUTO-1" for c in fake.status_calls),
    str(fake.status_calls))

# ── (2) Answer a ticket with NO pending decision → comment posted, unblocked, To Do ──
print("\n--- Test 2: Answer without pending decision ---")

# Reset FakeBacklog
fake.status_calls = []
fake.comment_calls = []

# Track unblock calls
unblock_called = []
_original_unblock = autopilot.unblock
def _track_unblock(cfg, tid):
    unblock_called.append(tid)
    return _original_unblock(cfg, tid)
autopilot.unblock = _track_unblock

# Ensure no pending decisions
decisions._save(cfg, [])
loaded_before = decisions.load(cfg)
chk("Test 2 setup: no pending decisions",
    len(loaded_before) == 0, str(loaded_before))

# Answer a ticket that has no pending decision
r2 = client.post("/api/answer", data={
    "ticket": "AUTO-2",
    "app": "automatixy",
    "text": "This is just a clarification, no decision was pending",
})

chk("Test 2: /api/answer returns redirect (302)", r2.status_code in (302, 303), str(r2.status_code))

# Verify: comment posted
chk("Test 2: add_comment called with the answer",
    any("clarification" in c.get("body", "") for c in fake.comment_calls),
    str(fake.comment_calls))

# Verify: unblock called
chk("Test 2: autopilot.unblock called for the ticket",
    "AUTO-2" in unblock_called, str(unblock_called))

# Verify: ticket transitioned to 'To Do'
chk("Test 2: set_status called with 'To Do' for non-pending ticket",
    any(c["status"] == "To Do" and c["ticket"] == "AUTO-2" for c in fake.status_calls),
    str(fake.status_calls))

# ── (3) Verify the row disappears from Needs-you immediately ───────────────────
print("\n--- Test 3: Row disappears from Needs-you ---")

# Setup: create a task that needs attention
task_file = tmp / "audit.jsonl"
task_file.write_text(
    '{"ticket_id": "AUTO-3", "outcome": "escalated", "started": "2026-06-30T12:00:00", "note": "Test escalation"}',
    encoding="utf-8"
)

# Verify it appears in Needs-you (mock the summary to include our test task)
original_summary = needs.summary
needs.summary = lambda c, a=None: {
    "decisions": [],
    "approvals": [],
    "tasks": [{
        "ticket_id": "AUTO-3",
        "outcome": "escalated",
        "started": "2026-06-30T12:00:00",
        "note": "Test escalation"
    }],
    "total": 1
}

needs_before = needs.summary(cfg)
chk("Test 3 setup: ticket appears in Needs-you before answer",
    any(t.get("ticket_id") == "AUTO-3" for t in needs_before.get("tasks", [])),
    str(needs_before))

# Answer the ticket (which should dismiss it)
r3 = client.post("/api/answer", data={
    "ticket": "AUTO-3",
    "app": "automatixy",
    "text": "Proceed with the fix",
})

# Verify: dismiss was called (row disappears)
dismissed = D.load_dismissed(cfg.audit_path)
chk("Test 3: ticket added to dismissed set (row disappears from Needs-you)",
    "AUTO-3" in dismissed, f"Dismissed tickets: {list(dismissed.keys())}")

# ── print tally ──────────────────────────────────────────────────────────────
print("\n======== EU-131 COCKPIT ANSWER → TO DO QA ========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
