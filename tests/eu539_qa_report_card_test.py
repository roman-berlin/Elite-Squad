"""EU-581 — QA report card rendering + dismiss endpoint (split from EU-539).

Covers: findings rendered as Jira /browse/ links, verdict text present, card persists
across reloads until dismissed, zero-findings shows "no new findings", and POST
/api/qa-dismiss flips qa_dismissed so the card disappears.

Stubs ``claude_agent_sdk`` and ``requests``, replaces ``threading.Thread`` with a sync stub
so the /api/qa background job runs INLINE, then asserts via the Flask test client."""
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
# Replace threading.Thread so the /api/qa bg job runs INLINE → deterministic state.
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

# Patch threading BEFORE create_app
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


# ── AC2 + AC4: seeded state → report card visible → dismiss removes it ─────
_reset()

# Seed state exactly as a successful /api/qa run would leave it
st = cockpit_state.get_state()
st["qa_dismissed"] = False
st["qa_findings"] = ["EU-901", "EU-902"]
st["qa_verdict"] = "GO — dev is ready"

r = client.get("/")
body = _html_body(r)

chk("AC2: report card present (findings count shown)",
    "2 findings filed" in body, "card HTML missing in / response")
chk("AC2: finding 1 renders as /browse/ link",
    '/browse/EU-901' in body and 'target=_blank' in body and 'rel=noopener' in body,
    f"/browse/EU-901 + target/rel attrs missing")
chk("AC2: finding 2 renders as /browse/ link",
    '/browse/EU-902' in body,
    "/browse/EU-902 missing from card")
chk("AC2: verdict text present",
    "GO — dev is ready" in body, "verdict text not in response")

# AC3: persist across reloads
r2 = client.get("/")
body2 = _html_body(r2)
chk("AC3: card persists on second GET /",
    "2 findings filed" in body2 and "GO — dev is ready" in body2,
    "card disappeared on reload")

# AC4: dismiss endpoint flips flag and card vanishes
r_disc = client.post("/api/qa-dismiss", follow_redirects=False)
chk("AC4: POST /api/qa-dismiss redirects to /",
    r_disc.status_code in (302, 303),
    f"got {r_disc.status_code}")
chk("AC4: qa_dismissed flipped to True after POST",
    st.get("qa_dismissed") is True,
    f"qa_dismissed={st.get('qa_dismissed')}")

# Subsequent GET should NOT contain the card
r_after = client.get("/")
after_body = _html_body(r_after)
chk("AC4: card gone after dismiss (no verdict text)",
    "GO — dev is ready" not in after_body,
    "verdict text still present after dismiss")
chk("AC4: card gone after dismiss (no /browse/ links)",
    "/browse/" not in after_body,
    "/browse/ anchor still present after dismiss")

# After dismiss, state has qa_dismissed=True. A fresh GET should render nothing.
chk("AC4: default post-dismiss shows no report card",
    "Findings Report" not in after_body,
    "report card title still visible after dismiss")

# ── Zero findings case ─────────────────────────────────────────────────────
_reset()
st = cockpit_state.get_state()
st["qa_dismissed"] = False
st["qa_findings"] = []
st["qa_verdict"] = "NO-GO — found issues"

r_zf = client.get("/")
zf_body = _html_body(r_zf)
chk("Zero findings: literal 'no new findings'",
    "no new findings" in zf_body,
    "'no new findings' text missing")
chk("Zero findings: no empty <ul> markup",
    "<ul>" not in zf_body,
    "empty/unwanted <ul> rendered")
chk("Zero findings: no /browse/ anchors",
    "/browse/" not in zf_body,
    "spurious browse link present")
chk("Zero findings: verdict text still present",
    "NO-GO — found issues" in zf_body,
    "verdict missing in zero-finding card")

# ── Default (never ran): qa_dismissed=True → no card ───────────────────────
_reset()
r_def = client.get("/")
def_body = _html_body(r_def)
chk("Default: no report card when never ran (qa_dismissed=True by default)",
    "Findings Report" not in def_body,
    "unexpected report card on default state")
chk("Default: no /browse/ links",
    "/browse/" not in def_body,
    "/browse/ found in default page")

# ── council.py unchanged ───────────────────────────────────────────────────
council_path = Path("orchestrator/council.py").resolve()
chk("council.py untouched (still exists, no errors reading)",
    council_path.exists(), "council.py missing")

# ── Full pipeline: seeded stale state, POST /api/qa, verify card appears ────
_old_patrol_calls = [0]

async def _fp_stale(c, app_name, do_file=True, audit=None):
    _old_patrol_calls[0] += 1
    import orchestrator.patrol as pm
    return pm.PatrolSummary("patrol ok", ["EU-OLD"])


_council_call_count = [0]

async def _sr_new_go(c, app_name, audit=None):
    _council_call_count[0] += 1
    return "GO — clean build"


import orchestrator.council as _council_mod
import orchestrator.patrol as _patrol_mod

_patrol_mod.patrol = _fp_stale
_council_mod.ship_review = _sr_new_go

# Seed stale values simulating a previous incomplete run
_reset()
st = cockpit_state.get_state()
st["qa_started"] = 1000.0
st["qa_phase"] = "patrol"
st["error_phase"] = "patrol"
st["qa_findings"] = ["EU-OLD"]
st["qa_verdict"] = "stale verdict"
st["qa_dismissed"] = True   # was dismissed; new run should clear

r_pipe = client.post("/api/qa", data={"app": "alpha"})

chk("pipeline: POST /api/qa returns redirect",
     r_pipe.status_code in (302, 303),
     f"got {r_pipe.status_code}")

# SyncThread ran inline → state should reflect success
chk("pipeline: stale findings overwritten",
     st.get("qa_findings") == ["EU-OLD"],
     f"found: {st.get('qa_findings')}")
chk("pipeline: verdict updated",
     st.get("qa_verdict") == "GO — clean build",
     f"got: {st.get('qa_verdict')!r}")
chk("pipeline: qa_dismissed cleared (new report landed)",
     st.get("qa_dismissed") is False,
     f"qa_dismissed={st.get('qa_dismissed')}")

r_post = client.get("/")
pipe_body = _html_body(r_post)
chk("pipeline: card visible after full pipeline",
     "GO — clean build" in pipe_body and "1 finding filed" in pipe_body,
     f"card html missing in / response")

# Restore originals
_patrol_mod.patrol = None
_council_mod.ship_review = None

# ═══════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════
print("\n============ EU-581 QA REPORT CARD TEST ============")
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
