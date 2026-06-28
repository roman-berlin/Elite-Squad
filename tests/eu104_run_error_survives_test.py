"""EU-104 (iter-3): a terminal run release must NEVER swallow the run's error message.

The clear-on-terminal fix zeroes the run-slot + liveness fields (so a finished run never ghosts
as 'Working'), but it must NOT wipe ``last_msg``.  Each run's ``_bg`` writes the failure reason
there (``st['last_msg'] = str(exc)``) and ``release_run`` runs in the SAME ``finally`` immediately
afterwards — so if ``release_run`` cleared ``last_msg`` the operator would never see WHY a run
failed.  That clobber was the iteration-2 review rejection; these tests are its regression guard.

They drive the REAL Flask routes (a stubbed run_loop / autopilot that raises) and pin, for every
run path the reviewer named:

  * manual run        — POST /api/run        (run_api._bg / run_selected_api._bg share the block)
  * bug-report run    — POST /api/report     (the answer/report _bg)
  * autopilot         — POST /api/autopilot  (the autopilot _bg)

…that the error string survives the terminal release.  A fourth test pins the OTHER half: a CLEAN
terminal outcome still clears the transient 'stopping…' control-bar note, so the clear-on-terminal
behaviour the ticket needs is not lost.

Style follows tests/eu64_run_routes_test.py (Flask test client + a stubbed run_loop/autopilot).
"""
from __future__ import annotations

import sys
import tempfile
import time
import types
from pathlib import Path

# ── minimal Agent-SDK stub so the orchestrator imports without the real SDK installed ──
_sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self


_sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", _sdk)
sys.path.insert(0, ".")

from orchestrator import cockpit_state, server
from orchestrator import autopilot as _ap_mod
from orchestrator.config import AppConfig, Config


def _make_app():
    """A fresh single-project Flask app with a healthy unit and a no-op intake."""
    d = Path(tempfile.mkdtemp())
    (d / "audit.jsonl").write_text("")
    cfg = Config(
        apps=[AppConfig(name="alpha", repo_path=str(d), base_branch="DEV",
                        protected_branch="MAIN", backlog_backend="none")],
        audit_path=str(d / "audit.jsonl"), use_worktree=False)
    app = server.create_app(cfg)
    server.health.summary = lambda c: {"healthy": True, "checks": []}
    server.intake.from_text = lambda rcfg, ap, *a, **k: [f"wl:{ap}"]
    return app


def _wait_idle(app_key, timeout=5.0):
    """Block until the app's run releases its slot — once is_active is False the _bg ``finally``
    (release_run + the guarded note-clear) has fully run, so last_msg has settled."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not cockpit_state.is_active(app_key):
            return True
        time.sleep(0.02)
    return False


def setup():
    cockpit_state.reset_run_state()
    cockpit_state.set_max_parallel_runs(0)


def test_manual_run_error_survives_terminal_release():
    """A /api/run whose run_loop RAISES leaves the error string in last_msg after release_run."""
    setup()
    app = _make_app()

    async def boom(rcfg, worklist, audit, stop_event=None):
        raise RuntimeError("build exploded at the gate")
    server.run_loop = boom

    app.test_client().post("/api/run", data={"kind": "task", "text": "x", "app": "alpha"})
    assert _wait_idle("alpha"), "manual run never released its slot"

    st = cockpit_state.get_state("alpha")
    assert not st["active"], "active must be cleared on the terminal outcome"
    assert st["last_msg"] == "build exploded at the gate", (
        f"run error was swallowed by the terminal release — last_msg={st['last_msg']!r}; "
        "release_run must NOT clear last_msg (the operator must still see why the run failed)"
    )


def test_report_run_error_survives_terminal_release():
    """The bug-report (/api/report) run path also preserves its error in last_msg after release."""
    setup()
    app = _make_app()

    async def boom(rcfg, worklist, audit, stop_event=None):
        raise RuntimeError("report build failed")
    server.run_loop = boom

    app.test_client().post("/api/report", data={"text": "something is broken on DEV", "app": "alpha"})
    assert _wait_idle("alpha"), "report run never released its slot"

    st = cockpit_state.get_state("alpha")
    assert st["last_msg"] == "report build failed", (
        f"report-run error was swallowed by the terminal release — last_msg={st['last_msg']!r}"
    )


def test_autopilot_error_survives_terminal_release():
    """An autopilot loop that RAISES leaves 'autopilot error: …' in last_msg after release_run."""
    setup()
    app = _make_app()

    async def boom(cfg, app_name=None, once=False, interval=60, stop_event=None):
        raise RuntimeError("autopilot blew up")
    _ap_mod.autopilot = boom
    _ap_mod.daemon_is_external = lambda: False   # no foreign daemon — let this Start proceed

    app.test_client().post("/api/autopilot",
                           data={"action": "start", "mode": "drain", "app": "alpha"})
    assert _wait_idle("alpha"), "autopilot never released its slot"

    st = cockpit_state.get_state("alpha")
    assert not st["autopilot_on"], "autopilot_on must be cleared on the terminal outcome"
    assert st["last_msg"] == "autopilot error: autopilot blew up", (
        f"autopilot error was swallowed by the terminal release — last_msg={st['last_msg']!r}"
    )


def test_clean_run_clears_stale_stopping_note():
    """The other half: a CLEAN terminal outcome clears the transient 'stopping…' control-bar note,
    so a finished/stopped run never lingers as 'Working' (the clear-on-terminal the ticket needs)."""
    setup()
    app = _make_app()

    async def clean(rcfg, worklist, audit, stop_event=None):
        # Simulate the Stop button having written the transient control-bar note mid-run.
        cockpit_state.get_state("alpha")["last_msg"] = (
            "stopping after the current step — DEV untouched, no merge")
        return
    server.run_loop = clean

    app.test_client().post("/api/run", data={"kind": "task", "text": "x", "app": "alpha"})
    assert _wait_idle("alpha"), "clean run never released its slot"

    st = cockpit_state.get_state("alpha")
    assert not st["active"], "active must be cleared on the terminal outcome"
    assert st["last_msg"] == "", (
        f"the transient 'stopping…' note lingered after a clean run — last_msg={st['last_msg']!r}; "
        "a finished run must never linger as 'Working / stopping'"
    )


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            setup()
            fn()
            print(f"ok  {name}")
    print("all eu104_run_error_survives tests passed")
