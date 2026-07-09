"""Deploy progress + fresh badge QA: while an app-ship runs, the control bar shows a live
progress strip that polls /api/deploy-status and reloads when done. EU-205 removed the unit promote
button; EU-206 removed the per-project ship button from the cockpit UI (underlying functionality
remains via ship-preview page and routes)."""
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

from orchestrator import server, sync
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp / "app"), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"

# control over deploy state + git-ahead counts (no real git / network)
sync.can_promote = lambda: True
_ahead = {"unit": 0, "app": 0}
sync.promote_status = lambda c: {"ahead": _ahead["unit"], "subjects": [], "error": None}
sync.app_promote_status = lambda a: {"ahead": _ahead["app"], "base": "DEV", "prot": "MAIN", "error": None}

# reset deploy flags
server._state["promoting"] = False
server._state["shipping"] = False

# --- /api/deploy-status reflects the live flags ---
client = server.create_app(cfg).test_client()
import json as _json
d0 = _json.loads(client.get("/api/deploy-status").get_data(as_text=True))
chk("deploy-status: idle -> not active", d0["active"] is False and d0["kind"] == "")
server._state["promoting"] = True
d1 = _json.loads(client.get("/api/deploy-status").get_data(as_text=True))
chk("deploy-status: promoting -> active/promote", d1["active"] and d1["kind"] == "promote")
server._state["promoting"] = False
server._state["shipping"] = True
d2 = _json.loads(client.get("/api/deploy-status").get_data(as_text=True))
chk("deploy-status: shipping -> active/ship", d2["active"] and d2["kind"] == "ship")
server._state["shipping"] = False

# --- control bar: progress strip appears while deploying, with the poller ---
server._state["promoting"] = True
bar = server._control_bar(cfg, "automatixy", True)
chk("strip shows while promoting", "<div class=deploybar>" in bar)
chk("strip says deploying the unit", "Deploying the unit" in bar)
chk("strip polls the status endpoint", "/api/deploy-status" in bar and "location.reload()" in bar)
server._state["promoting"] = False

server._state["shipping"] = True
bar_s = server._control_bar(cfg, "automatixy", True)
chk("strip shows while shipping", "<div class=deploybar>" in bar_s and "Shipping" in bar_s and "automatixy" in bar_s)
server._state["shipping"] = False

# --- no strip element when idle (the CSS rule is always present; the strip element is not) ---
chk("no strip when idle", "<div class=deploybar>" not in server._control_bar(cfg, "automatixy", True))

# --- 0-ahead: both unit and app ship buttons removed from cockpit UI (EU-205 + EU-206) ---
_ahead["unit"] = 0
_ahead["app"] = 0
idle_bar = server._control_bar(cfg, "automatixy", True)
# EU-205: unit promote button removed; EU-206: app ship button removed
chk("unit promote button removed (EU-205)", "Update unit" not in idle_bar and "unit current" not in idle_bar)
chk("app ship button removed (EU-206)", "Ship automatixy" not in idle_bar and "automatixy shipped" not in idle_bar)

# --- non-zero: neither unit nor app ship buttons render (EU-205 + EU-206) ---
_ahead["unit"] = 3
_ahead["app"] = 13
live_bar = server._control_bar(cfg, "automatixy", True)
# EU-205: unit promote button removed; EU-206: app ship button removed
chk("unit ahead -> no Update unit button (EU-205)", "Update unit" not in live_bar)
chk("app ahead -> no Ship button (EU-206)", "Ship automatixy" not in live_bar)
chk("ahead -> no 'all merged' notes", "unit current" not in live_bar and "automatixy shipped" not in live_bar)

# --- the General's OWN repo as an app (e.g. 'Elite-Unit') has neither unit promote nor app ship
#     button — both removed from cockpit UI (EU-205 + EU-206) ---
cfg_eu = Config(apps=[AppConfig(name="Elite-Unit", repo_path=str(tmp), base_branch="dev",
                                protected_branch="main", backlog_backend="none")],
                audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg_eu.detected_auth = lambda: "test"
_ahead["app"] = 9
eu_bar = server._control_bar(cfg_eu, "Elite-Unit", True)
chk("unit-repo app -> no ship buttons (both removed via EU-205 + EU-206)",
    "Ship Elite-Unit" not in eu_bar and "Elite-Unit shipped" not in eu_bar)

print("\n============ DEPLOY PROGRESS QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
