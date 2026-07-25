"""EU-579 — /api/qa-status endpoint + QA run-state regression tests (2026-07-25).

Stubs ``claude_agent_sdk`` and ``requests``, replaces ``threading.Thread`` with a sync stub so
the /api/qa background job runs INLINE, then asserts the endpoint's shape, the run-state fields
(phase / findings / verdict / error_phase / dismissed), and that a new run resets stale state.

Iteration-2 guard: EVERY patrol / ship_review stub below keeps the ORIGINAL signatures
``(c, app_name, do_file=True, audit=None)`` / ``(c, app_name, audit=None)`` — the same signatures
the EU-63/EU-27/EU-204 scoping harnesses stub. If the server ever passes an extra kwarg again,
these stubs raise TypeError at call time and this harness goes red — that is exactly how the
first attempt broke cockpit_star_test / eu63_ship_scope_test / eu63_tab_routes_test. Findings
ride the return value (``PatrolSummary.filed``), never a new call parameter. Exits non-zero on
any failed check."""
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

# ═══════════════════════════════════════════════════════════════════════════

import orchestrator.cockpit_state as cockpit_state
import orchestrator.server as srv
from orchestrator import council as _council_mod
from orchestrator import patrol as _patrol_mod
from orchestrator.config import AppConfig, Config

results: list[tuple[str, bool, str]] = []


def chk(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition), detail))


# ── SyncThread: run the /api/qa background job INLINE ─────────────────────
class _SyncThread:
    """Runs target INLINE on start() — makes the bg job deterministic for assertions."""
    def __init__(self, target=None, daemon=False, **kw):
        self.t = target

    def start(self):
        if self.t:
            self.t()


srv.threading = types.SimpleNamespace(Thread=_SyncThread, Event=threading.Event,
                                      Lock=threading.Lock)

tmp = Path(tempfile.mkdtemp())
AUDIT = tmp / "audit.jsonl"
AUDIT.write_text("", encoding="utf-8")

cfg = Config(apps=[AppConfig(name="alpha", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(AUDIT), use_worktree=False)
cfg.detected_auth = lambda: "test"
srv.health.summary = lambda c: {"healthy": True, "checks": []}

_old_patrol = _patrol_mod.patrol
_old_ship = _council_mod.ship_review

st = cockpit_state.get_state()


def _reset():
    """Fresh defaults-only registry (also drops ad-hoc flags like qa/patrolling/shipreview)."""
    cockpit_state.reset_run_state()


# ── Test group A: fresh-server defaults — never 500s when no QA ever ran ──
_reset()
client = srv.create_app(cfg).test_client()
r = client.get("/api/qa-status")
chk("GET /api/qa-status → HTTP 200", r.status_code == 200, f"status={r.status_code}")
body = r.get_json()
chk("response has exactly the 7 contract keys", sorted(body.keys()) ==
    ["active", "dismissed", "elapsed_s", "error_phase", "findings", "phase", "verdict"],
    f"keys={sorted(body.keys())}")
for field, expected in [("active", False), ("phase", None), ("elapsed_s", 0),
                        ("findings", []), ("verdict", ""), ("error_phase", None),
                        ("dismissed", True)]:
    chk(f"default {field}", body[field] == expected,
        f"{field}={body[field]!r}, expected={expected!r}")

# ── Test group B: findings = the REAL filed keys, threaded via the return value ──
_patrol_called: list = []


async def _fp_findings(c, app_name, do_file=True, audit=None):
    _patrol_called.append(app_name)
    return _patrol_mod.PatrolSummary("patrol ok", ["EU-901", "EU-902"])


async def _noop_ship(c, app_name, audit=None):
    return ""


_patrol_mod.patrol = _fp_findings
_council_mod.ship_review = _noop_ship

_reset()
client = srv.create_app(cfg).test_client()
client.post("/api/qa", data={"app": "alpha"})   # SyncThread → bg ran inline; state is final

chk("patrol called exactly once, concrete app", _patrol_called == ["alpha"], str(_patrol_called))
chk("qa_findings populated from PatrolSummary.filed",
    st.get("qa_findings") == ["EU-901", "EU-902"], f"found: {st.get('qa_findings')}")
chk("qa flag cleared after run", st.get("qa") is False, str(st.get("qa")))
ep = client.get("/api/qa-status").get_json()
chk("endpoint findings match the filed keys", ep["findings"] == ["EU-901", "EU-902"],
    f"found: {ep['findings']}")

# ── Test group B2: the iteration-1 regression — a plain-str patrol stub (the EU-63
#    harness signature) must not crash the run nor need any new kwarg ──
async def _fp_plain_str(c, app_name, do_file=True, audit=None):
    return "ok"     # legacy stubs return a bare str — no .filed attribute


_patrol_mod.patrol = _fp_plain_str
_reset()
client = srv.create_app(cfg).test_client()
client.post("/api/qa", data={"app": "alpha"})

chk("plain-str patrol stub: run completes without error",
    st.get("qa_error_phase") is None,
    f"error_phase={st.get('qa_error_phase')!r} last_result={st.get('last_result')!r}")
chk("plain-str patrol stub: findings default to [] (getattr fallback)",
    st.get("qa_findings") == [], f"found: {st.get('qa_findings')}")
chk("plain-str patrol stub: qa flag cleared", st.get("qa") is False, str(st.get("qa")))
ep = client.get("/api/qa-status").get_json()
chk("plain-str patrol stub: endpoint 200 + empty findings",
    ep["findings"] == [] and ep["error_phase"] is None, str(ep))

# ── Test group C: verdict = the EXACT decision text ship_review returns ──
_go_called = [False]
GO_TEXT = "GO — dev is ready"


async def _fp_no_findings(c, app_name, do_file=True, audit=None):
    return _patrol_mod.PatrolSummary("patrol ok", [])


async def _sr_go(c, app_name, audit=None):
    _go_called[0] = True
    return GO_TEXT


_patrol_mod.patrol = _fp_no_findings
_council_mod.ship_review = _sr_go

_reset()
client = srv.create_app(cfg).test_client()
client.post("/api/qa", data={"app": "alpha"})

chk("ship_review actually called", _go_called[0], "was never called")
chk("qa_verdict is the exact decision text — no reformatting",
    st.get("qa_verdict") == GO_TEXT, repr(st.get("qa_verdict")))
chk("qa_dismissed=False once a new report lands",
    st.get("qa_dismissed") is False, str(st.get("qa_dismissed")))
chk("qa_phase cleared after a successful run", st.get("qa_phase") is None,
    repr(st.get("qa_phase")))
ep = client.get("/api/qa-status").get_json()
chk("endpoint verdict = exact decision text", ep["verdict"] == GO_TEXT, repr(ep["verdict"]))
chk("endpoint dismissed=False", ep["dismissed"] is False, str(ep["dismissed"]))

# ── Test group D: error_phase tracks which phase failed ──
_sr_not_run_flag = [False]


async def _fp_fail(c, app_name, do_file=True, audit=None):
    raise RuntimeError("recon connection refused")


async def _sr_should_not_run(c, app_name, audit=None):
    _sr_not_run_flag[0] = True


_patrol_mod.patrol = _fp_fail
_council_mod.ship_review = _sr_should_not_run

_reset()
client = srv.create_app(cfg).test_client()
client.post("/api/qa", data={"app": "alpha"})

chk("patrol failure → error_phase='patrol'",
    st.get("qa_error_phase") == "patrol", f"got: {st.get('qa_error_phase')!r}")
chk("patrol failure → qa_phase cleared", st.get("qa_phase") is None, repr(st.get("qa_phase")))
chk("patrol failure → qa flag cleared", st.get("qa") is False, str(st.get("qa")))
chk("patrol failure → ship_review NOT reached", not _sr_not_run_flag[0], "it was called")

# ship_review raises AFTER patrol succeeds — findings must survive the failure
_sr_fail_called = [False]


async def _fp_clean(c, app_name, do_file=True, audit=None):
    return _patrol_mod.PatrolSummary("patrol ok", ["EU-910"])


async def _sr_fail(c, app_name, audit=None):
    _sr_fail_called[0] = True
    raise RuntimeError("CTO LLM error")


_patrol_mod.patrol = _fp_clean
_council_mod.ship_review = _sr_fail

_reset()
client = srv.create_app(cfg).test_client()
client.post("/api/qa", data={"app": "alpha"})

chk("ship_review failure → error_phase='ship_review'",
    st.get("qa_error_phase") == "ship_review", f"got: {st.get('qa_error_phase')!r}")
chk("ship_review failure → findings preserved",
    st.get("qa_findings") == ["EU-910"], f"found: {st.get('qa_findings')}")
chk("ship_review was reached", _sr_fail_called[0], "wasn't called")

# ── Test group E: a NEW run resets stale state from a previous run ──
_reset()


async def _fp_new_run(c, app_name, do_file=True, audit=None):
    return _patrol_mod.PatrolSummary("patrol ok", ["EU-999"])


# Seed STALE values from a pretend previous run
cockpit_state._state.update({
    "qa_started": 1000.0, "qa_phase": "patrol", "qa_error_phase": "patrol",
    "qa_findings": ["EU-1"], "qa_verdict": "old verdict", "qa_dismissed": False,
})

_patrol_mod.patrol = _fp_new_run
_council_mod.ship_review = _noop_ship

client = srv.create_app(cfg).test_client()
client.post("/api/qa", data={"app": "alpha"})

chk("stale findings OVERWRITTEN by the new run", st.get("qa_findings") == ["EU-999"],
    f"found: {st.get('qa_findings')}")
chk("stale verdict OVERWRITTEN at claim time", st.get("qa_verdict") == "",
    f"got: {st.get('qa_verdict')!r}")
chk("stale error_phase cleared (reset at claim + success)",
    st.get("qa_error_phase") is None, f"got: {st.get('qa_error_phase')!r}")
chk("qa_started refreshed (no longer the stale timestamp)",
    st.get("qa_started") != 1000.0, f"got: {st.get('qa_started')!r}")
ep = client.get("/api/qa-status").get_json()
chk("endpoint shows the NEW run's findings, not stale ones",
    ep["findings"] == ["EU-999"] and ep["verdict"] == "", str(ep))

# Restore originals
_patrol_mod.patrol = _old_patrol
_council_mod.ship_review = _old_ship

# ═══════════════════════════════════════════════════════════════════════════
print("\n============ EU-579 QA STATUS ENDPOINT TEST ============")
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
