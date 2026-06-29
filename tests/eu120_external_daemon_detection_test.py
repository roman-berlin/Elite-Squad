"""EU-120 — external daemon detection in autopilot status.

This harness tests the external daemon detection in cockpit_state.get_autopilot_status():

  (a) When no daemon is running, external=False and on is determined by autopilot_on
  (b) When an external daemon is running (different PID), external=True and on=True
      even if this cockpit's autopilot_on flag is False
  (c) When this cockpit's own autopilot is running (same PID), external=False
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from unittest.mock import patch

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

from orchestrator import cockpit_state
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
        print(f"\n❌ EU-120 — FAILED ({len(failed)}/{len(results)})")
        for msg in failed:
            print(f"  - {msg}")
        sys.exit(1)
    else:
        print(f"\n✅ EU-120 — PASSED ({len(results)}/{len(results)})")
        sys.exit(0)


# =============================================================================
# (a) No daemon running → external=False
# =============================================================================

def _test_no_daemon_external_false() -> None:
    """When no daemon is running, external must be False."""
    cockpit_state.reset_run_state()
    st = cockpit_state.get_state("automatixy")
    st["autopilot_on"] = False

    with patch("orchestrator.autopilot.daemon_running", return_value=False), \
         patch("orchestrator.autopilot.daemon_is_external", return_value=False):
        s = cockpit_state.get_autopilot_status("automatixy")
        chk("no daemon: external=False", s["external"] is False, str(s))
        chk("no daemon, autopilot_off: on=False", s["on"] is False, str(s))


_test_no_daemon_external_false()


# =============================================================================
# (b) External daemon running → external=True, on=True
# =============================================================================

def _test_external_daemon_detected() -> None:
    """When an external daemon is running (different PID), external=True and on=True."""
    cockpit_state.reset_run_state()
    st = cockpit_state.get_state("automatixy")
    st["autopilot_on"] = False  # cockpit's own flag is False

    with patch("orchestrator.autopilot.daemon_running", return_value=True), \
         patch("orchestrator.autopilot.daemon_is_external", return_value=True):
        s = cockpit_state.get_autopilot_status("automatixy")
        chk("external daemon: external=True", s["external"] is True, str(s))
        chk("external daemon: on=True even though autopilot_on=False", s["on"] is True, str(s))


_test_external_daemon_detected()


# =============================================================================
# (c) Internal autopilot running → external=False
# =============================================================================

def _test_internal_autopilot_external_false() -> None:
    """When this cockpit's own autopilot is running, external=False."""
    cockpit_state.reset_run_state()
    st = cockpit_state.get_state("automatixy")
    st["autopilot_on"] = True  # cockpit's own autopilot

    # daemon_is_external returns False when PID matches this process
    with patch("orchestrator.autopilot.daemon_running", return_value=True), \
         patch("orchestrator.autopilot.daemon_is_external", return_value=False):
        s = cockpit_state.get_autopilot_status("automatixy")
        chk("internal autopilot: external=False", s["external"] is False, str(s))
        chk("internal autopilot: on=True", s["on"] is True, str(s))


_test_internal_autopilot_external_false()


# =============================================================================
# (d) External daemon + stop_event behavior
# =============================================================================

def _test_external_daemon_stopping_behavior() -> None:
    """External daemon detection should respect stop_event behavior."""
    cockpit_state.reset_run_state()
    st = cockpit_state.get_state("automatixy")
    st["autopilot_on"] = False
    ev = threading.Event()
    ev.set()
    st["stop_event"] = ev

    with patch("orchestrator.autopilot.daemon_running", return_value=True), \
         patch("orchestrator.autopilot.daemon_is_external", return_value=True):
        s = cockpit_state.get_autopilot_status("automatixy")
        chk("external daemon with stop_event: stopping=True", s["stopping"] is True, str(s))
        chk("external daemon with stop_event: on=True", s["on"] is True, str(s))
        chk("external daemon with stop_event: external=True", s["external"] is True, str(s))


_test_external_daemon_stopping_behavior()


# =============================================================================
# Summary
# =============================================================================

summarize()
