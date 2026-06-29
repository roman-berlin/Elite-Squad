"""EU-120 — launchctl daemon stop handling (engineer 3's slice).

This harness tests the _stop_launchd_daemon() helper that durably stops a launchd
KeepAlive daemon using launchctl bootout/unload:

  (a) Returns False on non-macOS platforms (no-op)
  (b) Returns False when the plist file doesn't exist (already uninstalled)
  (c) Calls launchctl bootout on modern macOS (Darwin)
  (d) Falls back to launchctl unload if bootout fails/doesn't exist
  (e) Best-effort: handles subprocess failures gracefully (returns False)
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
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

from orchestrator import autopilot

# ── result accumulator ───────────────────────────────────────────────────────────
results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    """Accumulate without raising so every case runs; summary at end."""
    results.append((name, bool(cond), str(detail)))


def summarize() -> None:
    """Print the accumulated results and exit with the right code."""
    failed = [msg for msg, ok, _ in results if not ok]
    if failed:
        print(f"\n❌ EU-120 launchctl stop — FAILED ({len(failed)}/{len(results)})")
        for msg in failed:
            print(f"  - {msg}")
        sys.exit(1)
    else:
        print(f"\n✅ EU-120 launchctl stop — PASSED ({len(results)}/{len(results)})")
        sys.exit(0)


# =============================================================================
# (a) Non-macOS platform → returns False (no-op)
# =============================================================================


def _test_non_macos_returns_false() -> None:
    """On non-Darwin platforms, the function returns False without calling launchctl."""
    import platform as platform_module
    with patch.object(platform_module, "system", return_value="Linux"):
        result = autopilot._stop_launchd_daemon()
        chk("non-macOS: returns False", result is False, f"got {result}")


_test_non_macos_returns_false()


# =============================================================================
# (b) Plist file missing → returns False (nothing to unload)
# =============================================================================


def _test_missing_plist_returns_false() -> None:
    """When the plist file doesn't exist, returns False (already uninstalled)."""
    import platform as platform_module
    # Mock Darwin but with a missing plist
    with patch.object(platform_module, "system", return_value="Darwin"), \
         patch("pathlib.Path.home", return_value=Path("/tmp/does-not-exist")):
        # Create a temp directory that doesn't have the plist
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir)
            with patch("pathlib.Path.home", return_value=fake_home):
                result = autopilot._stop_launchd_daemon()
                chk("missing plist: returns False", result is False, f"got {result}")


_test_missing_plist_returns_false()


# =============================================================================
# (c) Modern macOS: calls launchctl bootout
# =============================================================================


def _test_bootout_called_on_modern_macos() -> None:
    """On modern macOS, launchctl bootout is called (and succeeds)."""
    import platform as platform_module
    # Create a fake plist file
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_home = Path(tmpdir)
        plist_dir = fake_home / "Library" / "LaunchAgents"
        plist_dir.mkdir(parents=True, exist_ok=True)
        plist_path = plist_dir / "com.romanberlin.general.autopilot.plist"
        plist_path.write_text("test plist content")

        with patch.object(platform_module, "system", return_value="Darwin"), \
             patch("pathlib.Path.home", return_value=fake_home), \
             patch("os.getuid", return_value=501), \
             patch("subprocess.run") as mock_run:
            # Mock successful bootout call
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            result = autopilot._stop_launchd_daemon()

            # Verify bootout was called
            bootout_calls = [
                call for call in mock_run.call_args_list
                if len(call[0]) > 0 and "bootout" in call[0][0]
            ]
            chk("modern macOS: bootout called", len(bootout_calls) > 0,
                f"bootout calls: {len(bootout_calls)}")
            chk("modern macOS: returns True on success", result is True, f"got {result}")


_test_bootout_called_on_modern_macos()


# =============================================================================
# (d) Fallback to unload if bootout fails
# =============================================================================


def _test_fallback_to_unload_on_bootout_failure() -> None:
    """If bootout fails/raises, falls back to launchctl unload."""
    import platform as platform_module
    # Create a fake plist file
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_home = Path(tmpdir)
        plist_dir = fake_home / "Library" / "LaunchAgents"
        plist_dir.mkdir(parents=True, exist_ok=True)
        plist_path = plist_dir / "com.romanberlin.general.autopilot.plist"
        plist_path.write_text("test plist content")

        with patch.object(platform_module, "system", return_value="Darwin"), \
             patch("pathlib.Path.home", return_value=fake_home), \
             patch("os.getuid", return_value=501), \
             patch("subprocess.run") as mock_run:
            # First call (bootout) fails with OSError, second call (unload) succeeds
            mock_run.side_effect = [
                OSError("bootout not available"),  # bootout fails
                MagicMock(returncode=0, stdout="", stderr=""),  # unload succeeds
            ]

            result = autopilot._stop_launchd_daemon()

            # Verify both bootout and unload were attempted
            chk("fallback: unload attempted after bootout failure",
                len(mock_run.call_args_list) >= 2,
                f"total calls: {len(mock_run.call_args_list)}")
            chk("fallback: returns True on successful unload", result is True, f"got {result}")


_test_fallback_to_unload_on_bootout_failure()


# =============================================================================
# (e) Best-effort: handles subprocess failures gracefully
# =============================================================================


def _test_best_effort_on_complete_failure() -> None:
    """If both bootout and unload fail, returns False (doesn't raise)."""
    import platform as platform_module
    # Create a fake plist file
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_home = Path(tmpdir)
        plist_dir = fake_home / "Library" / "LaunchAgents"
        plist_dir.mkdir(parents=True, exist_ok=True)
        plist_path = plist_dir / "com.romanberlin.general.autopilot.plist"
        plist_path.write_text("test plist content")

        with patch.object(platform_module, "system", return_value="Darwin"), \
             patch("pathlib.Path.home", return_value=fake_home), \
             patch("os.getuid", return_value=501), \
             patch("subprocess.run") as mock_run:
            # Both bootout and unload fail
            mock_run.side_effect = [
                OSError("bootout failed"),
                TimeoutError("unload timed out"),
            ]

            # Should not raise, just return False
            result = autopilot._stop_launchd_daemon()
            chk("best-effort: returns False on total failure", result is False, f"got {result}")


_test_best_effort_on_complete_failure()


# =============================================================================
# (f) Verify correct plist path and command structure
# =============================================================================


def _test_correct_plist_path_and_command() -> None:
    """Uses the correct plist path and bootout/unload command format."""
    import platform as platform_module
    # Create a fake plist file
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_home = Path(tmpdir)
        plist_dir = fake_home / "Library" / "LaunchAgents"
        plist_dir.mkdir(parents=True, exist_ok=True)
        plist_path = plist_dir / "com.romanberlin.general.autopilot.plist"
        plist_path.write_text("test plist content")

        with patch.object(platform_module, "system", return_value="Darwin"), \
             patch("pathlib.Path.home", return_value=fake_home), \
             patch("os.getuid", return_value=501), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            autopilot._stop_launchd_daemon()

            # Check the bootout call structure
            bootout_call = mock_run.call_args_list[0]
            cmd = bootout_call[0][0]  # First positional arg is the command list
            chk("bootout command: starts with launchctl", cmd[0] == "launchctl", f"got {cmd}")
            chk("bootout command: contains 'bootout'", "bootout" in cmd, f"got {cmd}")
            chk("bootout command: contains service label", "com.romanberlin.general.autopilot" in cmd[2],
                f"got {cmd}")


_test_correct_plist_path_and_command()


# =============================================================================
# Summary
# =============================================================================

summarize()
