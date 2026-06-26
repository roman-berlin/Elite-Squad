"""EU-61 Part B — the cockpit HTTP layer for the proposal approval queue.

The module-level contract (enqueue / approve / deny / subset) is pinned by
eu61_proposal_approval_test.py. THIS harness pins the Flask surface that wires it to the cockpit,
which that test does not exercise:

  * GET  /needs                 renders one card per pending batch (source, target app, every
                                proposed title with a pre-checked checkbox),
  * POST /api/approve-proposals files the WHOLE batch to its board (de-duped via filing) + redirects,
  * POST /api/approve-proposals with a `titles` subset files ONLY the checked titles,
  * POST /api/deny-proposals    discards the batch, touches no board,
  * a missing/unknown batch id is handled (no crash, friendly last_msg).

A stub backlog stands in for Jira (no network); filing.make_backlog is redirected at it.
"""
import sys, types, tempfile
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda *a, **k: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import server, approvals, filing
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# Never let the approve path ping Telegram.
import orchestrator.notify as _notify
_notify.send = lambda *a, **k: None


class StubBacklog:
    """In-memory board mirroring JiraAdapter's filing contract (create_task + de-dup)."""
    def __init__(self, open_summaries=None):
        self._open = {s.strip().lower(): k for s, k in (open_summaries or {}).items()}
        self.created = []
    def find_open_by_summary(self, summary):
        return self._open.get(str(summary).strip().lower())
    def create_task(self, summary, description, labels=None, issue_type="Task"):
        key = f"AUTO-{700 + len(self.created)}"
        self.created.append((summary, list(labels or []), issue_type))
        return key


REPORT = (
    "Decision record: keep the merge gate as-is.\n"
    "===TICKETS===\n"
    '[{"title": "Add retry on flaky deploy step", "type": "Task", "severity": "MEDIUM",'
    ' "body": "deploy.sh races the health check"},'
    ' {"title": "Document the rollback runbook", "type": "Task", "severity": "LOW",'
    ' "body": "no written rollback steps"}]\n'
    "===END===\n"
)

def fresh():
    """A pristine cfg+client (its own proposals.json) so each phase is independent — the queue is
    keyed by audit_path, and identical batches across phases would otherwise collide by id."""
    tmp = Path(tempfile.mkdtemp())
    c = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                              protected_branch="MAIN", backlog_backend="none")],
               audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
    c.detected_auth = lambda: "test"
    return c, server.create_app(c).test_client()


# --- 1) GET /needs renders the proposal card ---------------------------------------------------- #
cfg, client = fresh()
bid = approvals.enqueue_proposals(cfg, app_name="automatixy", officer_label="council",
                                  source="council/daily", report=REPORT)
body = client.get("/needs").get_data(as_text=True)
chk("needs page shows the batch source", "council/daily" in body)
chk("needs page lists the first proposed title", "Add retry on flaky deploy step" in body)
chk("needs page lists the second proposed title", "Document the rollback runbook" in body)
chk("needs page posts to the approve endpoint", "/api/approve-proposals" in body)
chk("needs page posts to the deny endpoint", "/api/deny-proposals" in body)
chk("needs page carries the batch id in the form", bid in body)
chk("titles are pre-checked checkboxes", 'type=checkbox name=titles' in body and "checked" in body)


# --- 2) POST approve (whole batch) files to the board, de-duped ------------------------------ #
stub = StubBacklog(open_summaries={"Document the rollback runbook": "AUTO-321"})
filing.make_backlog = lambda a: stub
r = client.post("/api/approve-proposals", data={"batch": bid,
                "titles": ["Add retry on flaky deploy step", "Document the rollback runbook"]})
chk("approve redirects back to /needs", r.status_code in (302, 303) and "/needs" in r.headers.get("Location", ""))
chk("approve filed exactly the one NEW ticket", len(stub.created) == 1, str(stub.created))
chk("approve de-duped the already-open ticket",
    [s for s, *_ in stub.created] == ["Add retry on flaky deploy step"], str(stub.created))
chk("approve clears the batch from the queue", approvals.pending_proposals(cfg) == [])
chk("approve reports a filed count in last_msg", "Filed 1" in server._state.get("last_msg", ""),
    server._state.get("last_msg", ""))


# --- 3) POST approve with a subset files ONLY the checked titles ------------------------------ #
cfg, client = fresh()
bid2 = approvals.enqueue_proposals(cfg, app_name="automatixy", officer_label="meeting",
                                   source="meeting: deploy", report=REPORT)
stub2 = StubBacklog()
filing.make_backlog = lambda a: stub2
client.post("/api/approve-proposals", data={"batch": bid2, "titles": ["Add retry on flaky deploy step"]})
chk("subset approve creates only the chosen title",
    [s for s, *_ in stub2.created] == ["Add retry on flaky deploy step"], str(stub2.created))
chk("subset approve leaves nothing pending", approvals.pending_proposals(cfg) == [])


# --- 4) POST deny discards the batch, touches no board --------------------------------------- #
cfg, client = fresh()
bid3 = approvals.enqueue_proposals(cfg, app_name="automatixy", officer_label="council",
                                   source="council/daily", report=REPORT)
stub3 = StubBacklog()
filing.make_backlog = lambda a: stub3
r3 = client.post("/api/deny-proposals", data={"batch": bid3, "reason": "not now"})
chk("deny redirects back to /needs", r3.status_code in (302, 303) and "/needs" in r3.headers.get("Location", ""))
chk("deny created nothing on the board", stub3.created == [], str(stub3.created))
chk("deny clears the batch from the queue", approvals.pending_proposals(cfg) == [])
chk("deny reports it in last_msg", "denied" in server._state.get("last_msg", "").lower(),
    server._state.get("last_msg", ""))


# --- 5) an unknown/already-actioned batch id is handled, not a 500 --------------------------- #
cfg, client = fresh()
r4 = client.post("/api/approve-proposals", data={"batch": "does-not-exist"})
chk("approving an unknown batch does not 500", r4.status_code in (302, 303))
chk("unknown batch reports 'already actioned'",
    "already actioned" in server._state.get("last_msg", "").lower(), server._state.get("last_msg", ""))
r5 = client.post("/api/approve-proposals", data={})    # no batch field at all
chk("missing batch field does not 500", r5.status_code in (302, 303))


passed = sum(1 for _, c, _ in results if c)
for n, c, d in results:
    print(f"  {'✓' if c else '✗'} {n}" + (f"  [{d}]" if (not c and d) else ""))
print(f"{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
