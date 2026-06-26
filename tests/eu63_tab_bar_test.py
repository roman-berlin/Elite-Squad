"""EU-63 [Frontend] — cockpit_views tab bar + mutually-exclusive add-tab picker.

The cockpit is one-project-per-tab; the retired "All projects"/* switcher is gone. This covers the
VIEW slice (engineer-3): ``_control_bar`` renders a tab strip with one tab per OPEN project (the
active one highlighted), and a '+' add-tab picker that offers ONLY projects not already open in a
tab (mutual exclusion). Tab clicks + picker entries are per-tab ``/?app=<project>`` links, and the
nav links no longer carry the '*' all-projects sentinel.
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

from orchestrator import cockpit_views as V
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

# --- 1) outside a request: synthetic single tab for current_app, no '*' switcher ---
bar = V._control_bar(cfg, "automatixy", True)
chk("renders the tab strip", "class=tabstrip" in bar, bar[:60])
chk("active project shows a highlighted tab", "class='ptab on'" in bar)
chk("tab links to its per-tab route", "/?app=automatixy" in bar)
chk("no '*' all-projects nav link remains", "app=*" not in bar and "app=%2A" not in bar)
chk("'Choose a ticket' targets the concrete project", "/tickets?app=automatixy" in bar)
# the only-open project is automatixy, so the picker must OFFER Elite-Unit (a new-tab link)...
chk("add-tab picker offers a not-open project", "/?app=Elite-Unit" in bar)
# ...and must NOT offer the already-open project as an openable picker link.
chk("add-tab picker greys out the already-open project", "automatixy &middot; open" in bar)

# --- 2) with a live workspace (two open tabs), the bar reflects the open set + active ---
cockpit_state.reset_workspaces()
ws = cockpit_state.workspace_for("sid-A")
ws.add_tab("automatixy")
ws.add_tab("Elite-Unit")          # now active
# Use a real flask request context so _workspace_tabs reads the session cookie path.
import orchestrator.server as srv
srv.health.summary = lambda c: {"healthy": True, "checks": []}
app = srv.create_app(cfg)
with app.test_request_context("/", headers={"Cookie": "eu_cockpit_sid=sid-A"}):
    bar2 = V._control_bar(cfg, "Elite-Unit", True)
chk("both open tabs are rendered", "/?app=automatixy" in bar2 and "/?app=Elite-Unit" in bar2)
chk("the active tab (Elite-Unit) is highlighted", "class='ptab on' href='/?app=Elite-Unit'" in bar2)
# every project is open -> picker offers none, shows the all-open notice
chk("picker shows 'all open' when nothing is openable", "Every project is already open" in bar2)

# --- 3) called without a project context: '*' sentinel must never appear in the output ---
bar_noproj = V._control_bar(cfg, None, True)
chk("no app=* emitted when current_app is None", "app=*" not in bar_noproj and "app=%2A" not in bar_noproj)

print("\n============ EU-63 TAB-BAR (FRONTEND) QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
