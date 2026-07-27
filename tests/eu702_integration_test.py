"""EU-702 — End-to-end integration check: every action's outcome is visible.

This is the integration test for the epic "Make every action's outcome visible".
It verifies the whole feature chain — control-bar notes, result strips, per-page
inline strips, dismissals, timestamps — working together without gaps.

Acceptance criteria covered (mapping to original epic ACs):
  A  Control-bar notes: start/stop refusals and confirmations surface in the bar
     (per-app message priority, one-shot clear, explicit tone → CSS class).
  B  Result strips: one-shot outcomes show on the live board AND persist until dismissed.
  C  Per-page inline: failures from /council /standup /memory /needs /report render
     on the owning page via the same accessor + renderer as the board.
  D  Relative timestamps: both the control-bar note and the result strip show elapsed time.
  E  Dismiss contract: POST /api/dismiss-result clears the record everywhere.
  F  No tone-guessing: tone comes ONLY from stored records, never inferred from text.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from unittest.mock import patch

# ── SDK / network stubs ────────────────────────────────────────────────────────────
_sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
_sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", _sdk)

_req = types.ModuleType("requests")
_req.Session = lambda: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules.setdefault("requests", _req)

sys.path.insert(0, ".")

from orchestrator import cockpit_state, council, health, intent, memory, needs, server
from orchestrator import autopilot as _ap_mod
from orchestrator.cockpit_views import _result_strip, _rel, _control_bar
from orchestrator.config import AppConfig, Config
from orchestrator.backlog import base as backlog_base
from orchestrator import sync

sync.can_promote = lambda: False

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))
    tag = "PASS" if cond else "FAIL"
    line = f"  [{tag}] {name}"
    if detail and not cond:
        line += f"  ({detail})"
    print(line)


print("\n========== EU-702 INTEGRATION TEST ==========")

# ── harness ──────────────────────────────────────────────────────────────────────────
_TMP = Path(tempfile.mkdtemp())
_ap_mod._PID_FILE = _TMP / "general-autopilot.pid"
_CFG = Config(
    apps=[
        AppConfig(name="automatixy", repo_path=str(_TMP), base_branch="DEV",
                  protected_branch="MAIN", backlog_backend="jira"),
        AppConfig(name="Elite-Unit", repo_path=str(_TMP), base_branch="dev",
                  protected_branch="main", backlog_backend="none"),
    ],
    audit_path=str(_TMP / "a.jsonl"),
    use_worktree=False,
)
_CFG.detected_auth = lambda: "test"
server.health.summary = lambda c: {"healthy": True, "checks": []}
sync_can_promote = lambda: False

_APP = server.create_app(_CFG)
_CLIENT = _APP.test_client()

# Fake autopilot loop so we can hold runs alive.
_release = threading.Event()
_started_apps: list = []


async def _fake_autopilot(cfg, app_name=None, once=False, interval=60, stop_event=None):
    _started_apps.append(app_name)
    for _ in range(3000):
        if _release.is_set() or (stop_event is not None and stop_event.is_set()):
            return
        time.sleep(0.02)


_ap_mod.autopilot = _fake_autopilot


def _drain_threads() -> None:
    _release.set()
    for _ in range(1500):
        if cockpit_state.active_run_count() == 0:
            break
        time.sleep(0.02)
    cockpit_state.reset_run_state()
    _release.clear()
    _started_apps.clear()


def _clear_all() -> None:
    """Wipe last_result AND last_msg from all scopes."""
    # Unit-wide state
    server._state.pop("last_result", None)
    server._state.pop("last_result_record", None)
    server._state.pop("last_msg", None)
    server._state.pop("last_msg_record", None)
    # Per-app states via cockpit_state._states
    for st in list(cockpit_state._states.values()):
        st.pop("last_result", None)
        st.pop("last_result_record", None)
        st.pop("last_msg", None)
        st.pop("last_msg_record", None)


def _wait_result(scope, substr: str, timeout: float = 5.0):
    """Poll the accessor until the background action's record lands (ceremony routes
    run their action in a daemon thread)."""
    end = time.time() + timeout
    while time.time() < end:
        rec = server.get_last_result(scope)
        if rec and substr in (rec.get("text") or ""):
            return rec
        time.sleep(0.02)
    return server.get_last_result(scope)


def _wait_flag_clear(flag: str, timeout: float = 5.0) -> None:
    """The ceremony flag clears in the worker's finally RIGHT AFTER the record is
    written — spin it down so the page GET isn't gated."""
    end = time.time() + timeout
    while time.time() < end and server._state.get(flag):
        time.sleep(0.02)


# =============================================================================
# A — Control-bar notes: start/stop refusals and confirmations
# =============================================================================
print("\n--- A: Control-bar notes ---")


def _note_span(bar: str, msg: str, tone_class: str) -> bool:
    """Check that *msg* appears inside <span class=\"tbnote {tone_class}\"> ... </span>,
    allowing a timestamp span between them."""
    import html as _h
    open_tag = f'<span class="tbnote {tone_class}">'
    idx = bar.find(open_tag)
    if idx < 0:
        return False
    remainder = bar[idx:]
    escaped = _h.escape(msg)
    # Find the message text (already HTML-escaped in output)
    return escaped in remainder and '</span>' in remainder[remainder.index(escaped):]


# A1 — Run-selected refusal surfaces in control bar (original AC)
cockpit_state.reset_run_state()
_release.clear()
_started_apps.clear()

with patch("orchestrator.autopilot.daemon_running", return_value=False):
    r1 = _CLIENT.post("/api/autopilot",
                       data={"app": "automatixy", "action": "start", "mode": "drain"})
chk("A1-start: autopilot starts → 302",
    r1.status_code == 302, f"status={r1.status_code}")

with patch("orchestrator.intake.from_tickets",
                                        side_effect=RuntimeError("blocked")):
    r2 = _CLIENT.post("/api/run-selected",
                       data={"app": "automatixy", "ticket": "EU-999"},
                       follow_redirects=False)
chk("A1-select: run-selected while running → 302",
    r2.status_code == 302, f"status={r2.status_code}")

r3 = _CLIENT.get("/", follow_redirects=True)
body = r3.data.decode()
chk("A1-get: home renders OK", r3.status_code == 200)
chk("A1-text: refusal text appears on GET /",
    ("a run is already in progress" in body or "could not start" in body),
    f"text missing in first 400 chars: {body[:400]}")

_drain_threads()

# A2 — Control bar shows explicit tone (not guessed from text)
cockpit_state.reset_run_state()
server.set_last_msg(None, "error", "run stopped by operator")
bar = _control_bar(_CFG, "automatixy", True)
chk("A2-error: 'stopped by operator' rendered as bad (explicit tone)",
    _note_span(bar, "run stopped by operator", "bad"),
    f"note snippet: {[c for c in bar.split('tbnote') if 'operator' in c][:1]}")

# "0 failed" with tone ok should NOT paint red
cockpit_state.reset_run_state()
server.set_last_msg(None, "ok", "build finished, 0 failed")
bar2 = _control_bar(_CFG, "automatixy", True)
_snip_start = max(0, bar2.find('tbnote'))
chk("A2-warn-ok: '0 failed' with tone ok paints neutral (not bad)",
    _note_span(bar2, "build finished, 0 failed", "ok"),
    f"snippet: {bar2[_snip_start:_snip_start+150]}")

# A3 — Per-app message clears after render
cockpit_state.reset_run_state()
st = cockpit_state.get_state("automatixy")
server.set_last_msg("automatixy", "warn", "project-specific warning")
bar_a3 = _control_bar(_CFG, "automatixy", True)
chk("A3-first: per-app msg appears", "project-specific warning" in bar_a3)
chk("A3-clear: state cleared after render",
    st.get("last_msg") == "" and st.get("last_msg_record") is None,
    f"msg={st.get('last_msg')!r} rec={st.get('last_msg_record')!r}")
bar_a3b = _control_bar(_CFG, "automatixy", True)
chk("A3-second: msg does NOT persist",
    "project-specific warning" not in bar_a3b)


# =============================================================================
# B — Result strips: persist until dismissed
# =============================================================================
print("\n--- B: Result strips ---")

_clear_all()
server.set_last_result("automatixy", "ok", "✓ QA verdict passed")

# B1 — Strip visible in board
board = _CLIENT.get("/api/board?app=automatixy").get_data(as_text=True)
chk("B1-board: strip visible in board HTML",
    "QA verdict passed" in board and "data-dismiss-result" in board)

# B2 — Strip survives full GET /
home = _CLIENT.get("/").get_data(as_text=True)
chk("B2-home: strip also in home page",
    "QA verdict passed" in home)

# B3 — Strip persists on second render (non-destructive)
board2 = _CLIENT.get("/api/board?app=automatixy").get_data(as_text=True)
chk("B3-persist: strip STILL present after board re-render",
    "QA verdict passed" in board2)

# B4 — Legacy plain-string fallback still works
_clear_all()
server._state["last_result"] = "legacy fallback text"
strip_legacy = _result_strip(server._state)
chk("B4-legacy: plain-string fallback renders",
    "legacy fallback text" in strip_legacy)
chk("B4-legacy-not-pop: non-destructive",
    "legacy fallback text" in _result_strip(server._state))
_clear_all()


# =============================================================================
# C — Per-page inline: failures on the owning page
# =============================================================================
print("\n--- C: Per-page inline results ---")

_clear_all()

# C1 — Council failure renders on /council
_orig_council = council.hold_council
async def _boom(*a, **k):
    raise RuntimeError("council broken")
council.hold_council = _boom
try:
    _CLIENT.post("/api/council")
    rec = _wait_result(None, "council broken")
    chk("C1-write: council failure stored error-toned",
        rec is not None and rec.get("tone") == "error", repr(rec))
finally:
    council.hold_council = _orig_council

# Wait for background thread to finish
end = time.time() + 5
while time.time() < end and server._state.get("councilling"):
    time.sleep(0.02)

page = _CLIENT.get("/council").get_data(as_text=True)
chk("C1-page: council failure renders inline on /council",
    "council broken" in page and "var(--badbg)" in page)
# The board strip should also survive (peek, not pop)
board_check = _CLIENT.get("/api/board?app=automatixy").get_data(as_text=True)
chk("C1-board-survives: board strip still shows after /council visit",
    "council broken" in board_check)

# C2 — Scribe failure renders on /memory
_clear_all()
_orig_scribe = memory.scribe
async def _scribe_fail(*a, **k):
    raise RuntimeError("scribe crashed")
memory.scribe = _scribe_fail
try:
    _CLIENT.post("/api/scribe")
    rec2 = _wait_result(None, "scribe crashed")
    chk("C2-write: scribe failure stored error-toned",
        rec2 is not None and rec2.get("tone") == "error")
finally:
    memory.scribe = _orig_scribe

end = time.time() + 5
while time.time() < end and server._state.get("scribing"):
    time.sleep(0.02)

mem_page = _CLIENT.get("/memory").get_data(as_text=True)
chk("C2-page: scribe failure renders inline on /memory",
    "scribe crashed" in mem_page and "var(--badbg)" in mem_page)

# C3 — Standup failure renders on /standup
_clear_all()
_orig_standup = council.hold_standup
async def _standup_fail(*a, **k):
    raise RuntimeError("standup down")
council.hold_standup = _standup_fail
try:
    _CLIENT.post("/api/standup")
    rec3 = _wait_result(None, "standup down")
    chk("C3-write: standup failure stored error-toned",
        rec3 is not None and rec3.get("tone") == "error")
finally:
    council.hold_standup = _orig_standup

end = time.time() + 5
while time.time() < end and server._state.get("standuping"):
    time.sleep(0.02)

su_page = _CLIENT.get("/standup").get_data(as_text=True)
chk("C3-page: standup failure renders inline on /standup",
    "standup down" in su_page and "var(--badbg)" in su_page)

# C4 — Report health-block renders on /report
_clear_all()
_health_orig = server.health.summary
server.health.summary = lambda c: {"healthy": False, "checks": []}
try:
    _CLIENT.post("/api/report",
                 data={"app": "automatixy", "text": "bug report"})
    rec4 = server.get_last_result("automatixy")
    chk("C4-write: report block stored error-toned",
        rec4 is not None and rec4.get("tone") == "error")
finally:
    server.health.summary = _health_orig

rep_page = _CLIENT.get("/report").get_data(as_text=True)
chk("C4-page: blocked report renders inline on /report",
    "fix the health problems" in rep_page)


# =============================================================================
# D — Relative timestamps
# =============================================================================
print("\n--- D: Relative timestamps ---")

# D1 — _rel helper produces expected output
now = time.time()
chk("D1-now: now → Xs ago", "s ago" in _rel(now))
chk("D1-minute: 65s ago → minutes (contains 'm ago')", "m ago" in _rel(now - 65))
chk("D1-hour: 3661s ago → hours (contains 'h ago')", "h ago" in _rel(now - 3661))
from datetime import datetime, timezone
expected_date = datetime.fromtimestamp(now - 90000, tz=timezone.utc).strftime("%b %d")
chk("D1-day: 90000s ago → calendar date",
    expected_date in _rel(now - 90000),
    f"expected={expected_date!r} got={_rel(now - 90000)!r}")
chk("D1-none: None → empty string", _rel(None) == "")
chk("D1-bad: garbage → empty string", _rel("bogus") == "")

# D2 — _result_strip includes timestamp when record has it
_clear_all()
server.set_last_result("automatixy", "ok", "result with timestamp")
strip_ts = _result_strip(cockpit_state.get_state("automatixy"))
chk("D2-strip: result strip contains timestamp marker",
    "ago" in strip_ts.lower(),
    f"strip: {strip_ts[:200]}")

# D3 — Control-bar note includes timestamp when record has it
cockpit_state.reset_run_state()
server.set_last_msg("automatixy", "error", "bar note with timestamp")
bar_ts = _control_bar(_CFG, "automatixy", True)
chk("D3-bar: control-bar note contains timestamp marker",
    "ago" in bar_ts.lower(),
    f"bar snippet: {[c for c in bar_ts.split('tbnote') if 'timestamp' in c][:1]}")


# =============================================================================
# E — Dismiss contract
# =============================================================================
print("\n--- E: Dismiss contract ---")

_clear_all()
server.set_last_result("automatixy", "error", "dismiss-me-test")

# E1 — Strip visible before dismiss
pre = _CLIENT.get("/api/board?app=automatixy").get_data(as_text=True)
chk("E1-pre: strip visible before dismiss",
    "dismiss-me-test" in pre)

# E2 — Dismiss endpoint works
rv = _CLIENT.post("/api/dismiss-result?app=automatixy")
chk("E2-api: dismiss returns 200 {ok:true}",
    rv.status_code == 200, rv.get_data(as_text=True)[:80])
rv_json = json.loads(rv.get_data(as_text=True))
chk("E2-json: dismiss response JSON",
    rv_json.get("ok") is True)

# E3 — Strip gone after dismiss
post = _CLIENT.get("/api/board?app=automatixy").get_data(as_text=True)
chk("E3-post: strip GONE after dismiss",
    "dismiss-me-test" not in post)

# E4 — Home page also cleans
_home_post = _CLIENT.get("/").get_data(as_text=True)
chk("E4-home: home page clean after dismiss",
    "dismiss-me-test" not in _home_post)

# E5 — Idempotent
rv2 = _CLIENT.post("/api/dismiss-result?app=automatixy")
chk("E5-idem: double-dismiss → 200",
    rv2.status_code == 200)


# =============================================================================
# F — No tone-guessing anywhere
# =============================================================================
print("\n--- F: No tone-guessing ---")

# F1 — Text containing 'error' with tone='ok' renders green (not red)
_clear_all()
server.set_last_result("automatixy", "ok", "1 error recovered successfully")
board_f = _CLIENT.get("/api/board?app=automatixy").get_data(as_text=True)
chk("F1-ok-with-error-text: renders green (ok tone wins over text)",
    "var(--okbg)" in board_f)
chk("F1-no-red: does NOT contain var(--badbg)",
    "var(--badbg)" not in board_f)

# F2 — Text containing 'success' with tone='error' renders red (not green)
_clear_all()
server.set_last_result("automatixy", "error", "reported a success")
strip_f2 = _result_strip(cockpit_state.get_state("automatixy"))
chk("F2-error-with-success-text: renders red (error tone wins over text)",
    "var(--badbg)" in strip_f2)
chk("F2-no-green: does NOT contain var(--okbg)",
    "var(--okbg)" not in strip_f2)

# F3 — Warn tone is neutral (not red, not green)
_clear_all()
server.set_last_result("automatixy", "warn", "degraded backend")
strip_f3 = _result_strip(cockpit_state.get_state("automatixy"))
chk("F3-warn-tone: neutral palette (amber, not red/green)",
    "var(--warnbg)" in strip_f3 and "var(--badbg)" not in strip_f3 and "var(--okbg)" not in strip_f3)


# =============================================================================
# Report
# =============================================================================
passed_n = sum(1 for _, ok, _ in results if ok)
total_n = len(results)
print(f"\n{'=' * 50}")
print(f"  {passed_n}/{total_n} passed")
failed = [n for n, ok, d in results if not ok]
if failed:
    print(f"\n  FAILED ({len(failed)}):")
    for n in failed:
        for name, ok, det in results:
            if name == n and not ok:
                print(f"    - {name}: {det[:120]}")
print(f"\n  RESULT:", "ALL GREEN ✅" if passed_n == total_n else f"{total_n - passed_n} FAIL ❌")
sys.exit(0 if passed_n == total_n else 1)
