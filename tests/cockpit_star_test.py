"""EU-27 — finish the "All projects" (*) hardening for the cockpit control bar.

With the project selector on "All projects" the toolbar passes current_app="*". "*" is truthy, so the
old `current_app or cfg.apps[0].name` / `request.form.get("app") or ...` fallbacks never fired and the
literal "*" flowed into cfg.app("*") -> KeyError. This crashed Patrol (swallowed into last_msg) and the
"+ New task" -> Run path ("could not start: '*'") from the DEFAULT all-projects view.

Asserts the complete fix:
  - _control_bar(cfg, "*") bakes a CONCRETE app into every single-app ACTION button (no value="*"),
    while NAV links ("Choose a ticket") keep ?app=* so All-projects actually lists all projects.
  - cfg.app() is NEVER called with "*" while rendering the bar.
  - /api/patrol with app="*" sweeps EVERY app (the "All projects" intent), no KeyError.
  - /api/run with app="*" starts a real run instead of reporting "could not start: '*'".
"""
import sys, types, tempfile, threading, time
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
from orchestrator import sync, patrol as patrol_mod
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
sync.can_promote = lambda: False     # keep the bar off the promote/ship code paths

# Guard: cfg.app() must never be asked for "*".
_real_app = cfg.app
star_calls = []
def _guard_app(name):
    if name == "*":
        star_calls.append(name)
    return _real_app(name)
cfg.app = _guard_app

# --- 1) _control_bar normalizes "*" -> a concrete app; no "*" leaks into any button ---
bar = srv._control_bar(cfg, "*", True)
chk("Choose-a-ticket NAV link preserves All-projects (/tickets?app=* — safe, that route handles *)",
    "/tickets?app=*" in bar, bar[:200])
chk("single-app ACTION buttons emit no app value=\"*\" hidden field", 'value="*"' not in bar)
chk("action buttons fall back to the first concrete app", "automatixy" in bar)
chk("cfg.app() was never called with '*' while rendering (the crash invariant)", not star_calls, str(star_calls))

# --- 2) /api/patrol with app="*" sweeps EVERY app, no KeyError ---
swept = []
async def fake_patrol(c, app_name, do_file=True, audit=None):
    swept.append(app_name)
    return "ok"
patrol_mod.patrol = fake_patrol
srv._state["patrolling"] = False
srv._state["last_msg"] = ""
client = srv.create_app(cfg).test_client()
client.post("/api/patrol", data={"app": "*"})
for _ in range(40):
    if not srv._state.get("patrolling") and swept:
        break
    time.sleep(0.05)
chk("Patrol on 'All projects' sweeps every app", swept == ["automatixy", "Elite-Unit"], str(swept))
chk("Patrol left no error in last_msg", "failed" not in (srv._state.get("last_msg") or ""), srv._state.get("last_msg"))

# --- 3) /api/run with app="*" starts (never 'could not start: *') ---
captured, done = {}, threading.Event()
async def fake_run_loop(rcfg, worklist, audit, stop_event=None):
    done.set()
srv.health.summary = lambda c: {"healthy": True, "checks": []}
def fake_from_text(rcfg, app_name, *a, **k):
    captured["app"] = app_name
    return ["wl"]
srv.intake.from_text = fake_from_text
srv.run_loop = fake_run_loop
srv._state["active"] = False
srv._state["last_msg"] = ""
done.clear()
client.post("/api/run", data={"kind": "task", "text": "do x", "app": "*"})
done.wait(3)
chk("New task -> Run on 'All projects' starts (no 'could not start')",
    "could not start" not in (srv._state.get("last_msg") or ""), srv._state.get("last_msg"))
chk("intake received a concrete app, not '*'", captured.get("app") == "automatixy", str(captured))
for _ in range(40):
    if not srv._state["active"]:
        break
    time.sleep(0.05)

print("\n============ COCKPIT 'ALL PROJECTS' (*) QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
