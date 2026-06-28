"""Live-run status QA: a build started outside the cockpit (answer box / /unblock / autopilot) is still
recognized as LIVE from fresh audit activity, so the phase bar lights up instead of showing 'last run ·
interrupted'.

EU-106 update: the Live Feed, Activity, and Tickets-to-work panels have been removed from render_board.
Tests that asserted their presence are updated to assert their absence."""
import sys, types, tempfile, json
from datetime import datetime, timedelta
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

from orchestrator import warroom, dashboard as D
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
audit = tmp / "audit.jsonl"
def stamp(secs_ago=0):
    return (datetime.now() - timedelta(seconds=secs_ago)).strftime("%Y-%m-%dT%H:%M:%S")
def write(*events):
    audit.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    # This test rewrites the SAME audit path repeatedly within one filesystem mtime tick. The audit cache
    # keys on (size, mtime_ns); on coarse-mtime filesystems (CI containers, mounted/overlay FS) two
    # same-size rewrites collide and the cache serves stale data — so bust it here. Real audits are
    # append-only (size grows every write), so this collision can't happen in production; we're isolating
    # the render logic under test from the cache, which has its own coverage in cockpit_cache_test.py.
    D._audit_cache.clear()
    D._tasks_cache.clear()

cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(audit), use_worktree=False)

# --- _run_in_flight ---
write({"event": "ticket_start", "ticket_id": "AUTO-14", "app": "automatixy", "ts": stamp(5)})
chk("in-flight: recent start, no terminal -> live", warroom._run_in_flight(cfg, D.load_tasks(audit), "automatixy"))

write({"event": "ticket_start", "ticket_id": "AUTO-14", "app": "automatixy", "ts": stamp(120)},
      {"event": "merged", "ticket_id": "AUTO-14", "app": "automatixy", "ts": stamp(110)})
chk("in-flight: a finished run is NOT live", not warroom._run_in_flight(cfg, D.load_tasks(audit), "automatixy"))

write({"event": "ticket_start", "ticket_id": "AUTO-14", "app": "automatixy", "ts": stamp(400)})
chk("in-flight: stale (no activity for >150s) is NOT live", not warroom._run_in_flight(cfg, D.load_tasks(audit), "automatixy"))

chk("in-flight: no runs -> not live", not warroom._run_in_flight(cfg, [], "automatixy"))

# --- render_board: a background build (state.active False) renders LIVE with the phase bar lit ---
write({"event": "ticket_start", "ticket_id": "AUTO-14", "app": "automatixy", "ts": stamp(4)})
board = warroom.render_board(cfg, "automatixy", {})   # empty state = cockpit didn't start it
chk("board: in-flight run shows hero-merged live header (runlive + hgdot)",
    "runlive" in board and "hgdot" in board, "expected runlive/hgdot in board HTML")
chk("board: the current phase is highlighted (ph now)", 'class="ph now"' in board)
chk("board: it is NOT shown as 'last run'", "last run" not in board)

# a genuinely finished run still reads as the last run (no false 'running')
write({"event": "ticket_start", "ticket_id": "AUTO-9", "app": "automatixy", "ts": stamp(300)},
      {"event": "merged", "ticket_id": "AUTO-9", "app": "automatixy", "ts": stamp(290)})
board2 = warroom.render_board(cfg, "automatixy", {})
chk("board: a finished run shows 'last run', not running",
    "last run" in board2 and "runlive" not in board2)

# --- EU-106: Live Feed, Activity, and Tickets-to-work panels are removed ---
page = warroom.render_page(cfg, "automatixy", {}, "", {"healthy": True, "checks": []})
chk("EU-106 board: Live feed panel removed", "Live feed" not in board)
chk("EU-106 board: Activity panel removed", "id=actpanel" not in board)
chk("EU-106 board: Tickets-to-work panel removed", "id=blpanel" not in board)
chk("EU-106 board: _liveness chip in Active run panel header (not live feed)",
    "Active run" in board)
chk("page: scroll position preserved across the 2s refresh (no jump while reading)",
    "_atBottom" in page and "keep[id]" in page)

print("\n============== LIVE-RUN STATUS QA ==============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
