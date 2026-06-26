"""EU-64 [Ordnance BE] — two-project concurrency, end to end.

The single global run-lock is retired: a run on one project must NOT block a run on another. This
harness exercises that with the two real projects (EU + automatixy) and asserts the three guarantees
the feature must keep true at once:

  A. TWO PROJECTS IN PARALLEL — starting a run on EU and on automatixy concurrently: BOTH proceed
     (neither is refused), and each shows active INDEPENDENTLY (one being live doesn't mark the other).
  B. SAME PROJECT IS STILL IDEMPOTENT — a second run on a project that is already live is refused
     (per-project F7 guard), with the refusal banner landing on THAT project's own run-state only.
  C. THE WORKTREE LOCK STILL GUARDS GIT COLLISIONS — even with per-project run state, two runs that
     share an app worktree can't reset/clean it from under each other: the second DEFERS (SKIPPED),
     never clobbers, and a different app path is an independent lock.

Stubs the Agent SDK, health, intake and ``run_loop`` so it's fast and offline; the run_loop stub
BLOCKS on an Event so both projects genuinely stay live at the same instant (a real race, not a
sequence). Soft ``k/n passed`` tally so ``tests/run_all.py`` (EU-44 gate) judges it honestly.
"""
import sys, types, tempfile, threading, time, asyncio
from pathlib import Path

# --- stub the Agent SDK so importing the orchestrator needs no real models / network. ---
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.server as srv
import orchestrator.loop as loop
from orchestrator import cockpit_state
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket, TicketReport, Outcome

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ==================================================================================================
# A + B — two-project concurrency and per-project idempotency, through the cockpit run routes.
# ==================================================================================================
d = Path(tempfile.mkdtemp()); (d / "audit.jsonl").write_text("")
def _app(name):
    return AppConfig(name=name, repo_path=str(d), base_branch="DEV",
                     protected_branch="MAIN", backlog_backend="none")
cfg = Config(apps=[_app("EU"), _app("automatixy")], audit_path=str(d / "audit.jsonl"),
             use_worktree=False)

cockpit_state.reset_run_state()
cockpit_state.set_max_parallel_runs(0)   # unlimited — this harness is about isolation, not the cap

# run_loop that BLOCKS until released, so both projects are demonstrably live at the same moment.
started = []
started_lock = threading.Lock()
release = threading.Event()
async def fake_run_loop(rcfg, worklist, audit, stop_event=None):
    with started_lock:
        started.append(worklist)
    release.wait(3)
srv.health.summary = lambda c: {"healthy": True, "checks": []}
srv.intake.from_text = lambda rcfg, app, *a, **k: [f"wl:{app}"]
srv.run_loop = fake_run_loop

app = srv.create_app(cfg)

# A) fire EU and automatixy near-simultaneously from separate clients (separate tabs/sessions).
barrier = threading.Barrier(2)
def fire(project, text):
    c = app.test_client()
    barrier.wait()                       # release both POSTs at the same instant
    c.post("/api/run", data={"kind": "task", "text": text, "app": project})
threads = [threading.Thread(target=fire, args=("EU", "eu task")),
           threading.Thread(target=fire, args=("automatixy", "ax task"))]
for t in threads: t.start()
for _ in range(80):
    if len(started) >= 2:
        break
    time.sleep(0.05)
for t in threads: t.join(5)

chk("EU + automatixy both proceed (neither refused)", len(started) == 2, f"started={len(started)}")
chk("EU run is active", cockpit_state.is_active("EU"))
chk("automatixy run is active", cockpit_state.is_active("automatixy"))
chk("both projects active independently (2 active runs)", cockpit_state.active_run_count() == 2,
    str(cockpit_state.active_run_count()))
# isolation: each project's run-state is its own object, not a shared global.
chk("each project has its own run-state object",
    cockpit_state.get_state("EU") is not cockpit_state.get_state("automatixy"))

# B) a SECOND run on the SAME project (EU) is refused while EU is live; automatixy is unaffected.
n_before = len(started)
app.test_client().post("/api/run", data={"kind": "task", "text": "eu again", "app": "EU"})
time.sleep(0.2)
chk("second run on the SAME project (EU) is refused", len(started) == n_before, f"started={len(started)}")
st_eu = cockpit_state.get_state("EU")
chk("refusal banner lands on EU's own state",
    "in progress" in (st_eu.get("last_msg") or "").lower(), st_eu.get("last_msg"))
chk("automatixy's banner untouched by EU's refusal",
    "in progress" not in (cockpit_state.get_state("automatixy").get("last_msg") or "").lower(),
    cockpit_state.get_state("automatixy").get("last_msg"))
chk("automatixy still active after EU's refusal", cockpit_state.is_active("automatixy"))

# let both runs finish and confirm each releases its OWN slot.
release.set()
for _ in range(80):
    if not (cockpit_state.is_active("EU") or cockpit_state.is_active("automatixy")):
        break
    time.sleep(0.05)
chk("both projects release their own slot when done",
    not cockpit_state.is_active("EU") and not cockpit_state.is_active("automatixy"))


# ==================================================================================================
# C — the worktree lock STILL guards git collisions under per-project run state.
# ==================================================================================================
loop._notify = lambda cfg, t: None
wtdir = tempfile.mkdtemp()
loop._worktree_path = lambda a, c: str(Path(wtdir) / a.name)
class DummyGit:
    def ensure_clean(self): pass
loop._make_git = lambda c, a: DummyGit()
called = {"process": False}
async def fake_process(ticket, app, cfg, git, backlog, audit, budget, stop_event=None):
    called["process"] = True
    return TicketReport(ticket.id, Outcome.MERGED, 0, 0.0, app.name)
loop.process_ticket = fake_process
class _A:
    def record(s, *a, **k): pass

wt_app = AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                   protected_branch="MAIN", backlog_backend="none")
wt_cfg = Config(apps=[wt_app], audit_path=str(d / "audit.jsonl"), use_worktree=True)
wt_ticket = Ticket(id="AUTO-1", key="AUTO-1", summary="s", description="d",
                   ephemeral=True, app="automatixy")

# a parallel run already holds automatixy's worktree -> the new run DEFERS (no clobber).
called["process"] = False
with loop._worktree_lock(str(Path(wtdir) / "automatixy")):
    reports = asyncio.run(loop.run(wt_cfg, [(wt_app, wt_ticket)], _A()))
    # a DIFFERENT app path is an independent lock (per-app, not global).
    indep = True
    try:
        with loop._worktree_lock(str(Path(wtdir) / "EU")):
            pass
    except loop.WorktreeBusy:
        indep = False
chk("busy worktree -> ticket DEFERRED (SKIPPED, not clobbered)",
    bool(reports) and reports[0].outcome == Outcome.SKIPPED,
    str(reports[0].outcome) if reports else "no report")
chk("busy worktree -> build NOT run (active run's work preserved)", not called["process"])
chk("a different app's worktree is an independent lock", indep)

# once the lock is released, the same app's worktree is claimable and the ticket processes normally.
called["process"] = False
reports = asyncio.run(loop.run(wt_cfg, [(wt_app, wt_ticket)], _A()))
chk("free worktree -> ticket processed normally",
    called["process"] and bool(reports) and reports[0].outcome == Outcome.MERGED,
    str(reports[0].outcome) if reports else "no report")


print("\n================ EU-64 TWO-PROJECT CONCURRENCY QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
