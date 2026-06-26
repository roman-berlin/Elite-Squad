"""EU-63 [Test Engineer] — per-tab SSE board is READ-ONLY: a tab's board poll must never steal the
active tab from another tab.

The ticket's "Per-tab SSE board — each tab streams only its project's board" hinges on a subtle split
in server.py: navigation/actions go through ``_scope`` (which OPENS/FOCUSES a tab and flips the
session's active project), but the board + SSE stream go through ``_board_project`` — a deliberately
READ-ONLY resolver that renders the requested tab's project WITHOUT mutating which tab is active.

Why it matters: every open tab runs its own ``EventSource(/api/stream?app=<its project>)`` polling in
the background. If the board route mutated the active tab (e.g. if someone "simplified" it to reuse
``_scope``), tab B's 1.5s background poll would silently yank the active project away from the tab the
user is actually looking at — the tabs would fight and couldn't stream in parallel. eu63_tab_routes
proves the board RENDERS the right project; this pins the non-mutation invariant that lets the streams
coexist. It is the regression test for reusing ``_scope`` in the board/stream path.
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
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")

import orchestrator.server as srv
from orchestrator import cockpit_state
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

d = Path(tempfile.mkdtemp()); (d / "audit.jsonl").write_text("")
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none"),
                   AppConfig(name="Elite-Unit", repo_path=str(d / "eu"), base_branch="dev",
                             protected_branch="main", backlog_backend="none")],
             audit_path=str(d / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"
srv.health.summary = lambda c: {"healthy": True, "checks": []}

# cfg.app('*') must never be reached from the board/stream path either.
_real_app = cfg.app
star_calls = []
cfg.app = lambda name: (star_calls.append(name) if name == "*" else None) or _real_app(name)

client = srv.create_app(cfg).test_client()
SID = "sess-board"
client.set_cookie("eu_cockpit_sid", SID)   # pin a known session so we can inspect its workspace

# Seed a two-tab workspace whose ACTIVE tab is automatixy (the tab the "user" is looking at).
cockpit_state.reset_workspaces()
ws = cockpit_state.workspace_for(SID)
ws.add_tab("automatixy")
ws.add_tab("Elite-Unit")
ws.set_active("automatixy")
chk("precondition: active tab is automatixy", ws.active == "automatixy")

# --- 1) the OTHER tab's background board poll renders ITS project but does NOT flip the active tab ---
b_eu = client.get("/api/board?app=Elite-Unit").get_data(as_text=True)
chk("board?app=Elite-Unit renders the Elite-Unit board", "Elite-Unit" in b_eu)
chk("background board poll of another tab does NOT steal the active tab",
    cockpit_state.workspace_for(SID).active == "automatixy", cockpit_state.workspace_for(SID).active)

# --- 2) repeated cross-tab polls (as the real SSE stream does) keep the active tab put ---
for _ in range(3):
    client.get("/api/board?app=Elite-Unit")
    client.get("/api/board?app=automatixy")
chk("active tab survives a burst of interleaved per-tab polls",
    cockpit_state.workspace_for(SID).active == "automatixy", cockpit_state.workspace_for(SID).active)
chk("no new tab opened by board polls (still exactly the two seeded tabs)",
    cockpit_state.workspace_for(SID).projects() == ["automatixy", "Elite-Unit"],
    str(cockpit_state.workspace_for(SID).projects()))

# --- 3) the retired '*' sentinel on the board falls back to a concrete project, never cfg.app('*') ---
b_star = client.get("/api/board?app=*").get_data(as_text=True)
chk("board?app=* never renders the retired 'all projects' board", "all projects" not in b_star)
chk("board?app=* falls back to the active concrete project (automatixy)", "automatixy" in b_star)
chk("cfg.app('*') never reached from the board path", not star_calls, str(star_calls))

# --- 4) contrast: navigation (_scope) DOES flip active — proving the two resolvers differ on purpose ---
client.get("/tickets?app=Elite-Unit")
chk("navigation (not board) flips the active tab to Elite-Unit",
    cockpit_state.workspace_for(SID).active == "Elite-Unit", cockpit_state.workspace_for(SID).active)
# ...and a board poll of automatixy still must not flip it back.
client.get("/api/board?app=automatixy")
chk("board poll after nav still does not mutate the active tab",
    cockpit_state.workspace_for(SID).active == "Elite-Unit", cockpit_state.workspace_for(SID).active)

print("\n============ EU-63 BOARD READ-ONLY (PER-TAB SSE) QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
