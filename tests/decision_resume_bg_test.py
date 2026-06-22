"""F12 regression: a resumed decision builds on a BACKGROUND thread, so the Telegram
poll thread is never blocked while a resumed ticket builds (/unblock and other replies
keep being processed). Before the fix, handle_reply ran asyncio.run(run_loop(...))
synchronously in the poll thread, freezing polling for minutes."""
import sys, types, tempfile, threading, time
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

# --- fake the loop module so we never import the real (heavy) build loop ---
poll_thread_id = threading.get_ident()
started = threading.Event()
release = threading.Event()
finished = threading.Event()
run_thread = {}

fake_loop = types.ModuleType("orchestrator.loop")
async def _run(cfg, worklist, audit):
    run_thread["id"] = threading.get_ident()
    started.set()
    release.wait(5)        # simulate a long-running resumed build
    finished.set()
fake_loop.run = _run
sys.modules["orchestrator.loop"] = fake_loop

# silence notifications / audit
decisions.notify = types.SimpleNamespace(send=lambda *a, **k: None)
audit = types.SimpleNamespace(record=lambda *a, **k: None)

tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="jira",
                             backlog={"base_url": "https://x.atlassian.net", "project_key": "AUTO"})],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)

# seed a pending decision, then answer it
decisions.add(cfg, Ticket(id="AUTO-1", key="AUTO-1", summary="do thing", description="d",
                          acceptance_criteria=["x"], app="automatixy", ephemeral=True),
              "automatixy", "pick a date")

t0 = time.time()
ok = decisions.handle_reply(cfg, audit, "AUTO-1: use DD/MM")
elapsed = time.time() - t0

chk("handle_reply returns True (ticket resumed)", ok is True)
chk("handle_reply returns immediately, never blocking the poll thread", elapsed < 1.0, f"{elapsed:.2f}s")
chk("resumed build actually started", started.wait(2))
chk("resumed build runs OFF the poll thread", run_thread.get("id") not in (None, poll_thread_id))
chk("poll thread is free while the build is still running", not finished.is_set())

# the pending decision was consumed (so a follow-up reply is a fresh free-text msg, not a re-resume)
chk("pending decision consumed on resume", decisions.load(cfg) == [])

release.set()
chk("background build completes", finished.wait(2))

print("\n============ F12 DECISION-RESUME BG QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
