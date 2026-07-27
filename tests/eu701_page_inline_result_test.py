"""EU-701: Render action failures on the page that owns the action.

Failures from /api/standup, /api/council, /api/scribe, /api/answer (Needs-you) and
/api/report used to surface ONLY on the home board's result strip — a user acting from
one of those pages saw nothing unless they navigated back to /. EU-701 renders the
stored last-result record inline on each owning page via the shared accessor + renderer:

  read   server.get_last_result(app)  — app scope first, unit-wide ``_state`` fallback
                                        (same convention as GET /api/last-result)
  render cockpit_views._result_strip  — the SAME strip the live board splices in, so
                                        every surface shares one tone map:
                                        ok→good (var(--ok*)), error→bad (var(--bad*)),
                                        warn→neutral (var(--warn*))  [EU-701 warn branch]

Per-page checks (one per AC): trigger a REAL failure through the page's own POST action
(the production writer path, not a hand-set record), GET the page, assert the failure
text renders inline, toned, and exactly once. Then the no-regression pins:

  R1  The board strip still renders the record, and SURVIVES a page visit — the page
      readers peek (EU-673/EU-677 class: a destructive reader here would erase the
      board's strip the moment someone opens /standup, exactly like the old /memory
      pop did).
  R2  The home page renders the result exactly once (no duplicate bar strip — EU-676).
  R3  Tone map: ok→good, error→bad, warn→neutral on the pages.
  R4  POST /api/dismiss-result clears the record the pages read (the strip's dismiss
      button contract the page helper's JS relies on).
"""
from __future__ import annotations

import sys
import tempfile
import time
import types
from pathlib import Path

# ── SDK / network stubs (same pattern as the sibling tests) ────────────────────
_sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
_sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = _sdk

_req = types.ModuleType("requests")
_req.Session = lambda: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = _req

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import cockpit_state, council, health, intent, memory, needs, server
from orchestrator.backlog import base as backlog_base
from orchestrator.config import AppConfig, Config

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))
    tag = "PASS" if cond else "FAIL"
    line = f"  [{tag}] {name}"
    if detail and not cond:
        line += f"  ({detail})"
    print(line)


print("\n================ EU-701 per-page inline result strips ================")

tmp = Path(tempfile.mkdtemp())
cfg = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(tmp / "app"), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="jira")],
    audit_path=str(tmp / "audit.jsonl"),
    use_worktree=False,
)
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()


def _clear() -> None:
    """Remove every last_result key from BOTH scopes (unit-wide + the test app)."""
    for st in (server._state, cockpit_state.get_state("automatixy")):
        st.pop("last_result", None)
        st.pop("last_result_record", None)


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
    written — spin it down so the page GET isn't gated (the pages suppress the strip
    while their action is in flight)."""
    end = time.time() + timeout
    while time.time() < end and server._state.get(flag):
        time.sleep(0.02)


# ===========================================================================
# /standup — /api/standup failure renders inline on /standup
# ===========================================================================
print("\n--- /standup: standup failure renders inline ---")
_clear()
_orig_hold_standup = council.hold_standup

async def _standup_boom(*a, **k):
    raise RuntimeError("boom")

council.hold_standup = _standup_boom
try:
    r = client.post("/api/standup")
    chk("standup: POST /api/standup redirects back to /standup",
        r.headers.get("Location", "").endswith("/standup"), r.headers.get("Location", ""))
    rec = _wait_result(None, "standup failed: boom")
    chk("standup: failure stored error-toned on the unit-wide scope",
        rec is not None and rec.get("tone") == "error", repr(rec))
finally:
    council.hold_standup = _orig_hold_standup
_wait_flag_clear("standuping")
page = client.get("/standup").get_data(as_text=True)
chk("standup: failure text renders inline on /standup", "standup failed: boom" in page)
chk("standup: renders exactly once on the page", page.count("standup failed: boom") == 1,
    f"count={page.count('standup failed: boom')}")
chk("standup: error tone paints the bad palette", "var(--badbg)" in page)
chk("standup: page GET is a peek — the stored record survives",
    isinstance(server.get_last_result(None), dict))

# ===========================================================================
# /council — /api/council failure renders inline on /council
# ===========================================================================
print("\n--- /council: council failure renders inline ---")
_clear()
_orig_hold_council = council.hold_council

async def _council_boom(*a, **k):
    raise RuntimeError("boom")

council.hold_council = _council_boom
try:
    r = client.post("/api/council")
    chk("council: POST /api/council redirects back to /council",
        r.headers.get("Location", "").endswith("/council"), r.headers.get("Location", ""))
    rec = _wait_result(None, "council failed: boom")
    chk("council: failure stored error-toned on the unit-wide scope",
        rec is not None and rec.get("tone") == "error", repr(rec))
finally:
    council.hold_council = _orig_hold_council
_wait_flag_clear("councilling")
page = client.get("/council").get_data(as_text=True)
chk("council: failure text renders inline on /council", "council failed: boom" in page)
chk("council: renders exactly once on the page", page.count("council failed: boom") == 1,
    f"count={page.count('council failed: boom')}")
chk("council: error tone paints the bad palette", "var(--badbg)" in page)

# ===========================================================================
# /memory — /api/scribe failure renders inline on /memory (replaces the old
# hand-rolled banner; still a peek — the EU-673 pop-regression stays closed)
# ===========================================================================
print("\n--- /memory: scribe failure renders inline ---")
_clear()
_orig_scribe = memory.scribe

async def _scribe_boom(*a, **k):
    raise RuntimeError("boom")

memory.scribe = _scribe_boom
try:
    r = client.post("/api/scribe")
    chk("memory: POST /api/scribe redirects back to /memory",
        r.headers.get("Location", "").endswith("/memory"), r.headers.get("Location", ""))
    rec = _wait_result(None, "scribe failed: boom")
    chk("memory: failure stored error-toned on the unit-wide scope",
        rec is not None and rec.get("tone") == "error", repr(rec))
finally:
    memory.scribe = _orig_scribe
_wait_flag_clear("scribing")
page = client.get("/memory").get_data(as_text=True)
chk("memory: failure text renders inline on /memory", "scribe failed: boom" in page)
chk("memory: renders exactly once on the page", page.count("scribe failed: boom") == 1,
    f"count={page.count('scribe failed: boom')}")
chk("memory: error tone paints the bad palette", "var(--badbg)" in page)
board = client.get("/api/board?app=automatixy").get_data(as_text=True)
chk("memory: the board strip survives the /memory visit (peek, EU-673 class)",
    "scribe failed: boom" in board)

# ===========================================================================
# /needs — /api/answer filing failure renders inline on /needs
# ===========================================================================
print("\n--- /needs: answer filing failure renders inline ---")
_clear()
_orig_classify = intent.classify_intent
_orig_make_backlog = backlog_base.make_backlog
_orig_summary = needs.summary
intent.classify_intent = lambda text: "file_ticket"

def _backlog_down(*a, **k):
    raise RuntimeError("jira down")

backlog_base.make_backlog = _backlog_down
needs.summary = lambda c, app_name=None: {
    "rows": [], "decisions": [], "proposals": [], "tasks": [], "total": 0}
try:
    r = client.post("/api/answer", data={"ticket": "AUTO-99", "app": "automatixy",
                                         "text": "file a ticket about this"})
    chk("needs: POST /api/answer redirects back to /needs",
        r.headers.get("Location", "").endswith("/needs"), r.headers.get("Location", ""))
    rec = server.get_last_result("automatixy")
    chk("needs: filing failure stored error-toned on the app scope",
        rec is not None and rec.get("tone") == "error"
        and "Filing failed for AUTO-99: jira down" in (rec.get("text") or ""), repr(rec))
finally:
    intent.classify_intent = _orig_classify
    backlog_base.make_backlog = _orig_make_backlog
    needs.summary = _orig_summary
page = client.get("/needs").get_data(as_text=True)
chk("needs: filing failure renders inline on /needs",
    "Filing failed for AUTO-99: jira down" in page)
chk("needs: renders exactly once on the page",
    page.count("Filing failed for AUTO-99: jira down") == 1,
    f"count={page.count('Filing failed for AUTO-99: jira down')}")
chk("needs: error tone paints the bad palette", "var(--badbg)" in page)

# ===========================================================================
# /report — /api/report health-block renders inline on /report
# ===========================================================================
print("\n--- /report: blocked report renders inline ---")
_clear()
_orig_health = health.summary
health.summary = lambda c: {"healthy": False, "checks": []}
try:
    client.post("/api/report", data={"app": "automatixy", "text": "the leads page is broken"})
    rec = server.get_last_result("automatixy")
    chk("report: health block stored error-toned on the app scope",
        rec is not None and rec.get("tone") == "error"
        and "blocked — fix the health problems first" in (rec.get("text") or ""), repr(rec))
finally:
    health.summary = _orig_health
page = client.get("/report").get_data(as_text=True)
chk("report: blocked outcome renders inline on /report",
    "blocked — fix the health problems first" in page)
chk("report: renders exactly once on the page",
    page.count("blocked — fix the health problems first") == 1,
    f"count={page.count('blocked — fix the health problems first')}")
chk("report: error tone paints the bad palette", "var(--badbg)" in page)

# ===========================================================================
# R1/R2 — no regression to the live-board strip or the home page
# ===========================================================================
print("\n--- R1/R2: board strip + home page render the record exactly as before ---")
_clear()
server.set_last_result(None, "error", "standup failed: board-pin")
board = client.get("/api/board?app=automatixy").get_data(as_text=True)
chk("R1a: the live-board strip still renders the record", "standup failed: board-pin" in board)
client.get("/standup")   # a page visit must NOT consume the record
board = client.get("/api/board?app=automatixy").get_data(as_text=True)
chk("R1b: the board strip survives a /standup visit (peek, not pop)",
    "standup failed: board-pin" in board)
chk("R1c: the stored record is still intact afterwards",
    isinstance(server.get_last_result(None), dict))
home = client.get("/").get_data(as_text=True)
chk("R2a: the home page renders the result exactly once (no duplicate strip)",
    home.count("standup failed: board-pin") == 1,
    f"count={home.count('standup failed: board-pin')}")
chk("R2b: the home render is a peek too — record survives GET /",
    isinstance(server.get_last_result(None), dict))

# ===========================================================================
# R3 — tone map on the pages: ok→good, error→bad, warn→neutral
# ===========================================================================
print("\n--- R3: ok/error/warn map onto good/bad/neutral on the pages ---")
_clear()
server.set_last_result(None, "warn", "careful: degraded backend")
page = client.get("/needs").get_data(as_text=True)
chk("R3a: warn renders the neutral palette",
    "careful: degraded backend" in page and "var(--warnbg)" in page)
server.set_last_result(None, "ok", "✓ all good")
page = client.get("/standup").get_data(as_text=True)
chk("R3b: ok renders the good palette", "✓ all good" in page and "var(--okbg)" in page)
# error→bad already pinned per-page above (var(--badbg) on all five).

# ===========================================================================
# R4 — dismiss contract the page strip's button relies on
# ===========================================================================
print("\n--- R4: POST /api/dismiss-result clears what the pages read ---")
server.set_last_result(None, "ok", "dismiss me")
client.post("/api/dismiss-result")
chk("R4: dismiss clears the unit-wide record", server.get_last_result(None) is None)
chk("R4: and the page renders no strip afterwards",
    "dismiss me" not in client.get("/standup").get_data(as_text=True))
_clear()

# ===========================================================================
# Report
# ===========================================================================
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print("\n-----------------------------------------------------------")
print(f"  {passed}/{total} passed")
if passed != total:
    print(f"  RESULT: {total - passed} FAIL")
    sys.exit(1)
print("  RESULT: ALL GREEN")
sys.exit(0)
