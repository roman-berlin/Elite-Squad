"""EU-582 — QA failure state rendering + retry button.

Covers: patrol failure → "QA failed during Phase 1" with error text + Retry form;
        ship-review failure → "QA failed during Phase 2" + partial findings preserved + Retry form;
        retry POST resets qa_error_phase to None; active run hides failure card.

Stubs ``claude_agent_sdk`` and ``requests``, replaces ``threading.Thread`` with a sync stub
so the /api/qa background job runs INLINE, then asserts via the Flask test client."""
import html as _html
import sys
import tempfile
import threading
import types
from pathlib import Path

sys.path.insert(0, ".")

# ═══════════════════════════════════════════════════════════════════════════
# Stub dependencies BEFORE any orchestrator import
# ═══════════════════════════════════════════════════════════════════════════
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

req = types.ModuleType("requests")
req.Session = lambda *a, **k: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules["requests"] = req

# ── SyncThread ────────────────────────────────────────────────────────────
class _SyncThread:
    def __init__(self, target=None, daemon=False, **kw):
        self.t = target
    def start(self):
        if self.t:
            self.t()

# ═══════════════════════════════════════════════════════════════════════════
# Imports after stubs
# ═══════════════════════════════════════════════════════════════════════════
import orchestrator.cockpit_state as cockpit_state
import orchestrator.server as srv
from orchestrator import council as _council_mod
from orchestrator import patrol as _patrol_mod
from orchestrator.config import AppConfig, Config

results: list[tuple[str, bool, str]] = []


def chk(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition), detail))


tmp = Path(tempfile.mkdtemp())
AUDIT = tmp / "audit.jsonl"
AUDIT.write_text("", encoding="utf-8")

cfg = Config(apps=[AppConfig(name="alpha", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(AUDIT), use_worktree=False)
cfg.detected_auth = lambda: "test"
srv.health.summary = lambda c: {"healthy": True, "checks": []}

_sync_threading = types.SimpleNamespace(Thread=_SyncThread, Event=threading.Event,
                                        Lock=threading.Lock)
srv.threading = _sync_threading

client = srv.create_app(cfg).test_client()


def _reset():
    """Fresh defaults-only registry."""
    cockpit_state.reset_run_state()


def _html_body(response) -> str:
    """Return the decoded HTML body of a GET response."""
    return response.get_data(as_text=True)


# ══════════════════════════════════════════════════════════════════════════
# AC1: Patrol-phase failure → "QA failed during Phase 1" + error + Retry
# ══════════════════════════════════════════════════════════════════════════
_patrol_raised_flag = [False]


async def _fp_patrol_boom(c, app_name, do_file=True, audit=None):
    _patrol_raised_flag[0] = True
    raise RuntimeError("recon connection refused")


_council_not_called = [False]


async def _sr_never_runs(c, app_name, audit=None):
    _council_not_called[0] = True


_patrol_mod.patrol = _fp_patrol_boom
_council_mod.ship_review = _sr_never_runs

_reset()
st = cockpit_state.get_state()
r = client.post("/api/qa", data={"app": "alpha"})

chk("AC1: POST /api/qa returns redirect on claim",
    r.status_code in (302, 303), f"got {r.status_code}")
chk("AC1: patrol actually raised", _patrol_raised_flag[0], "patrol was never called")
chk("AC1: ship_review NOT reached after patrol crash", not _council_not_called[0], "it ran")
chk("AC1: error_phase='patrol'",
    st.get("qa_error_phase") == "patrol", f"got: {st.get('qa_error_phase')!r}")

r_get = client.get("/")
body = _html_body(r_get)

chk("AC1: 'QA failed during Phase 1' in page",
    "QA failed during Phase 1" in body,
    "failure phase label missing from rendered HTML")
chk("AC1: real error text shown ('recon connection refused')",
    "recon connection refused" in body,
    "exception message not rendered in failure state")
chk("AC1: Retry form action=/api/qa present",
    'action=/api/qa' in body and 'method=post' in body,
    "retry form or wrong action in failure card")
chk("AC1: hidden app input names 'alpha' in retry form",
    '<input type=hidden name=app value="alpha">' in body,
    "app hidden field missing or wrong value in retry form")
chk("AC1: Retry submit button text present",
    "Retry" in body, "Retry button text not found")

# Restore originals
_patrol_mod.patrol = None
_council_mod.ship_review = None

# ══════════════════════════════════════════════════════════════════════════
# AC2: Ship-review failure → "Phase 2" + partial findings + Retry
# ══════════════════════════════════════════════════════════════════════════
_sr_ship_boom_flag = [False]


async def _fp_patrol_clean(c, app_name, do_file=True, audit=None):
    return _patrol_mod.PatrolSummary("patrol ok", ["EU-910"])


async def _sr_ship_boom_fn(c, app_name, audit=None):
    _sr_ship_boom_flag[0] = True
    raise RuntimeError("ship verdict LLM error")


_patrol_mod.patrol = _fp_patrol_clean
_council_mod.ship_review = _sr_ship_boom_fn

_reset()
st = cockpit_state.get_state()
r2 = client.post("/api/qa", data={"app": "alpha"})

chk("AC2: ship_review raised", _sr_ship_boom_flag[0], "ship_review wasn't called")
chk("AC2: error_phase='ship_review'",
    st.get("qa_error_phase") == "ship_review",
    f"got: {st.get('qa_error_phase')!r}")
chk("AC2: patrol findings preserved after ship_review crash",
    st.get("qa_findings") == ["EU-910"],
    f"found: {st.get('qa_findings')}")

r2_get = client.get("/")
body2 = _html_body(r2_get)

chk("AC2: 'QA failed during Phase 2' in page",
    "QA failed during Phase 2" in body2,
    "phase 2 label missing")
chk("AC2: real error text ('ship verdict LLM error') shown",
    "ship verdict LLM error" in body2,
    "error text not in failure card")
chk("AC2: partial finding link '/browse/EU-910' present",
    "/browse/EU-910" in body2,
    "partial finding browse link missing (EU-910)")
chk("AC2: Retry form still present on phase 2 failure",
    'action=/api/qa' in body2,
    "retry form absent after phase-2 failure")
chk("AC2: no findings report card leaked (qa_dismissed stays True)",
    "Findings Report" not in body2,
    "report card incorrectly rendered alongside failure (qa_dismissed should stay True)")

# Restore originals
_patrol_mod.patrol = None
_council_mod.ship_review = None

# ══════════════════════════════════════════════════════════════════════════
# AC3: Retry re-claims QA flag, clears stale error, fresh run succeeds
# ══════════════════════════════════════════════════════════════════════════
_retry_succeeded = [False]


async def _fp_retry_ok(c, app_name, do_file=True, audit=None):
    return _patrol_mod.PatrolSummary("patrol ok", [])


async def _sr_retry_ok(c, app_name, audit=None):
    _retry_succeeded[0] = True
    return "GO — clean dev"


_patrol_mod.patrol = _fp_retry_ok
_council_mod.ship_review = _sr_retry_ok

# Seed failure state manually (simulating what we just tested above)
_reset()
cockpit_state._state.update({
    "qa_error_phase": "patrol",
    "qa_findings": [],
    "qa_verdict": "",
    "qa_dismissed": True,
})

r3 = client.post("/api/qa", data={"app": "alpha"})
chk("AC3: Retry POST /api/qa redirects",
    r3.status_code in (302, 303), f"got {r3.status_code}")
chk("AC3: fresh run executes both phases", _retry_succeeded[0], "retry didn't run")
chk("AC3: qa_error_phase cleared after successful retry",
    st.get("qa_error_phase") is None,
    f"got: {st.get('qa_error_phase')!r}")
chk("AC3: qa_dismissed flipped False after successful retry",
    st.get("qa_dismissed") is False,
    f"got: {st.get('qa_dismissed')}")

r3_get = client.get("/")
body3 = _html_body(r3_get)

chk("AC3: 'QA failed during' gone after successful retry",
    "QA failed during" not in body3,
    "stale error label persists after successful run")

# Restore
_patrol_mod.patrol = None
_council_mod.ship_review = None

# ══════════════════════════════════════════════════════════════════════════
# Test 4: Active run hides failure card (no contradictory UI mid-run)
# Real threading so bg thread runs concurrently while we check state
# ══════════════════════════════════════════════════════════════════════════
_async_threading = types.SimpleNamespace(Thread=threading.Thread,
                                         Event=threading.Event, Lock=threading.Lock)

# Temporarily switch back to real threading so the bg thread runs concurrently
srv.threading = _async_threading

_reset()
st = cockpit_state.get_state()
st["qa_error_phase"] = "patrol"       # leftover from a previous attempt
st["qa_started"] = cockpit_state.time.time()
st["qa"] = True                       # an in-flight run — must hide stale failure

client2 = srv.create_app(cfg).test_client()
r4 = client2.get("/")
body4 = _html_body(r4)

# The view renders the progress strip when qa is truthy AND the failure card only when qa is falsy.
# So even though qa_error_phase='patrol', the failure text should NOT appear — the progress strip should.
chk("Test 4: progress strip appears while qa is active",
    "<span id=qaphase" in body4,
    "qa-strip not rendered during active run")
chk("Test 4: failure text hidden while qa is active",
    "QA failed during Phase 1" not in body4,
    "stale failure showed during active run (should see progress strip instead)")

# Restore sync threading for clean-up
srv.threading = _sync_threading

# ══════════════════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════════════════
print("\n============ EU-582 QA FAILURE STATE TEST ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
if passed == len(results):
    print("  RESULT: ALL GREEN")
else:
    print(f"  RESULT: {len(results) - passed} FAIL(s)")
sys.exit(0 if passed == len(results) else 1)
