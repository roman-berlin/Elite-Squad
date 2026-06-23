"""Live-run status QA: a build started outside the cockpit (answer box / /unblock / autopilot) is still
recognized as LIVE from fresh audit activity, so the phase bar lights up instead of showing 'last run ·
interrupted'. Plus the taller/resizable terminal + collapsible Activity panel render."""
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
board = warroom.render_board(cfg, "automatixy", {}, log_lines=[])   # empty state = cockpit didn't start it
chk("board: in-flight run shows '● running'", "● running" in board, "expected running status")
chk("board: the current phase is highlighted (ph now)", 'class="ph now"' in board)
chk("board: it is NOT shown as 'last run'", "last run" not in board)

# a genuinely finished run still reads as the last run (no false 'running')
write({"event": "ticket_start", "ticket_id": "AUTO-9", "app": "automatixy", "ts": stamp(300)},
      {"event": "merged", "ticket_id": "AUTO-9", "app": "automatixy", "ts": stamp(290)})
board2 = warroom.render_board(cfg, "automatixy", {}, log_lines=[])
chk("board: a finished run shows 'last run', not running", "last run" in board2 and "● running" not in board2)

# --- the Activity panel is collapsible + the terminal is taller/resizable ---
chk("board: Activity is a collapsible panel", "id=actpanel" in board and 'class="panel collapse"' in board)
page = warroom.render_page(cfg, "automatixy", {}, "", {"healthy": True, "checks": []}, log_lines=[])
chk("page: terminal is taller + resizable", "height:380px" in page and "resize:vertical" in page)
chk("page: collapse state persists (applyUi + localStorage)", "applyUi" in page and "ui.open." in page)
chk("page: Tickets-to-work panel persists collapse + resize (blpanel/blbox)",
    "blpanel" in page and "id=blbox" in page)
chk("page: Live feed renders before Tickets-to-work before Activity",
    0 < board.find("Live feed") < board.find("id=blpanel") < board.find("id=actpanel"))
chk("page: collapsible panels excluded from menu auto-close", 'classList.contains("collapse")' in page)
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
