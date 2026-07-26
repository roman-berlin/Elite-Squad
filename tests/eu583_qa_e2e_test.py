"""EU-583 — End-to-end QA progress strip + report card integration.

Walks the FULL lifecycle in one flow, proving the pieces wire together correctly:
  E2E-1 Happy-path lifecycle: POST /api/qa → phase-1 UI → phase-2 UI → report card → dismiss
  E2E-2 Failure-path lifecycle: ship_review raises → failure UI + partial findings + retry POST
  E2E-3 Full gate: test auto-discovers (appears in run_all output)

Stubs ``claude_agent_sdk`` and ``requests``, replaces ``threading.Thread`` with a sync stub so the
/api/qa background job runs INLINE, then uses closure callbacks to observe intermediate state AND
rendered HTML mid-lifecycle, exactly like the sibling tests do."""
import html as _html_mod
import sys
import tempfile
import threading
import time
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

# ── SyncThread: run the /api/qa background job INLINE ─────────────────────
class _SyncThread:
    def __init__(self, target=None, daemon=False, **kw):
        self.t = target
        self.done = False

    def start(self):
        if self.t:
            self.t()
        self.done = True


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


def _html_body(response) -> str:
    """Return the decoded HTML body of a GET response."""
    return response.get_data(as_text=True)


# Shared temp dir and config for all tests below
tmp = Path(tempfile.mkdtemp())
AUDIT = tmp / "audit.jsonl"
AUDIT.write_text("", encoding="utf-8")

cfg = Config(apps=[AppConfig(name="alpha", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(AUDIT), use_worktree=False)
cfg.detected_auth = lambda: "test"
srv.health.summary = lambda c: {"healthy": True, "checks": []}

# Patch threading before create_app
_sync_threading = types.SimpleNamespace(Thread=_SyncThread, Event=threading.Event,
                                        Lock=threading.Lock)
srv.threading = _sync_threading

client = srv.create_app(cfg).test_client()


def _reset():
    """Fresh defaults-only registry."""
    cockpit_state.reset_run_state()


# ══════════════════════════════════════════════════════════════════════════
# E2E-1: Happy-path lifecycle — phase 1 UI → phase 2 UI → report card → dismiss
# ══════════════════════════════════════════════════════════════════════════

# Closures to capture snapshots at key points during the single bg call.
_phase1_snapshot: dict = {}       # {body: ..., status: ...} captured INSIDE patrol
_phase2_snapshot: dict = {}       # {body: ..., status: ...} captured INSIDE ship_review
_report_snapshot: dict = {}       # {body: ..., status: ...} captured AFTER both phases

st = cockpit_state.get_state()


async def _fp_happy(c, app_name, do_file=True, audit=None):
    """Patrol: sets phase-1 findings and captures a phase-1 UI snapshot."""
    st["phase1_snap"] = True  # marker: we're inside patrol
    # Snapshot the home page DURING phase 1
    r1 = client.get("/")
    _phase1_snapshot["body"] = _html_body(r1)
    qs1 = client.get("/api/qa-status").get_json()
    _phase1_snapshot["status"] = qs1
    return _patrol_mod.PatrolSummary("patrol ok", ["EU-901", "EU-902"])


async def _sr_happy_go(c, app_name, audit=None):
    """Ship-review: captures a phase-2 UI snapshot then returns a GO verdict."""
    st["ship_review_done"] = True  # marker: we're inside ship_review
    # Snapshot the home page DURING phase 2
    r2 = client.get("/")
    _phase2_snapshot["body"] = _html_body(r2)
    qs2 = client.get("/api/qa-status").get_json()
    _phase2_snapshot["status"] = qs2
    return "GO — dev is ready"


_patrol_mod.patrol = _fp_happy
_council_mod.ship_review = _sr_happy_go

_reset()
r_qa = client.post("/api/qa", data={"app": "alpha"})

chk("E2E-1a: POST /api/qa redirects",
    r_qa.status_code in (302, 303), f"got {r_qa.status_code}")

chk("E2E-1a: patrol ran inside bg",
    st.get("phase1_snap") is True,
    f"marker={st.get('phase1_snap')}")

chk("E2E-1a: ship_review ran inside bg",
    st.get("ship_review_done") is True,
    f"marker={st.get('ship_review_done')}")

# --- Phase 1 UI assertions (captured INSIDE patrol, before ship_review started)
p1 = _phase1_snapshot
chk("E2E-1b: Phase 1 label visible mid-patrol",
    "Phase 1/2" in p1.get("body", ""), "Phase 1/2 missing from / during patrol")
chk("E2E-1b: Phase 1 desc 'inspecting dev' visible mid-patrol",
    "inspecting dev" in p1.get("body", ""), "'inspecting dev' missing mid-patrol")
chk("E2E-1b: Run QA button disabled mid-patrol",
    "disabled" in p1.get("body", ""), "'disabled' not in / during patrol")
chk("E2E-1b: qa-strip rendered mid-patrol",
    '<div class="qa-strip">' in p1.get("body", ""), "no qa-strip mid-patrol")
chk("E2E-1b: elapsed timer span present mid-patrol",
    'id=qaelapsed' in p1.get("body", ""), "no elapsed timer mid-patrol")
chk("E2E-1b: /api/qa-status active=True, phase='patrol'",
    p1.get("status", {}).get("active") is True,
    f"active={p1.get('status', {}).get('active')}")
chk("E2E-1b: /api/qa-status phase='patrol'",
    p1.get("status", {}).get("phase") == "patrol",
    f"phase={p1.get('status', {}).get('phase')}")
chk("E2E-1b: /api/qa-status elapsed_s >= 0",
    p1.get("status", {}).get("elapsed_s", -1) >= 0,
    f"elapsed_s={p1.get('status', {}).get('elapsed_s')}")

# --- Phase 2 UI assertions (captured INSIDE ship_review, after patrol finished)
p2 = _phase2_snapshot
chk("E2E-2a: Phase 2 label visible mid-ship_review",
    "Phase 2/2" in p2.get("body", ""), "Phase 2/2 missing from / during ship_review")
chk("E2E-2a: Phase 2 desc 'ship verdict' visible mid-ship_review",
    "ship verdict" in p2.get("body", ""), "'ship verdict' missing mid-ship_review")
chk("E2E-2a: /api/qa-status phase='ship_review'",
    p2.get("status", {}).get("phase") == "ship_review",
    f"phase={p2.get('status', {}).get('phase')}")
chk("E2E-2a: /api/qa-status still active during phase 2",
    p2.get("status", {}).get("active") is True,
    f"active={p2.get('status', {}).get('active')}")

# After both phases complete — the report should be visible on GET /
# (qa_phase cleared but qa_dismissed=False + findings/verdict exist)
r_home = client.get("/")
home_body = _html_body(r_home)

chk("E3: Report card title present after run",
    "Findings Report" in home_body,
    "report card not on home page after successful run")
chk("E3: Both Jira ticket links clickable",
    '/browse/EU-901' in home_body and '/browse/EU-902' in home_body,
    "missing browse links for EU-901/EU-902")
chk("E3: Jira links have target=_blank + rel=noopener",
    "target=_blank" in home_body and "rel=noopener" in home_body,
    "security attributes missing on jira links")
chk("E3: Verdict text present",
    "GO — dev is ready" in home_body,
    "verdict text missing from report card")
chk("E3: Dismiss button present",
    "Dismiss" in home_body,
    "dismiss button not in report card")
chk("E3: Findings count shown",
    "2 findings filed" in home_body,
    "finding count not displayed")

# AC4 persistence: second GET / must still show the report (not transient)
r_home2 = client.get("/")
home_body2 = _html_body(r_home2)
chk("E4: Report persists across reload (findings)",
    "2 findings filed" in home_body2,
    "report disappeared on second GET /")
chk("E4: Report persists across reload (verdict)",
    "GO — dev is ready" in home_body2,
    "verdict disappeared on second GET /")

# Dismiss the report
r_disc = client.post("/api/qa-dismiss", follow_redirects=False)
chk("E4: POST /api/qa-dismiss redirects",
    r_disc.status_code in (302, 303), f"got {r_disc.status_code}")
chk("E4: qa_dismissed flipped True after dismiss",
    st.get("qa_dismissed") is True,
    f"qa_dismissed={st.get('qa_dismissed')}")

# Card should be gone
r_after = client.get("/")
after_body = _html_body(r_after)
chk("E4: Report card gone after dismiss (no findings text)",
    "2 findings filed" not in after_body,
    "findings text persisted after dismiss")
chk("E4: Report card gone after dismiss (no verdict text)",
    "GO — dev is ready" not in after_body,
    "verdict text persisted after dismiss")
chk("E4: Report card gone after dismiss (no /browse/ links)",
    "/browse/" not in after_body,
    "browse link persisted after dismiss")

# ══════════════════════════════════════════════════════════════════════════
# E2E-2: Failure path — ship_review raises → failure UI + partial findings + retry
# ══════════════════════════════════════════════════════════════════════════

_patrol_partial_flag = [False]
_sr_raise_flag = [False]


async def _fp_partial_findings(c, app_name, do_file=True, audit=None):
    _patrol_partial_flag[0] = True
    return _patrol_mod.PatrolSummary("patrol ok", ["EU-910", "EU-911"])


async def _sr_ship_boom(c, app_name, audit=None):
    _sr_raise_flag[0] = True
    raise RuntimeError("ship review LLM timeout")


_patrol_mod.patrol = _fp_partial_findings
_council_mod.ship_review = _sr_ship_boom

_reset()
r_fail = client.post("/api/qa", data={"app": "alpha"})

chk("E2E-2a: POST /api/qa redirects after failure",
    r_fail.status_code in (302, 303), f"got {r_fail.status_code}")
chk("E2E-2a: patrol ran (partial findings filed)",
    _patrol_partial_flag[0], "patrol wasn't called")
chk("E2E-2a: ship_review raised",
    _sr_raise_flag[0], "ship_review didn't raise")
chk("E2E-2a: error_phase='ship_review'",
    st.get("qa_error_phase") == "ship_review",
    f"error_phase={st.get('qa_error_phase')!r}")
chk("E2E-2a: active=false after failure (qa flag cleared)",
    st.get("qa") is False,
    f"qa={st.get('qa')}")
chk("E2E-2a: partial findings preserved",
    st.get("qa_findings") == ["EU-910", "EU-911"],
    f"findings={st.get('qa_findings')}")

rf = client.get("/")
fail_body = _html_body(rf)

chk("E2E-2b: 'QA failed during Phase 2' text present",
    "QA failed during Phase 2" in fail_body,
    "failure phase label missing from HTML")
chk("E2E-2b: Error text rendered ('LLM timeout')",
    "LLM timeout" in fail_body,
    "error message not in failure block")
chk("E2E-2b: Partial findings links rendered (EU-910 + EU-911)",
    "/browse/EU-910" in fail_body and "/browse/EU-911" in fail_body,
    "partial finding links missing")
chk("E2E-2b: Retry form action=/api/qa present",
    'action=/api/qa' in fail_body and 'method=post' in fail_body,
    "retry form absent or wrong action")
chk("E2E-2b: hidden app input in retry form",
    '<input type=hidden name=app value="alpha">' in fail_body,
    "app hidden field missing in retry form")
chk("E2E-2b: Retry submit button visible",
    "Retry" in fail_body, "Retry button text not found")

# Verify no contradictory UI elements — report card should NOT leak alongside failure
chk("E2E-2b: No report card leaked alongside failure card",
    "Findings Report" not in fail_body,
    "report card incorrectly shown alongside failure")

# AC4 via /api/qa-status: error_phase='ship_review', active=False, findings preserved
qs_fail = client.get("/api/qa-status").get_json()
chk("E2E-2c: /api/qa-status error_phase='ship_review' after failure",
    qs_fail.get("error_phase") == "ship_review",
    f"error_phase={qs_fail.get('error_phase')}")
chk("E2E-2c: /api/qa-status active=false after failure",
    qs_fail.get("active") is False,
    f"active={qs_fail.get('active')}")
chk("E2E-2c: /api/qa-status findings preserved after failure",
    qs_fail.get("findings") == ["EU-910", "EU-911"],
    f"findings={qs_fail.get('findings')}")

# ══════════════════════════════════════════════════════════════════════════
# E2E-2d: Retry — POST /api/qa clears stale error, fresh run succeeds
# ══════════════════════════════════════════════════════════════════════════

_retry_ok = [False]


async def _fp_retry(c, app_name, do_file=True, audit=None):
    return _patrol_mod.PatrolSummary("patrol ok", [])


async def _sr_retry(c, app_name, audit=None):
    _retry_ok[0] = True
    return "GO — clean retry"


_patrol_mod.patrol = _fp_retry
_council_mod.ship_review = _sr_retry

r_retry = client.post("/api/qa", data={"app": "alpha"})
chk("E2E-2d: retry redirect",
    r_retry.status_code in (302, 303), f"got {r_retry.status_code}")
chk("E2E-2d: both phases executed on retry",
    _retry_ok[0], "retry didn't execute full pipeline")
chk("E2E-2d: error_phase cleared on fresh run",
    st.get("qa_error_phase") is None,
    f"error_phase={st.get('qa_error_phase')!r}")
chk("E2E-2d: qa_dismissed flipped False after success",
    st.get("qa_dismissed") is False,
    f"qa_dismissed={st.get('qa_dismissed')}")

rr = client.get("/")
retry_body = _html_body(rr)
chk("E2E-2d: 'QA failed during' GONE after retry",
    "QA failed during" not in retry_body,
    "stale failure label still visible after retry")
chk("E2E-2d: report card visible after successful retry",
    "GO — clean retry" in retry_body,
    "success report missing after retry")

# Restore originals
_patrol_mod.patrol = None
_council_mod.ship_review = None

# ══════════════════════════════════════════════════════════════════════════
print("\n============ EU-583 QA E2E INTEGRATION TEST ============================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-" * 72)
print(f"  {passed}/{len(results)} passed")
if passed == len(results):
    print("  RESULT: ALL GREEN")
else:
    print(f"  RESULT: {len(results) - passed} FAIL(s)")
sys.exit(0 if passed == len(results) else 1)
