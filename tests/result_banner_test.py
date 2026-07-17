"""One-shot result banner QA (EU-31): ship/promote/patrol write their outcome into a dedicated
read-and-clear `last_result`, rendered once on / and then cleared — instead of the sticky shared
`last_msg` (control-bar note) that lingered across unrelated later actions until overwritten.

Acceptance: after a promote/ship finishes, reloading / shows the result banner once; a subsequent
reload (with no new action) no longer shows it."""
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

from orchestrator import server
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp / "app"), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"

client = server.create_app(cfg).test_client()

# --- result banner is one-shot on / (the EU-31 fix) ---
server._state.pop("last_msg", None)
server._state["last_result"] = "Shipped automatixy DEV→MAIN (3 commit(s)) to PRODUCTION."
h1 = client.get("/").get_data(as_text=True)
chk("ship result shows on / after it finishes", "to PRODUCTION" in h1)
h2 = client.get("/").get_data(as_text=True)
chk("result banner is one-shot (cleared on the next reload)", "to PRODUCTION" not in h2)
chk("last_result is consumed (popped) after one view", not server._state.get("last_result"))

# --- a failure result is surfaced too, and is also one-shot ---
server._state["last_result"] = "Deploy failed: not fast-forward"
hf = client.get("/").get_data(as_text=True)
chk("promote failure is surfaced on /", "Deploy failed" in hf)
chk("failure banner clears on the next reload", "Deploy failed" not in client.get("/").get_data(as_text=True))

# --- the result banner does NOT leak the sticky last_msg, and vice-versa ---
server._state.pop("last_result", None)
server._state["last_msg"] = "council failed: boom"   # a non-ship/promote/patrol note
hm = client.get("/").get_data(as_text=True)
chk("sticky last_msg still renders as the control-bar note", "council failed: boom" in hm)
chk("sticky last_msg is NOT consumed by the result banner",
    server._state.get("last_msg") == "council failed: boom")
server._state.pop("last_msg", None)

# --- no result -> no banner, page renders fine ---
server._state.pop("last_result", None)
hn = client.get("/").get_data(as_text=True)
# EU-289 removed "+ New task" from the toolbar — sentinel on the surviving Reports menu.
chk("no result -> page renders without the banner", "Reports" in hn)

# --- deploy-status reports the dedicated result line ---
server._state["last_result"] = "Deployed 2 commit(s) DEV → main — the server self-updates within ~15 min."
import json as _json
d = _json.loads(client.get("/api/deploy-status").get_data(as_text=True))
chk("deploy-status returns the last_result line", "self-updates" in (d.get("msg") or ""))
# deploy-status uses .get (not pop) so the / banner can still consume it once
chk("deploy-status does not consume the result", server._state.get("last_result"))
server._state.pop("last_result", None)

print("\n============ ONE-SHOT RESULT BANNER QA (EU-31) ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
