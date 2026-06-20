"""Worktree lock: a parallel run can't reset/clean a busy worktree — its tickets DEFER, never clobber."""
import sys, types, asyncio, tempfile
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.loop as loop
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket, TicketReport, Outcome

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# ---- the lock mechanism ----
wt = str(Path(tempfile.mkdtemp()) / "automatixy")
with loop._worktree_lock(wt):
    busy = False
    try:
        with loop._worktree_lock(wt):
            pass
    except loop.WorktreeBusy:
        busy = True
    chk("second acquire while held -> WorktreeBusy", busy)
    # a different app path is an independent lock
    indep = True
    try:
        with loop._worktree_lock(wt + "2"):
            pass
    except loop.WorktreeBusy:
        indep = False
    chk("different app path -> independent lock", indep)
# released on context exit -> re-acquire works
reok = True
try:
    with loop._worktree_lock(wt):
        pass
except loop.WorktreeBusy:
    reok = False
chk("re-acquire after release works", reok)

# ---- run() defers a busy app instead of clobbering ----
loop._notify = lambda cfg, t: None
wtdir = tempfile.mkdtemp()
loop._worktree_path = lambda app, cfg: str(Path(wtdir) / app.name)
class DummyGit:
    def ensure_clean(self): pass
loop._make_git = lambda cfg, app: DummyGit()
called = {"process": False}
async def fake_process(ticket, app, cfg, git, backlog, audit, budget, stop_event=None):
    called["process"] = True
    return TicketReport(ticket.id, Outcome.MERGED, 0, 0.0, app.name)
loop.process_ticket = fake_process

class A:
    def record(s, *a, **k): pass

app = AppConfig(name="automatixy", repo_path=".", base_branch="DEV", protected_branch="MAIN", backlog_backend="none")
cfg = Config(apps=[app], audit_path="/tmp/x.jsonl", use_worktree=True)
ticket = Ticket(id="AUTO-14", key="AUTO-14", summary="s", description="d", ephemeral=True, app="automatixy")

# (a) a parallel run holds the lock -> the new run DEFERS, does not process
called["process"] = False
with loop._worktree_lock(str(Path(wtdir) / "automatixy")):
    reports = asyncio.run(loop.run(cfg, [(app, ticket)], A()))
chk("busy worktree -> ticket DEFERRED (SKIPPED)", reports and reports[0].outcome == Outcome.SKIPPED,
    str(reports[0].outcome) if reports else "no report")
chk("busy worktree -> build NOT run (no clobber)", not called["process"])

# (b) free worktree -> processed normally
called["process"] = False
reports = asyncio.run(loop.run(cfg, [(app, ticket)], A()))
chk("free worktree -> ticket processed", called["process"] and reports[0].outcome == Outcome.MERGED,
    str(reports[0].outcome))

print("\n================ WORKTREE LOCK QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
