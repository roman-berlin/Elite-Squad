"""EU-61 (Ordnance BE slice 2): the product-decision round-trip THROUGH Jira.

Pins the four hand-offs of the round-trip:

  1. park → Blocked      : decisions.add transitions the parked ticket to 'Blocked' and snapshots the
                           current latest-human-comment as a resume baseline.
  2. answer → comment    : decisions.resolve posts the Commander's answer back onto the ticket as a
                           comment (the write half of the round-trip).
  3. Jira-answer → read  : loop._resume_from_jira_answer reads the latest human comment (slice 1's
     + inject            : adapter.latest_answer) and folds the decision Q+A into the builder context.
  4. resume → In Progress: through process_ticket — a parked ticket the Commander answered on Jira is
                           re-read, injected, and transitioned Blocked → In Progress; the autopilot
                           lifts an answered-on-Jira ticket out of its skip-set.

A FakeBacklog records set_status / add_comment and serves a scripted latest_answer; the pending-decision
store is a real temp file; no network, no real models.
"""
import sys, types, tempfile, asyncio
from pathlib import Path

# Stub the Agent SDK + requests so importing the orchestrator never reaches the network.
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

from orchestrator import decisions, loop, autopilot
from orchestrator.backlog import base as backlog_base
from orchestrator.contracts import Outcome, Ticket, TicketReport
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

ns = types.SimpleNamespace


class FakeBacklog:
    """In-memory stand-in for a JiraAdapter: records status/comment writes and serves a scripted answer."""
    def __init__(self, latest=""):
        self.status = None
        self.comment = None
        self.latest = latest
    def set_status(self, ticket, status): self.status = status
    def add_comment(self, ticket, body): self.comment = body
    def latest_answer(self, ticket): return self.latest or None
    def get_task(self, key):
        return Ticket(id=key, key=key, summary="do thing", description="Original spec.", app="automatixy")


tmp = Path(tempfile.mkdtemp())
app = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev", protected_branch="main",
                backlog_backend="jira", backlog={"base_url": "https://x.atlassian.net", "project_key": "AUTO"})
cfg = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
TKT = Ticket(id="AUTO-1", key="AUTO-1", summary="do thing", description="Original spec.",
             acceptance_criteria=["x"], app="automatixy", ephemeral=False)
silent = ns(record=lambda *a, **k: None)
decisions.notify = ns(send=lambda *a, **k: None, configured=lambda: False)


def use(fake):
    """Point every lazy make_backlog() at this fake (decisions + autopilot import it from backlog.base)."""
    backlog_base.make_backlog = lambda a: fake
    return fake


# --- 1) park → Blocked (+ baseline snapshot) ----------------------------------------------------- #
decisions._save(cfg, [])
f1 = use(FakeBacklog(latest=""))            # no human comment on the ticket yet
decisions.add(cfg, TKT, "automatixy", "Which date format — DD/MM or ISO-8601?")
chk("park transitions the ticket to 'Blocked'", f1.status == "Blocked", str(f1.status))
entry = decisions.load(cfg)[0]
chk("park records the decision in 'Needs you'", entry["question"].startswith("Which date format"))
chk("park snapshots a resume baseline (no prior human comment)", entry.get("answer_baseline") == "")

# an ephemeral ticket must NOT touch the tracker (no Jira identity)
decisions._save(cfg, [])
f_eph = use(FakeBacklog())
decisions.add(cfg, Ticket(id="adhoc-x", key="adhoc-x", summary="s", description="d",
                          app="automatixy", ephemeral=True), "automatixy", "q")
chk("ephemeral park does NOT transition the tracker", f_eph.status is None)

# a SUB-decision (distinct entry_id) must NOT re-park the ticket
decisions._save(cfg, [])
f_sub = use(FakeBacklog())
decisions.add(cfg, TKT, "automatixy", "File these?", entry_id="AUTO-1#out-of-scope")
chk("a distinct-entry_id sub-decision does not block the ticket", f_sub.status is None)

# --- 2) answer → comment ------------------------------------------------------------------------- #
decisions._save(cfg, [])
f2 = use(FakeBacklog(latest=""))
decisions.add(cfg, TKT, "automatixy", "Which date format?")
res = decisions.resolve(cfg, "Use DD/MM/YYYY", "AUTO-1")
chk("resolve returns the decision carrying the answer", res and res["answer"] == "Use DD/MM/YYYY")
chk("answer → posted back onto the ticket as a comment", "Use DD/MM/YYYY" in (f2.comment or ""), str(f2.comment))
chk("the pending decision is popped on resolve", decisions.load(cfg) == [])

# --- 3) Jira-answer → read + inject (loop._resume_from_jira_answer) ------------------------------- #
decisions._save(cfg, [])
f3 = use(FakeBacklog(latest=""))
decisions.add(cfg, TKT, "automatixy", "Which date format?")     # baseline ""
f3.latest = "Use ISO-8601"                                       # Commander answered directly on Jira
out = loop._resume_from_jira_answer(cfg, TKT, f3, silent)
chk("Jira answer is read and folded into the builder context",
    "Use ISO-8601" in out.description and "Which date format" in out.description, out.description[-120:])
chk("the original spec is preserved alongside the decision", "Original spec." in out.description)
chk("the park is cleared after a Jira-native resume", decisions.load(cfg) == [])
chk("Jira-native resume does NOT echo the answer back as a new comment", f3.comment is None)

# unanswered (only the pre-park baseline comment) → still parked, no injection
decisions._save(cfg, [])
f3b = use(FakeBacklog(latest="stale QA note"))   # a human comment existed BEFORE the park
decisions.add(cfg, TKT, "automatixy", "Which date format?")     # baseline = "stale QA note"
out_b = loop._resume_from_jira_answer(cfg, TKT, f3b, silent)
chk("a pre-park comment is NOT mistaken for an answer (stays parked)",
    out_b.description == TKT.description and len(decisions.load(cfg)) == 1)

# --- 4) resume → In Progress (end-to-end through process_ticket) + autopilot pickup --------------- #
captured = {}
async def fake_attempt(ticket, app_, cfg_, git, backlog, audit, budget, branch, stop_event=None,
                       commenter=None):
    captured["desc"] = ticket.description
    return TicketReport(ticket.id, Outcome.MERGED, 1, 0.0, app_.name, branch)
loop._attempt = fake_attempt
loop._cleanup = lambda *a, **k: None
loop._notify = lambda *a, **k: None
loop._repin_worktree_deps = lambda *a, **k: None

class _Git:
    def checkout_feature(self, b): pass

decisions._save(cfg, [])
f4 = use(FakeBacklog(latest=""))
decisions.add(cfg, TKT, "automatixy", "Which date format?")     # parks → Blocked, baseline ""
f4.latest = "Use DD/MM/YYYY"                                     # Commander answered on Jira
rep = asyncio.run(loop.process_ticket(TKT, app, cfg, _Git(), f4, silent, budget=None))
chk("resume → ticket transitioned Blocked → In Progress", f4.status == "In Progress", str(f4.status))
chk("resume → Jira answer reached the builder context", "Use DD/MM/YYYY" in captured.get("desc", ""))
chk("resume → the park is cleared", decisions.load(cfg) == [])

# autopilot: an answered, parked ticket is detected for auto-resume; an unanswered one is left parked
decisions._save(cfg, [])
f5 = use(FakeBacklog(latest=""))
decisions.add(cfg, TKT, "automatixy", "Which date format?")     # baseline ""
chk("autopilot leaves an UNANSWERED parked ticket parked",
    autopilot._resumable_answered(cfg, "automatixy", {"AUTO-1"}) == {})
f5.latest = "Go with option B"                                   # Commander answered on Jira
resumable = autopilot._resumable_answered(cfg, "automatixy", {"AUTO-1"})
chk("autopilot detects an ANSWERED parked ticket for resume", "AUTO-1" in resumable)
chk("autopilot resume carries the (app, ticket) to re-enqueue",
    "AUTO-1" in resumable and resumable["AUTO-1"][1].id == "AUTO-1")

print("\n================ EU-61 DECISION ROUND-TRIP QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
