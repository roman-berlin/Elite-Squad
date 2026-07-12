"""EU-254: CSRF/Origin/Host guard on the ~30 state-changing POST endpoints.

Audit finding: form-urlencoded/multipart POSTs are CORS "simple requests" (no preflight), so a
malicious page open in Roman's browser — or a DNS-rebound attacker domain resolving to
127.0.0.1:8787 — could drive the unit (run, model switch, stop, approve) with no same-origin
check at all. This harness pins the ``before_request`` guard added in ``server.create_app``:

  1. A foreign ``Origin`` on a state-changing POST -> 403, handler never runs.
  2. A foreign ``Referer`` (no Origin) on a state-changing POST -> 403, handler never runs.
  3. A mismatched ``Host`` (DNS-rebinding) with NO Origin/Referer at all -> 403.
  4. A same-origin POST (matching Origin/Referer + Host) is accepted — reaches the handler.
  5. GET routes are completely unaffected by the guard, regardless of Origin/Host.

Stubs the Agent SDK / requests so importing the orchestrator needs no network / real models. Soft
``k/n passed`` tally so ``tests/run_all.py`` (the EU-44 gate) judges it honestly.
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
req.Session = lambda: types.SimpleNamespace(auth=None,
                                            headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")

import orchestrator.server as srv
from orchestrator import cockpit_state
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


d = Path(tempfile.mkdtemp()); (d / "audit.jsonl").write_text("")
app_cfg = AppConfig(name="alpha", repo_path=str(d), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")
cfg = Config(apps=[app_cfg], audit_path=str(d / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"

srv.health.summary = lambda c: {"healthy": True, "checks": []}
cockpit_state.reset_run_state()

flask_app = srv.create_app(cfg)
client = flask_app.test_client()

STATE_CHANGING = [
    ("/api/model", {"backend": "opus"}),
    ("/api/run", {"kind": "task", "text": "hello", "app": "alpha"}),
    ("/api/stop-run", {"app": "alpha"}),
    ("/api/approve", {"kind": "code"}),
]

FOREIGN_ORIGIN = "http://evil.example"
FOREIGN_REFERER = "http://evil.example/pwn"
GOOD_ORIGIN = "http://127.0.0.1:8787"
GOOD_REFERER = "http://127.0.0.1:8787/"
GOOD_HOST = "127.0.0.1:8787"
BAD_HOST = "attacker.tld"

# ==================================================================================================
# 1 — a foreign Origin on a state-changing POST is rejected with 403; handler never runs.
# ==================================================================================================
for path, data in STATE_CHANGING:
    cockpit_state.reset_run_state()
    resp = client.post(path, data=data, headers={"Origin": FOREIGN_ORIGIN, "Host": GOOD_HOST})
    chk(f"foreign Origin on {path} -> 403", resp.status_code == 403, resp.status_code)

# a foreign-Origin /api/run must NOT actually claim the run slot / start anything.
cockpit_state.reset_run_state()
client.post("/api/run", data={"kind": "task", "text": "x", "app": "alpha"},
            headers={"Origin": FOREIGN_ORIGIN, "Host": GOOD_HOST})
chk("foreign-Origin /api/run does not start a run", not cockpit_state.is_active("alpha"))

# ==================================================================================================
# 2 — a foreign Referer (no Origin) is rejected with 403; handler never runs.
# ==================================================================================================
for path, data in STATE_CHANGING:
    cockpit_state.reset_run_state()
    resp = client.post(path, data=data, headers={"Referer": FOREIGN_REFERER, "Host": GOOD_HOST})
    chk(f"foreign Referer (no Origin) on {path} -> 403", resp.status_code == 403, resp.status_code)

cockpit_state.reset_run_state()
client.post("/api/run", data={"kind": "task", "text": "x", "app": "alpha"},
            headers={"Referer": FOREIGN_REFERER, "Host": GOOD_HOST})
chk("foreign-Referer /api/run does not start a run", not cockpit_state.is_active("alpha"))

# ==================================================================================================
# 3 — a mismatched Host (DNS-rebinding) is rejected with 403 even with NO Origin/Referer at all.
# ==================================================================================================
for path, data in STATE_CHANGING:
    cockpit_state.reset_run_state()
    resp = client.post(path, data=data, headers={"Host": BAD_HOST})
    chk(f"mismatched Host (no Origin/Referer) on {path} -> 403", resp.status_code == 403, resp.status_code)

# ==================================================================================================
# 4 — a same-origin cockpit POST (Origin/Referer + Host all matching) is accepted, not 403.
# ==================================================================================================
for path, data in STATE_CHANGING:
    cockpit_state.reset_run_state()
    resp = client.post(path, data=data, headers={"Origin": GOOD_ORIGIN, "Host": GOOD_HOST})
    chk(f"same-origin Origin POST on {path} is accepted", resp.status_code != 403, resp.status_code)

for path, data in STATE_CHANGING:
    cockpit_state.reset_run_state()
    resp = client.post(path, data=data, headers={"Referer": GOOD_REFERER, "Host": GOOD_HOST})
    chk(f"same-origin Referer POST on {path} is accepted", resp.status_code != 403, resp.status_code)

# a same-origin /api/run actually reaches the handler and claims the run slot.
cockpit_state.reset_run_state()
started = []
async def fake_run_loop(rcfg, worklist, audit, stop_event=None):
    started.append(worklist)
    import time as _t
    _t.sleep(0.2)
srv.run_loop = fake_run_loop
srv.intake.from_text = lambda rcfg, app, *a, **k: [f"wl:{app}"]
client.post("/api/run", data={"kind": "task", "text": "x", "app": "alpha"},
            headers={"Origin": GOOD_ORIGIN, "Host": GOOD_HOST})
import time
for _ in range(60):
    if started:
        break
    time.sleep(0.05)
chk("same-origin /api/run actually starts a run", len(started) == 1, started)
for _ in range(60):
    if not cockpit_state.is_active("alpha"):
        break
    time.sleep(0.05)

# ==================================================================================================
# 5 — GET routes are completely unaffected by the guard, regardless of Origin/Host.
# ==================================================================================================
r = client.get("/", headers={"Origin": FOREIGN_ORIGIN, "Host": BAD_HOST})
chk("GET / with foreign Origin+Host is unaffected (not 403)", r.status_code != 403, r.status_code)
r = client.get("/api/health", headers={"Origin": FOREIGN_ORIGIN, "Host": BAD_HOST})
chk("GET /api/health with foreign Origin+Host is unaffected", r.status_code == 200, r.status_code)
r = client.get("/api/board", headers={"Origin": FOREIGN_ORIGIN, "Host": BAD_HOST})
chk("GET /api/board with foreign Origin+Host is unaffected", r.status_code != 403, r.status_code)


print("\n============ EU-254 CSRF/ORIGIN/HOST GUARD QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
