"""EU-104: per-tab run-state isolation — feed isolation + clear-on-terminal + ticket re-validation.

Covers:
  1. Feed isolation: log lines tagged to one app do NOT appear in another app's filtered feed,
     and vice versa — each tab sees only its own project's output.
  2. Clear-on-terminal: release_run zeroes is_active AND the display fields
     (autopilot_on, last_msg, last_activity) so a finished run never ghosts as 'Working'.
  3. Ticket re-validation: render_board suppresses the 'Working' active-run card when the Jira
     ticket is Done/Closed — a stale cockpit tab cannot show a ghost run for a landed ticket.

Style matches tests/eu63_tab_state_test.py (simple assert-based) and
tests/cockpit_run_state_test.py (pytest-style functions with a shared setup()).
"""
import json
import sys
import tempfile
import time
import types
from datetime import datetime
from pathlib import Path

sys.path.insert(0, ".")

import orchestrator.cockpit_state as cs

# Stub claude_agent_sdk so warroom can import without the real SDK installed.
_sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
_sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", _sdk)

from orchestrator import warroom


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def setup():
    """Reset all per-app run state and the log ring buffer before each test."""
    cs.reset_run_state()
    # Also clear the ring buffer so previous tests' lines don't bleed.
    cs._LOG.clear()


def _fake_tee():
    """Return a _Tee wired to a no-op file so write() populates _LOG without touching stdout."""
    class _NullFile:
        def write(self, v): pass
        def flush(self): pass
    return cs._Tee(_NullFile())


def _make_cfg_with_active_audit(app_name="myapp"):
    """Minimal cfg with one in-flight audit entry (ticket_start + build, no terminal event)."""
    d = Path(tempfile.mkdtemp())
    audit = d / "audit.jsonl"
    now = datetime.now().astimezone()
    ts = now.strftime("%Y-%m-%dT%H:%M:%S%z")
    rows = [
        dict(event="ticket_start", ticket_id="AUTO-99", app=app_name,
             branch="auto/AUTO-99", ts=ts),
        dict(event="build", ticket_id="AUTO-99", app=app_name,
             iteration=1, turns=5, cost_usd=0.0, effort="low",
             tools=["Read"], summary="wip", ts=ts),
    ]
    audit.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    ns = types.SimpleNamespace
    apps = [ns(name=app_name, backlog_backend="jira", backlog={})]
    cfg = ns(audit_path=str(audit), apps=apps)

    def _app(name):
        for a in cfg.apps:
            if a.name == name:
                return a
        raise KeyError(name)
    cfg.app = _app
    return cfg


# ---------------------------------------------------------------------------
# 1. Feed isolation
# ---------------------------------------------------------------------------

def test_automatixy_lines_absent_from_eu_feed():
    """Lines written while only 'automatixy' is active must NOT appear in the 'elite-unit' feed."""
    setup()
    cs.claim_run("automatixy")
    tee = _fake_tee()
    tee.write("automatixy-build-step\n")
    cs.release_run("automatixy")

    eu_lines = cs.recent_log(100, app="elite-unit")
    assert "automatixy-build-step" not in eu_lines, (
        f"automatixy line bled into elite-unit feed: {eu_lines}"
    )


def test_eu_lines_absent_from_automatixy_feed():
    """Lines written while only 'elite-unit' is active must NOT appear in the 'automatixy' feed."""
    setup()
    cs.claim_run("elite-unit")
    tee = _fake_tee()
    tee.write("eu-build-step\n")
    cs.release_run("elite-unit")

    auto_lines = cs.recent_log(100, app="automatixy")
    assert "eu-build-step" not in auto_lines, (
        f"elite-unit line bled into automatixy feed: {auto_lines}"
    )


def test_each_app_sees_only_its_own_lines():
    """Sequential runs: each app's filtered feed contains only its own lines, not the other's."""
    setup()
    tee = _fake_tee()

    cs.claim_run("automatixy")
    tee.write("auto-line-A\n")
    cs.release_run("automatixy")

    cs.claim_run("elite-unit")
    tee.write("eu-line-B\n")
    cs.release_run("elite-unit")

    auto_lines = cs.recent_log(100, app="automatixy")
    eu_lines   = cs.recent_log(100, app="elite-unit")

    assert "auto-line-A" in auto_lines,     "automatixy line missing from its own feed"
    assert "eu-line-B"   not in auto_lines, "EU line bled into automatixy feed"
    assert "eu-line-B"   in eu_lines,       "EU line missing from its own feed"
    assert "auto-line-A" not in eu_lines,   "automatixy line bled into EU feed"


def test_unfiltered_feed_returns_all_lines():
    """recent_log() with no app is backward-compatible: returns ALL lines regardless of app tag."""
    setup()
    tee = _fake_tee()

    cs.claim_run("automatixy")
    tee.write("auto-global-line\n")
    cs.release_run("automatixy")

    cs.claim_run("elite-unit")
    tee.write("eu-global-line\n")
    cs.release_run("elite-unit")

    all_lines = cs.recent_log(100)
    assert "auto-global-line" in all_lines, "automatixy line missing from unfiltered feed"
    assert "eu-global-line"   in all_lines, "EU line missing from unfiltered feed"


# ---------------------------------------------------------------------------
# 2. Clear-on-terminal
# ---------------------------------------------------------------------------

def test_is_active_false_after_release():
    """is_active must be False immediately after release_run (the simplest terminal outcome)."""
    setup()
    cs.claim_run("automatixy")
    assert cs.is_active("automatixy"), "is_active must be True after claim_run"

    cs.release_run("automatixy")
    assert not cs.is_active("automatixy"), "is_active must be False after release_run"


def test_display_fields_cleared_after_release():
    """After release_run the liveness fields that drive the 'Working' cockpit card are zeroed —
    but ``last_msg`` is PRESERVED so a run's failure reason survives the terminal release.

    EU-104 iter-3: release_run runs in the run _bg's ``finally`` right after the ``except`` writes
    the failure reason to last_msg, so it must not clear last_msg (else the operator never learns
    why the run failed).  Clearing the transient 'stopping…' note on a CLEAN outcome is the _bg's
    job, guarded by whether the run raised — see tests/eu104_run_error_survives_test.py.
    """
    setup()
    cs.claim_run("automatixy")
    st = cs.get_state("automatixy")
    # Simulate in-flight liveness fields + a run error the operator must still be able to read.
    st["autopilot_on"] = True
    st["last_msg"] = "run failed: boom"
    st["last_activity"] = time.time()

    cs.release_run("automatixy")

    assert not cs.is_active("automatixy"), "active must be False after release_run"
    assert st["autopilot_on"] is False, (
        "autopilot_on must be cleared by release_run (ghost autopilot badge — EU-104)"
    )
    assert st["last_activity"] is None, (
        "last_activity must be cleared by release_run (stale heartbeat timestamp — EU-104)"
    )
    assert st["last_msg"] == "run failed: boom", (
        "release_run must NOT clear last_msg — a run's failure reason must survive the terminal "
        "release so the operator still sees why it failed (EU-104 iter-3 review fix)"
    )


def test_per_app_state_cleared_independently():
    """Releasing one app's run must not touch the other app's run state."""
    setup()
    cs.claim_run("automatixy")
    cs.claim_run("elite-unit")

    cs.release_run("automatixy")

    # automatixy is idle; elite-unit is still active.
    assert not cs.is_active("automatixy"), "automatixy must be idle after its release"
    assert cs.is_active("elite-unit"),     "elite-unit must still be active (not released yet)"

    cs.release_run("elite-unit")
    assert not cs.is_active("elite-unit"), "elite-unit must be idle after its own release"


def test_release_idempotent_after_terminal_outcome():
    """A second release_run on an already-idle app must not raise and must leave state idle."""
    setup()
    cs.claim_run("automatixy")
    cs.release_run("automatixy")   # first release — terminal outcome
    cs.release_run("automatixy")   # second release — must be a no-op, not raise
    assert not cs.is_active("automatixy"), "double-release must not re-activate the app"


# ---------------------------------------------------------------------------
# 3. Ticket re-validation — render_board suppresses 'Working' when Jira says Done
# ---------------------------------------------------------------------------

def test_render_board_suppresses_working_card_for_done_ticket():
    """render_board must show 'last run' idle card, NOT 'Working', when Jira ticket is Done."""
    warroom._TICKET_STATUS_CACHE.clear()
    cfg = _make_cfg_with_active_audit()

    orig_td = warroom._ticket_done
    warroom._ticket_done = lambda *a, **k: True   # ticket is Done
    try:
        board = warroom.render_board(cfg, "myapp", {"active": True})
        assert "Working" not in board, (
            "render_board must suppress 'Working' for a Done ticket (EU-104 ghost-run guard)"
        )
        assert "last run" in board, (
            "render_board must show 'last run' idle card when Jira ticket is Done (EU-104)"
        )
    finally:
        warroom._ticket_done = orig_td
        warroom._TICKET_STATUS_CACHE.clear()


def test_render_board_shows_working_card_for_active_ticket():
    """render_board must show 'Working' when the Jira ticket is still In Progress."""
    warroom._TICKET_STATUS_CACHE.clear()
    cfg = _make_cfg_with_active_audit()

    orig_td = warroom._ticket_done
    warroom._ticket_done = lambda *a, **k: False   # ticket still in progress
    try:
        board = warroom.render_board(cfg, "myapp", {"active": True})
        assert "Working" in board, (
            "render_board must show 'Working' when the Jira ticket is still in progress"
        )
    finally:
        warroom._ticket_done = orig_td
        warroom._TICKET_STATUS_CACHE.clear()


def test_render_board_no_active_run_card_when_ticket_closed():
    """render_board with state active=False and a Closed ticket must show 'last run', not 'Working'."""
    warroom._TICKET_STATUS_CACHE.clear()
    cfg = _make_cfg_with_active_audit()

    orig_td = warroom._ticket_done
    warroom._ticket_done = lambda *a, **k: True   # ticket is Closed
    try:
        # Simulate: in-memory flag was reset (crash recovery) but audit still has an open entry.
        board = warroom.render_board(cfg, "myapp", {"active": False})
        assert "Working" not in board, (
            "render_board must NOT show 'Working' for a Closed ticket even if inflight heuristic fires"
        )
    finally:
        warroom._ticket_done = orig_td
        warroom._TICKET_STATUS_CACHE.clear()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            setup()
            fn()
            print(f"ok  {name}")
    print("all eu104_run_isolation tests passed")
