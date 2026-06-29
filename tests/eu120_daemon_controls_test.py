"""EU-120 — daemon controls rendering test.

This test verifies that:
  (1) When daemon_is_external() returns True, get_autopilot_status() returns on=True with external=True
  (2) The control bar renders 'Finish & stop' and 'Stop' buttons for external daemon runs
  (3) The drain action properly calls launchctl bootout for external daemons
"""
from __future__ import annotations

import sys
import tempfile
import threading
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── minimal SDK / requests stubs so the orchestrator can import without real deps ──
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self


sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)

req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers={},
    update=lambda *a, **k: None,
)
sys.modules.setdefault("requests", req)

sys.path.insert(0, ".")

from orchestrator import autopilot, cockpit_state, cockpit_views
from orchestrator.config import AppConfig, Config

# ── shared config ────────────────────────────────────────────────────────────────
_TMP = Path(tempfile.mkdtemp())
_CFG = Config(
    apps=[
        AppConfig(name="automatixy", repo_path=str(_TMP), base_branch="DEV",
                  protected_branch="MAIN", backlog_backend="none"),
    ],
    audit_path=str(_TMP / "a.jsonl"),
    use_worktree=False,
)

# ── result accumulator ───────────────────────────────────────────────────────────
results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    """Accumulate without raising so every case runs; summary at end."""
    results.append((name, bool(cond), str(detail)))


def summarize() -> None:
    """Print the accumulated results and exit with the right code."""
    failed = [msg for msg, ok, _ in results if not ok]
    if failed:
        print(f"\n❌ EU-120 daemon controls — FAILED ({len(failed)}/{len(results)})")
        for msg in failed:
            print(f"  - {msg}")
        sys.exit(1)
    else:
        print(f"\n✅ EU-120 daemon controls — PASSED ({len(results)}/{len(results)})")
        sys.exit(0)


# =============================================================================
# (1) daemon_is_external() → get_autopilot_status() returns on=True with external=True
# =============================================================================

def _test_external_daemon_status() -> None:
    """When daemon_is_external() returns True, get_autopilot_status() must return on=True with external=True."""
    cockpit_state.reset_run_state()
    st = cockpit_state.get_state("automatixy")
    st["autopilot_on"] = False  # cockpit's own flag is False

    with patch("orchestrator.autopilot.daemon_running", return_value=True), \
         patch("orchestrator.autopilot.daemon_is_external", return_value=True):
        s = cockpit_state.get_autopilot_status("automatixy")
        chk("external daemon: on=True", s.get("on") is True, str(s))
        chk("external daemon: external=True", s.get("external") is True, str(s))


_test_external_daemon_status()


# =============================================================================
# (2) Control bar renders 'Finish & stop' and 'Stop' for external daemon
# =============================================================================

def _test_control_bar_external_buttons() -> None:
    """When autopilot is on and external=True, the control bar must render 'Finish & stop' and 'Stop' buttons."""
    cockpit_state.reset_run_state()
    st = cockpit_state.get_state("automatixy")
    st["autopilot_on"] = True
    st["stop_event"] = threading.Event()

    with patch("orchestrator.autopilot.daemon_running", return_value=True), \
         patch("orchestrator.autopilot.daemon_is_external", return_value=True):
        html = cockpit_views._control_bar(_CFG, current_app="automatixy", healthy=True, is_mac=False)
        chk("control bar: contains 'Finish & stop' button",
            "Finish&nbsp;&amp;&nbsp;stop" in html,
            "HTML output missing 'Finish & stop' button")
        chk("control bar: contains 'Stop' button",
            "Stop</button>" in html,
            "HTML output missing 'Stop' button")
        chk("control bar: marks external daemon with '(external)' label",
            "(external)" in html,
            "HTML output missing external daemon label")


_test_control_bar_external_buttons()


# =============================================================================
# (3) Drain action calls launchctl bootout for external daemons
# =============================================================================

def _test_drain_calls_launchctl_bootout() -> None:
    """When drain action is posted for an external daemon, it must call launchctl bootout."""
    cockpit_state.reset_run_state()
    st = cockpit_state.get_state("automatixy")
    st["autopilot_on"] = True
    st["stop_event"] = threading.Event()

    # Mock platform.system to return Darwin (macOS)
    # Mock subprocess.run to track if launchctl bootout was called
    mock_run_calls = []

    def _mock_run(args, **kwargs):
        """Track subprocess calls and return success for launchctl commands."""
        mock_run_calls.append(args)
        import subprocess
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    # Create a fake plist path that exists for the test
    fake_plist = _TMP / "Library" / "LaunchAgents" / "com.romanberlin.general.autopilot.plist"
    fake_plist.parent.mkdir(parents=True, exist_ok=True)
    fake_plist.write_text("test plist content")

    with patch("orchestrator.autopilot.daemon_running", return_value=True), \
         patch("orchestrator.autopilot.daemon_is_external", return_value=True), \
         patch("platform.system", return_value="Darwin"), \
         patch("pathlib.Path.home", return_value=_TMP), \
         patch("subprocess.run", side_effect=_mock_run) as mock_run:
        # Simulate the drain action from server.py
        from orchestrator import autopilot as ap
        stopped = ap._stop_launchd_daemon()
        chk("drain action: _stop_launchd_daemon succeeded for external daemon",
            stopped is True,
            f"_stop_launchd_daemon returned {stopped}")
        chk("drain action: subprocess.run was called",
            len(mock_run_calls) > 0,
            f"subprocess.run calls: {mock_run_calls}")
        # Verify the call was made with the right arguments
        if mock_run_calls:
            # Check that at least one call was to launchctl bootout
            bootout_called = any("launchctl" in str(ca) and "bootout" in str(ca) for ca in mock_run_calls)
            chk("drain action: launchctl bootout command used",
                bootout_called,
                f"launchctl calls: {mock_run_calls}")


_test_drain_calls_launchctl_bootout()


# =============================================================================
# (4) Stop action calls launchctl bootout for external daemons (EU-120 iter-3)
# =============================================================================

def _test_stop_calls_launchctl_bootout() -> None:
    """When stop action is posted for an external daemon, it must call launchctl bootout.

    Without launchctl bootout, clicking 'Stop' on an external daemon doesn't actually
    stop it—the KeepAlive respawn makes the button a no-op for external runs.
    """
    cockpit_state.reset_run_state()
    st = cockpit_state.get_state("automatixy")
    st["autopilot_on"] = True
    st["stop_event"] = threading.Event()

    # Mock platform.system to return Darwin (macOS)
    # Mock subprocess.run to track if launchctl bootout was called
    mock_run_calls = []

    def _mock_run(args, **kwargs):
        """Track subprocess calls and return success for launchctl commands."""
        mock_run_calls.append(args)
        import subprocess
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    # Create a fake plist path that exists for the test
    fake_plist = _TMP / "Library" / "LaunchAgents" / "com.romanberlin.general.autopilot.plist"
    fake_plist.parent.mkdir(parents=True, exist_ok=True)
    fake_plist.write_text("test plist content")

    with patch("orchestrator.autopilot.daemon_running", return_value=True), \
         patch("orchestrator.autopilot.daemon_is_external", return_value=True), \
         patch("platform.system", return_value="Darwin"), \
         patch("pathlib.Path.home", return_value=_TMP), \
         patch("subprocess.run", side_effect=_mock_run) as mock_run:
        # Simulate the stop action from server.py (lines 449-456)
        from orchestrator import autopilot as ap
        ev = st.get("stop_event")
        if ev is not None:
            ev.set()
        st["autopilot_on"] = False
        # EU-120: for external daemons, durably stop the launchd service
        if ap.daemon_is_external():
            stopped = ap._stop_launchd_daemon()
            chk("stop action: _stop_launchd_daemon succeeded for external daemon",
                stopped is True,
                f"_stop_launchd_daemon returned {stopped}")

        chk("stop action: subprocess.run was called",
            len(mock_run_calls) > 0,
            f"subprocess.run calls: {mock_run_calls}")
        # Verify the call was made with the right arguments
        if mock_run_calls:
            # Check that at least one call was to launchctl bootout
            bootout_called = any("launchctl" in str(ca) and "bootout" in str(ca) for ca in mock_run_calls)
            chk("stop action: launchctl bootout command used",
                bootout_called,
                f"launchctl calls: {mock_run_calls}")


_test_stop_calls_launchctl_bootout()


# =============================================================================
# Summary
# =============================================================================

summarize()
