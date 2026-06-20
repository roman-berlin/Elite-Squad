"""Deploy progress + fresh badge QA: while a unit-promote / app-ship runs, the control bar shows a live
progress strip that polls /api/deploy-status and reloads when done; at 0-ahead both buttons show an
explicit 'all merged' status instead of a stale number."""
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
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
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

# --- 0-ahead: explicit 'all merged' status, not a stale number ---
_ahead["unit"] = 0
_ahead["app"] = 0
idle_bar = server._control_bar(cfg, "automatixy", True)
chk("unit at 0 -> 'unit current' (not a button)", "unit current" in idle_bar and "Update unit" not in idle_bar)
chk("app at 0 -> 'automatixy shipped' (not a stale ship button)",
    "automatixy shipped" in idle_bar and "Ship automatixy" not in idle_bar)

# --- non-zero: the buttons show the live count ---
_ahead["unit"] = 3
_ahead["app"] = 13
live_bar = server._control_bar(cfg, "automatixy", True)
chk("unit ahead -> Update unit button with the count", "Update unit" in live_bar and ">3<" in live_bar)
chk("app ahead -> Ship button with the count", "Ship automatixy" in live_bar and ">13<" in live_bar)
chk("ahead -> no 'all merged' note", "unit current" not in live_bar and "automatixy shipped" not in live_bar)

print("\n============ DEPLOY PROGRESS QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
