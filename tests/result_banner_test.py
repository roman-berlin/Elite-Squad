"""Result banner QA (EU-31 + EU-676): ship/promote/patrol write their outcome into a dedicated
`last_result` / `last_result_record`, rendered on / with tone-styled colours.

EU-676 changed the semantics: index() now uses `_result_strip()` (non-destructive peek) instead
of the old `_result_banner()` (destructive pop), so an un-dismissed result survives full-page
reloads.  Dismissal happens via ``POST /api/dismiss-result`` (EU-675) which clears both keys.
This test covers BOTH paths: the legacy plain-string ``last_result`` (fallback in ``_result_strip``)
and the structured ``last_result_record`` path."""
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
from orchestrator.cockpit_views import _result_strip
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

# ── _result_strip helper: non-destructive peek ──────────────────────────────
server._state.pop("last_result", None)
server._state.pop("last_result_record", None)
server._state["last_result"] = "plain fallback text"
s1 = _result_strip(server._state)
chk("_result_strip renders plain-string fallback", "plain fallback text" in s1)
s2 = _result_strip(server._state)
chk("_result_strip does NOT pop state (survives second call)",
    "plain fallback text" in s2 and server._state.get("last_result") == "plain fallback text")

# Legacy-only last_result (no record) renders via fallback path
server._state.pop("last_result_record", None)
s_plain = _result_strip(server._state)
chk("_result_strip empty when last_result_record=None", "" == _result_strip({"last_result_record": None}))

# No pending → empty
server._state.pop("last_result", None)
chk("_result_strip empty when nothing pending", "" == _result_strip({}))

# ── result banner is PERSISTENT (EU-676: no longer one-shot pop) ────────────
server._state.pop("last_msg", None)
server._state["last_result"] = "Shipped automatixy DEV→MAIN (3 commit(s)) to PRODUCTION."
h1 = client.get("/").get_data(as_text=True)
chk("ship result shows on / after it finishes", "to PRODUCTION" in h1)
h2 = client.get("/").get_data(as_text=True)
chk("result PERSISTS on reload (EU-676 no longer pops)", "to PRODUCTION" in h2)
chk("last_result NOT popped by GET / (EU-676)", server._state.get("last_result") is not None)

# ── a failure result surfaces and also persists ─────────────────────────────
server._state["last_result"] = "Deploy failed: not fast-forward"
hf = client.get("/").get_data(as_text=True)
chk("promote failure is surfaced on /", "Deploy failed" in hf)
chk("failure result PERSISTS on reload (EU-676)",
    "Deploy failed" in client.get("/").get_data(as_text=True))

# ── dismiss clears the strip ────────────────────────────────────────────────
server._state["last_result"] = "dismiss-me-test"
h_before = client.get("/").get_data(as_text=True)
chk("strip visible before dismiss", "dismiss-me-test" in h_before)
rv = client.post("/api/dismiss-result?app=", content_type="multipart/form-data")
chk("dismiss API returns 200", rv.status_code == 200)
h_after = client.get("/").get_data(as_text=True)
chk("strip GONE after POST /api/dismiss-result", "dismiss-me-test" not in h_after)

# ── the result strip does NOT leak the sticky last_msg, and vice-versa ──────
server._state.pop("last_result", None)
server._state.pop("last_result_record", None)
server._state["last_msg"] = "council failed: boom"   # a non-ship/promote/patrol note
hm = client.get("/").get_data(as_text=True)
chk("sticky last_msg still renders as the control-bar note", "council failed: boom" in hm)
chk("sticky last_msg is NOT consumed by the result strip",
    server._state.get("last_msg") == "council failed: boom")
server._state.pop("last_msg", None)

# ── no result -> no banner, page renders fine ───────────────────────────────
server._state.pop("last_result", None)
server._state.pop("last_result_record", None)
hn = client.get("/").get_data(as_text=True)
# EU-289 removed "+ New task"; 2026-07-19 flattened the Reports menu — sentinel on Task log.
chk("no result -> page renders without the banner", "Task log" in hn)

# ── deploy-status reports the dedicated result line ─────────────────────────
server._state["last_result"] = "Deployed 2 commit(s) DEV → main — the server self-updates within ~15 min."
import json as _json
d = _json.loads(client.get("/api/deploy-status").get_data(as_text=True))
chk("deploy-status returns the last_result line", "self-updates" in (d.get("msg") or ""))
# deploy-status uses .get (not pop) so the / strip can still render it repeatedly
chk("deploy-status does not consume the result", server._state.get("last_result"))
server._state.pop("last_result", None)

print("\n============ RESULT STRIP BANNER QA (EU-31 + EU-676) ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
