"""EU-63 [Ordnance BE] — server.py tab shell: every request is scoped to ONE concrete project.

The cockpit is a tabbed, one-project-per-tab workspace; the retired "All projects"/* context is gone.
This asserts the ROUTE-level wiring that engineer-2 owns (the per-tab workspace model itself is covered
by eu63_tab_state_test):
  - a per-browser session cookie is minted so the open tabs/active project survive across requests;
  - navigating to a concrete ?app=<proj> FOCUSES that tab, and a later app-less action (patrol/run)
    scopes to that active tab — proving request-to-tab resolution, not a global switch;
  - the retired '*' sentinel never reaches cfg.app('*'); it falls back to the active tab / first app;
  - the board (and therefore the SSE #board stream) is per-tab: /api/board?app=<proj> renders ONLY that
    project's board, and '*'/empty resolves to a concrete project (never the "all projects" board).
"""
import sys, types, tempfile, time, threading
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
from orchestrator import sync, patrol as patrol_mod, intake
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
sync.can_promote = lambda: False
srv.health.summary = lambda c: {"healthy": True, "checks": []}
intake.from_drain = lambda c, n, l: []     # no backlog fetch in these route tests

# cfg.app('*') must never be reached — the crash invariant the all-projects removal must preserve.
_real_app = cfg.app
star_calls = []
def _guard_app(name):
    if name == "*":
        star_calls.append(name)
    return _real_app(name)
cfg.app = _guard_app

from orchestrator import projects
projects.recents = lambda c: ["Elite-Unit"]

client = srv.create_app(cfg).test_client()

# --- 1) a session cookie is minted on first contact, and ALL configured apps are pre-opened ---
r = client.get("/")
set_cookie = r.headers.get("Set-Cookie") or ""
chk("first request mints the per-browser session cookie", "eu_cockpit_sid=" in set_cookie, set_cookie[:80])

html_text = r.get_data(as_text=True)
chk("rendered tab list contains automatixy", ">automatixy</a>" in html_text)
chk("rendered tab list contains Elite-Unit", ">Elite-Unit</a>" in html_text)
chk("last-active tab from recents is restored as active", "class='ptab on' href='/?app=Elite-Unit'" in html_text)

# --- 2) focusing a concrete tab, then an app-less action scopes to that ACTIVE tab ---
swept = []
async def fake_patrol(c, app_name, do_file=True, audit=None):
    swept.append(app_name); return "ok"
patrol_mod.patrol = fake_patrol
# 2026-07-19: /api/qa (the merged Patrol+Ship-review action) also runs the ship phase — stub it
# so this scoping harness stays hermetic.
from orchestrator import council as _council
async def _fake_ship(c, app_name, audit=None):
    return "GO"
_council.ship_review = _fake_ship

client.get("/tickets?app=Elite-Unit")          # focus the Elite-Unit tab for this session
srv._state["qa"] = False
client.post("/api/qa")                          # NO app field -> must use the active tab
for _ in range(40):
    if not srv._state.get("qa") and swept:
        break
    time.sleep(0.05)
chk("app-less QA scopes to the active tab (Elite-Unit)", swept == ["Elite-Unit"], str(swept))

# switching the active tab via a concrete ?app changes what later app-less actions target
swept.clear()
client.get("/tickets?app=automatixy")
srv._state["qa"] = False
client.post("/api/qa")
for _ in range(40):
    if not srv._state.get("qa") and swept:
        break
    time.sleep(0.05)
chk("focusing another tab re-scopes the next action (automatixy)", swept == ["automatixy"], str(swept))

# --- 3) '*' never reaches cfg.app('*'); a run with app='*' starts on a concrete app ---
captured, done = {}, threading.Event()
async def fake_run_loop(rcfg, worklist, audit, stop_event=None):
    done.set()
def fake_from_text(rcfg, app_name, *a, **k):
    captured["app"] = app_name; return ["wl"]
intake.from_text = fake_from_text
srv.run_loop = fake_run_loop
srv._state["active"] = False
client.post("/api/run", data={"kind": "task", "text": "do x", "app": "*"})
done.wait(3)
chk("run with app='*' resolves to a concrete app (no cfg.app('*'))", captured.get("app") in {"automatixy", "Elite-Unit"}, str(captured))
chk("cfg.app('*') was never called across the routes", not star_calls, str(star_calls))
for _ in range(40):
    if not srv._state["active"]:
        break
    time.sleep(0.05)

# --- 4) the board is per-tab: /api/board?app=<proj> renders ONLY that project, '*' falls back concrete ---
b_auto = client.get("/api/board?app=automatixy").get_data(as_text=True)
chk("board?app=<proj> is scoped to that project's board", "automatixy" in b_auto and "all projects" not in b_auto)
b_star = client.get("/api/board?app=*").get_data(as_text=True)
chk("board?app=* never renders the retired 'all projects' board", "all projects" not in b_star)

print("\n============ EU-63 TAB-SHELL ROUTES QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
