"""Ship-review UX QA: clicking 'Ship review' on 'All projects' must NOT crash on cfg.app('*') — it
resolves to a real shippable product (not the unit's own repo). And landing on /council now shows a
clear 'Ship-review in session' indicator while it runs, instead of a bare council page with no sign
anything is happening."""
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

tmp = Path(tempfile.mkdtemp())   # _repo_root(cfg) == parent of audit.jsonl == tmp
# Elite-Unit's repo IS the unit repo (== tmp); automatixy is a separate product.
appEU = AppConfig(name="Elite-Unit", repo_path=str(tmp), base_branch="dev", protected_branch="main",
                  backlog_backend="jira", backlog={"project_key": "EU"})
appAUTO = AppConfig(name="automatixy", repo_path=str(tmp / "auto"), base_branch="DEV", protected_branch="MAIN",
                    backlog_backend="jira", backlog={"project_key": "AUTO"})
cfg = Config(apps=[appEU, appAUTO], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"

# --- _first_shippable skips the unit's own repo and picks the real product ---
chk("ship-review resolves 'All projects' to the real product (not the unit repo)",
    server._first_shippable(cfg) == "automatixy")
cfg_unit_only = Config(apps=[appEU], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
chk("falls back to the first app when there's no separate product",
    server._first_shippable(cfg_unit_only) == "Elite-Unit")

# --- /council shows the ship-review indicator while it runs (not a bare council page) ---
sync.can_promote = lambda: False
client = server.create_app(cfg).test_client()
server._state["councilling"] = False
server._state["shipreview"] = True
body = client.get("/council").get_data(as_text=True)
chk("running ship-review shows an in-session indicator on /council", "Ship-review in session" in body)
chk("the 'Hold a council' action is hidden while a ship-review runs", "Hold a council now" not in body)
server._state["shipreview"] = False
idle = client.get("/council").get_data(as_text=True)
chk("idle /council has no ship-review indicator", "Ship-review in session" not in idle)

# --- EU-30: no shippable product configured -> guard, no empty "running for " banner ---
cfg_empty = Config(apps=[], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg_empty.detected_auth = lambda: "test"
empty_client = server.create_app(cfg_empty).test_client()
server._state["shipreview"] = False
server._state.pop("last_msg", None)
resp = empty_client.post("/api/ship-review", data={"app": "*"})
chk("POST ship-review w/o product redirects to /council", resp.status_code in (301, 302)
    and "/council" in resp.headers.get("Location", ""))
chk("no shippable product => _state['shipreview'] not set", not server._state.get("shipreview"))
chk("last_msg explains why ship-review didn't run",
    "No shippable product configured" in server._state.get("last_msg", ""))
chk("no empty 'running for ' banner appears",
    "running for " not in server._state.get("last_msg", ""))

print("\n============ SHIP-REVIEW UX QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
