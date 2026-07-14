"""EU-61 Part B: unit-proposed tickets land in an approval queue (approvals.py), and the Commander
Approves-to-file (a chosen subset, de-duped, to the right board) or Denies (nothing filed).

Exercises the queue end-to-end against a STUB backlog (no Jira, no network):
  * surface  — enqueue_proposals queues one batch; pending_proposals shows it with source + titles,
  * approve  — files the selected tickets to the BATCH's app/board, de-duped, then clears the batch,
  * subset   — per-ticket checkbox selection files only the chosen titles,
  * deny     — discards the batch; the board is never touched.
"""
import sys, types, tempfile
from pathlib import Path

sys.path.insert(0, ".")

# Stub the Agent SDK + requests so importing the orchestrator never reaches the network.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda *a, **k: None
req.RequestException = Exception
sys.modules["requests"] = req

from orchestrator import approvals, filing
from orchestrator.config import AppConfig

# Never let the approval path try to ping Telegram.
import orchestrator.notify as _notify
_notify.send = lambda *a, **k: None

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


class StubBacklog:
    """In-memory board mirroring JiraAdapter's filing contract (create_task + de-dup)."""
    def __init__(self, open_summaries=None):
        self._open = {s.strip().lower(): k for s, k in (open_summaries or {}).items()}
        self.created = []          # (summary, labels, issue_type)

    def find_open_by_summary(self, summary):
        return self._open.get(str(summary).strip().lower())

    def create_task(self, summary, description, labels=None, issue_type="Task", priority=None):
        key = f"AUTO-{700 + len(self.created)}"
        self.created.append((summary, list(labels or []), issue_type))
        return key


def _app(name):
    return AppConfig(name=name, repo_path="/tmp/x", base_branch="dev",
                     protected_branch="main", backlog_backend="none")


def _cfg(apps):
    tmp = Path(tempfile.mkdtemp())
    return types.SimpleNamespace(audit_path=str(tmp / "audit.jsonl"), apps=apps)


REPORT = (
    "Decision record: keep the merge gate as-is for now.\n"
    "===TICKETS===\n"
    '[{"title": "Add retry on flaky deploy step", "type": "Task", "severity": "MEDIUM",'
    ' "body": "deploy.sh races the health check"},'
    ' {"title": "Document the rollback runbook", "type": "Task", "severity": "LOW",'
    ' "body": "no written rollback steps"}]\n'
    "===END===\n"
)

# === 1) Surface: a batch is queued with its source + proposed titles ========================= #
cfg = _cfg([_app("automatixy"), _app("Elite-Unit")])
bid = approvals.enqueue_proposals(cfg, app_name="automatixy", officer_label="council",
                                  source="council/daily", report=REPORT)
chk("enqueue returns a batch id", bool(bid), repr(bid))
pend = approvals.pending_proposals(cfg)
chk("one batch is pending", len(pend) == 1, str(len(pend)))
batch = pend[0]
chk("batch records its source", batch.get("source") == "council/daily", str(batch.get("source")))
chk("batch records its target app", batch.get("app") == "automatixy", str(batch.get("app")))
chk("batch carries both proposed titles",
    [p["title"] for p in batch["proposals"]]
    == ["Add retry on flaky deploy step", "Document the rollback runbook"],
    str(batch["proposals"]))

# enqueue is idempotent — the same batch doesn't stack a duplicate card.
approvals.enqueue_proposals(cfg, app_name="automatixy", officer_label="council",
                            source="council/daily", report=REPORT)
chk("identical batch does not duplicate", len(approvals.pending_proposals(cfg)) == 1)

# === 2) Approve → file to the batch's board, de-duped ======================================== #
captured = {}
stub = StubBacklog(open_summaries={"Document the rollback runbook": "AUTO-321"})
filing.make_backlog = lambda a: (captured.__setitem__("app", a), stub)[1]
res = approvals.approve_proposals(cfg, bid)            # titles=None -> whole batch
chk("approve filed exactly the one new ticket", res is not None and res.filed_n == 1, str(res and res.filed))
chk("the already-open ticket was de-duped", res.deduped == ["AUTO-321"], str(res.deduped))
chk("only the new ticket was actually created", len(stub.created) == 1, str(stub.created))
chk("filed to the batch's OWN board (automatixy)",
    getattr(captured.get("app"), "name", None) == "automatixy", str(captured))
chk("approving clears the batch from the queue", approvals.pending_proposals(cfg) == [])
chk("re-approving an actioned batch is a no-op", approvals.approve_proposals(cfg, bid) is None)

# === 3) Per-ticket subset: only the checked titles are filed ================================= #
cfg2 = _cfg([_app("automatixy")])
bid2 = approvals.enqueue_proposals(cfg2, app_name="automatixy", officer_label="meeting",
                                   source="meeting: deploy hardening", report=REPORT)
stub2 = StubBacklog()
filing.make_backlog = lambda a: stub2
res2 = approvals.approve_proposals(cfg2, bid2, titles=["Add retry on flaky deploy step"])
chk("subset approve files only the chosen ticket", res2.filed_n == 1, str(res2.filed))
chk("only the chosen title was created",
    [s for s, *_ in stub2.created] == ["Add retry on flaky deploy step"], str(stub2.created))
chk("the unchosen ticket was NOT filed",
    all("rollback" not in s.lower() for s, *_ in stub2.created), str(stub2.created))

# === 4) Deny → the board is never touched =================================================== #
cfg3 = _cfg([_app("automatixy")])
bid3 = approvals.enqueue_proposals(cfg3, app_name="automatixy", officer_label="council",
                                   source="council/daily", report=REPORT)
stub3 = StubBacklog()
filing.make_backlog = lambda a: stub3
denied = approvals.deny_proposals(cfg3, bid3, reason="not now")
chk("deny reports success", denied is True)
chk("deny created nothing on the board", stub3.created == [], str(stub3.created))
chk("deny clears the batch from the queue", approvals.pending_proposals(cfg3) == [])
chk("denying an already-actioned batch is a no-op", approvals.deny_proposals(cfg3, bid3) is False)

# === 5) needs.summary folds the proposals stream into the single 'Needs you' total =========== #
try:
    from orchestrator import needs
    cfg4 = _cfg([_app("automatixy")])
    approvals.enqueue_proposals(cfg4, app_name="automatixy", officer_label="council",
                                source="council/daily", report=REPORT)
    s = needs.summary(cfg4)
    chk("needs.summary surfaces the proposal batch", len(s.get("proposals", [])) == 1, str(s.get("proposals")))
    chk("the batch counts toward the Needs-you total", s.get("total", 0) >= 1, str(s.get("total")))
except Exception as exc:  # noqa: BLE001 - summary pulls other streams; don't let them sink this slice
    chk("needs.summary integration", False, f"raised: {exc}")

# === 6) 2026-07-06 review fixes: approval notify, filing-window dedup, stale-claim recovery == #
# (a) approve sends the '✅ Approved & filed' notification — the two-phase refactor briefly
#     referenced a renamed variable and the NameError was swallowed, silencing EVERY approval.
import orchestrator.notify as _notify_mod
_sent: list[str] = []
_notify_mod.send = lambda *a, **k: (_sent.append(a[0] if a else ""), True)[1]
cfg6 = _cfg([_app("automatixy")])
bid6 = approvals.enqueue_proposals(cfg6, app_name="automatixy", officer_label="council",
                                   source="council/notify-pin", report=REPORT)
filing.make_backlog = lambda a: StubBacklog()
res6 = approvals.approve_proposals(cfg6, bid6)
chk("approve sends the 'Approved & filed' notification (NameError regression pin)",
    res6 is not None and any("Approved & filed" in m and "council/notify-pin" in m for m in _sent),
    str(_sent))
_notify_mod.send = lambda *a, **k: None

# (b) re-enqueuing the identical report while its batch is mid-claim ('filing') must reuse the
#     batch, not append a duplicate id that can never be approved, denied, or trimmed.
cfg7 = _cfg([_app("automatixy")])
bid7 = approvals.enqueue_proposals(cfg7, app_name="automatixy", officer_label="council",
                                   source="council/daily", report=REPORT)
import time as _time


def _mark_filing(items):
    for b in items:
        if b.get("id") == bid7:
            b["status"] = "filing"
            b["claim_ts"] = _time.time()          # fresh claim — someone is filing right now
    return items


approvals._mutate_proposals(cfg7, _mark_filing)
bid7_again = approvals.enqueue_proposals(cfg7, app_name="automatixy", officer_label="council",
                                         source="council/daily", report=REPORT)
chk("re-enqueue during the 'filing' window reuses the batch (no duplicate id)",
    bid7_again == bid7 and sum(1 for b in approvals._load_proposals(cfg7)
                               if b.get("id") == bid7) == 1,
    str(approvals._load_proposals(cfg7)))
chk("a FRESH 'filing' claim is not actionable (concurrent approve sees it as taken)",
    approvals.approve_proposals(cfg7, bid7) is None and not approvals.pending_proposals(cfg7))

# (c) a STALE 'filing' claim (dead process) recovers: visible as pending, approvable again.
def _age_claim(items):
    for b in items:
        if b.get("id") == bid7:
            b["claim_ts"] = _time.time() - approvals._FILING_STALE_S - 1
    return items


approvals._mutate_proposals(cfg7, _age_claim)
chk("a STALE 'filing' claim resurfaces in pending_proposals (crash can't hide a batch)",
    [b.get("id") for b in approvals.pending_proposals(cfg7)] == [bid7])
stub7 = StubBacklog()
filing.make_backlog = lambda a: stub7
res7 = approvals.approve_proposals(cfg7, bid7)
chk("a STALE 'filing' claim is approvable again (recovery path files the tickets)",
    res7 is not None and res7.filed_n == 2, str(res7 and res7.filed))

passed = sum(1 for _, c, _ in results if c)
for n, c, d in results:
    print(f"  {'✓' if c else '✗'} {n}" + (f"  [{d}]" if (not c and d) else ""))
print(f"{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
