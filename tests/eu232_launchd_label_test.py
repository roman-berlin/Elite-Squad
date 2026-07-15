"""EU-232 — autopilot stop-control correctness.

Three independent guards, all behavioural where practical:

  (1) LABEL EQUALITY: scripts/install-mac-autopilot-daemon.sh and orchestrator/autopilot.py must
      resolve to the SAME launchd label. The installer derives its LABEL= from
      ``orchestrator.autopilot.LAUNCHD_LABEL`` at install time (a shell-out to python3) — this guard
      actually RUNS that derivation and diffs the result against the live Python constant, so it goes
      RED the moment the two fall out of step again (the bug this ticket fixes: the installer wrote
      "com.roman.general.autopilot-keepalive" but the stopper targeted the stale
      "com.romanberlin.general.autopilot", so Stop always silently no-opped).

  (2) STOP VERIFICATION: _stop_launchd_daemon() must not trust launchctl's optimistic exit code — it
      has to poll daemon_running() and return True ONLY once the daemon is confirmed gone, False if
      it's still alive when the poll deadline passes.

  (3) REASONED STOPS: every autopilot_stop audit event must carry a non-empty reason= distinguishing
      cockpit-stop / sigterm / once-complete / plan-limit / budget / keyboard-interrupt /
      exception:<type> — never a bare {ts, event}.

  (4) The systemd unit installer must add Wants=network-online.target beside its existing After=.

No network, no real launchd, no real SDK — the SDK/requests modules are stubbed and _stop_launchd_daemon's
subprocess.run + daemon_running() are patched per case.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
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
req.Session = lambda: types.SimpleNamespace(auth=None, headers={}, update=lambda *a, **k: None)
sys.modules.setdefault("requests", req)

sys.path.insert(0, ".")

from orchestrator import autopilot
from orchestrator.config import Config

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts" / "install-mac-autopilot-daemon.sh"
SYSTEMD_INSTALLER = ROOT / "scripts" / "install-service.sh"

# ── result accumulator ───────────────────────────────────────────────────────────
results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


def _events(path) -> list[dict]:
    try:
        return [json.loads(ln) for ln in Path(path).read_text().splitlines() if ln.strip()]
    except OSError:
        return []


def _stop_reasons(path) -> list:
    return [e.get("reason") for e in _events(path) if e.get("event") == "autopilot_stop"]


# =============================================================================
# (1) LABEL EQUALITY — installer and stopper can never drift apart
# =============================================================================

chk("orchestrator.autopilot.LAUNCHD_LABEL is the live installed keepalive label",
    autopilot.LAUNCHD_LABEL == "com.roman.general.autopilot-keepalive",
    autopilot.LAUNCHD_LABEL)

_installer_text = INSTALLER.read_text(encoding="utf-8")
_label_line_m = re.search(r"^LABEL=.*$", _installer_text, re.MULTILINE)
chk("installer has a LABEL= assignment", _label_line_m is not None)
_label_line = _label_line_m.group(0) if _label_line_m else ""
chk("installer's LABEL= derives from orchestrator.autopilot.LAUNCHD_LABEL — not an independent literal "
    "(this is what makes drift structurally impossible, not just today's string match)",
    "LAUNCHD_LABEL" in _label_line and "orchestrator.autopilot" in _label_line,
    _label_line)

# Actually RUN the installer's LABEL derivation (env override unset) and diff it against the live
# Python constant — the guard that goes RED on real drift, not merely on textual presence.
_env = dict(os.environ)
_env.pop("GENERAL_LAUNCHD_LABEL", None)
# The LABEL line shells out to bare `python3`. Put the suite's own interpreter first on PATH so
# that resolves to an interpreter that can import the orchestrator: run un-activated from .venv,
# system python3 lacks claude_agent_sdk and this guard reddened on ModuleNotFoundError instead of
# the drift it exists to catch (red on every local run_all, green on CI where deps are global).
_env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + _env.get("PATH", "")
_proc = subprocess.run(
    ["bash", "-c", f'HERE={str(ROOT)!r}; {_label_line}; printf "%s" "$LABEL"'],
    capture_output=True, text=True, timeout=15, cwd=str(ROOT), env=_env,
)
_resolved = _proc.stdout.strip()
chk("installer's LABEL= resolves to EXACTLY orchestrator.autopilot.LAUNCHD_LABEL",
    _resolved == autopilot.LAUNCHD_LABEL,
    f"installer resolved {_resolved!r}, python constant is {autopilot.LAUNCHD_LABEL!r} "
    f"(rc={_proc.returncode}, stderr={_proc.stderr.strip()!r})")

# The stopper's plist path and bootout target must be built from the SAME constant (structural check
# on the live source, so a future edit that hardcodes a NEW literal in _stop_launchd_daemon — instead
# of reading LAUNCHD_LABEL — still gets caught even though the string happens to match today).
import inspect  # noqa: E402
_stopper_src = inspect.getsource(autopilot._stop_launchd_daemon)
chk("_stop_launchd_daemon builds the plist path from LAUNCHD_LABEL",
    "LAUNCHD_LABEL" in _stopper_src and '"Library" / "LaunchAgents"' in _stopper_src, "")
chk("_stop_launchd_daemon builds the bootout target from LAUNCHD_LABEL",
    re.search(r"bootout.*LAUNCHD_LABEL|LAUNCHD_LABEL.*bootout", _stopper_src, re.DOTALL) is not None
    or ("gui/{os.getuid()}/{LAUNCHD_LABEL}" in _stopper_src), "")


# =============================================================================
# (2) STOP VERIFICATION — poll daemon_running(), don't trust launchctl's exit code
# =============================================================================

def _with_fake_plist(fn):
    """Run fn() inside a tmpdir with a fake plist at LAUNCHD_LABEL's expected path, Darwin mocked."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_home = Path(tmpdir)
        plist_dir = fake_home / "Library" / "LaunchAgents"
        plist_dir.mkdir(parents=True, exist_ok=True)
        (plist_dir / f"{autopilot.LAUNCHD_LABEL}.plist").write_text("test plist content")
        with patch("platform.system", return_value="Darwin"), \
             patch("pathlib.Path.home", return_value=fake_home), \
             patch("os.getuid", return_value=501):
            fn()


def _test_returns_true_once_daemon_confirmed_gone() -> None:
    """bootout succeeds AND daemon_running() is already False → True immediately (no polling delay)."""
    def _inner():
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")), \
             patch("orchestrator.autopilot.daemon_running", return_value=False):
            t0 = time.monotonic()
            result = autopilot._stop_launchd_daemon(poll_timeout=2.0, poll_interval=0.05)
            elapsed = time.monotonic() - t0
            chk("already-gone: returns True", result is True, f"got {result}")
            chk("already-gone: no polling delay (< 1s)", elapsed < 1.0, f"elapsed={elapsed:.2f}s")
    _with_fake_plist(_inner)


_test_returns_true_once_daemon_confirmed_gone()


def _test_returns_true_after_polling_flips() -> None:
    """daemon_running() is True on the first couple polls, then False — must poll, then return True."""
    def _inner():
        calls = {"n": 0}

        def _daemon_running():
            calls["n"] += 1
            return calls["n"] < 3   # alive for the first 2 checks, gone on the 3rd

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")), \
             patch("orchestrator.autopilot.daemon_running", side_effect=_daemon_running):
            result = autopilot._stop_launchd_daemon(poll_timeout=5.0, poll_interval=0.01)
            chk("polls-then-gone: returns True", result is True, f"got {result}")
            chk("polls-then-gone: actually polled more than once", calls["n"] >= 3, f"calls={calls['n']}")
    _with_fake_plist(_inner)


_test_returns_true_after_polling_flips()


def _test_returns_false_when_daemon_never_exits() -> None:
    """daemon_running() stays True through the whole poll window → False once the deadline passes."""
    def _inner():
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")), \
             patch("orchestrator.autopilot.daemon_running", return_value=True):
            result = autopilot._stop_launchd_daemon(poll_timeout=0.1, poll_interval=0.02)
            chk("never-exits: returns False after the poll deadline", result is False, f"got {result}")
    _with_fake_plist(_inner)


_test_returns_false_when_daemon_never_exits()


def _test_no_poll_when_launchctl_totally_fails() -> None:
    """Both bootout and unload raise → False immediately, daemon_running() never even consulted."""
    def _inner():
        with patch("subprocess.run", side_effect=[OSError("boom"), OSError("boom2")]), \
             patch("orchestrator.autopilot.daemon_running") as mock_dr:
            result = autopilot._stop_launchd_daemon(poll_timeout=5.0, poll_interval=0.01)
            chk("total launchctl failure: returns False", result is False, f"got {result}")
            chk("total launchctl failure: daemon_running never polled (nothing to verify)",
                mock_dr.call_count == 0, f"calls={mock_dr.call_count}")
    _with_fake_plist(_inner)


_test_no_poll_when_launchctl_totally_fails()


# =============================================================================
# (3) REASONED STOPS — every autopilot_stop carries a distinguishing reason=
# =============================================================================

def _quiet_stubs() -> None:
    """Common offline-safe stubs shared by every reason-coverage case below."""
    autopilot.usage.budget_status = lambda c: {"over": False, "alert": False, "used": 0, "cap": 1, "pct": 0.0}
    autopilot.usage.plan_limit_hit = lambda c: {"hit": False, "over_limits": []}
    autopilot.usage.graceful_stop_check = lambda c: {"should_stop": False}
    autopilot.usage.pre_flight_check = lambda c: {"should_skip": False}
    autopilot.intake.from_drain = lambda c, app, n: []
    autopilot.notify.configured = lambda: False
    autopilot.notify.send = lambda *a, **k: None
    autopilot.notify.plan_limit_alert = lambda *a, **k: True
    autopilot.notify.dual_low_watermark_alert = lambda *a, **k: True
    autopilot._sleep = lambda seconds, stop_event=None: None


def _new_cfg(tmp_dir: Path, name: str) -> "Config":
    return Config(apps=[], audit_path=str(tmp_dir / f"{name}.jsonl"))


_tmp = Path(tempfile.mkdtemp())


def _test_reason_once_complete() -> None:
    """A normal --once run with an empty (clear) queue records reason='once-complete'."""
    _quiet_stubs()
    cfg = _new_cfg(_tmp, "once_complete")
    asyncio.run(autopilot.autopilot(cfg, once=True))
    reasons = _stop_reasons(cfg.audit_path)
    chk("once-complete: exactly one autopilot_stop", len(reasons) == 1, reasons)
    chk("once-complete: reason == 'once-complete'", reasons == ["once-complete"], reasons)


_test_reason_once_complete()


def _test_reason_cockpit_stop() -> None:
    """stop_event set externally (the cockpit Stop/Drain toggle) before the loop ever checks it →
    reason='cockpit-stop', distinct from the sigterm path even though both use the same Event."""
    _quiet_stubs()
    cfg = _new_cfg(_tmp, "cockpit_stop")
    ev = threading.Event()
    ev.set()   # already stopped before autopilot() even starts its loop
    asyncio.run(autopilot.autopilot(cfg, once=False, interval=1, stop_event=ev))
    reasons = _stop_reasons(cfg.audit_path)
    chk("cockpit-stop: exactly one autopilot_stop", len(reasons) == 1, reasons)
    chk("cockpit-stop: reason == 'cockpit-stop'", reasons == ["cockpit-stop"], reasons)


_test_reason_cockpit_stop()


def _test_reason_sigterm() -> None:
    """A real SIGTERM (mirrored here by invoking the ACTUAL registered handler, captured off
    signal.signal — not a reimplementation) sets reason='sigterm' BEFORE the loop's own stop_event
    check would otherwise default it to 'cockpit-stop'."""
    _quiet_stubs()
    cfg = _new_cfg(_tmp, "sigterm")
    captured: dict = {}
    _orig_signal = signal.signal

    def _capture(sig, handler):
        if sig == signal.SIGTERM:
            captured["handler"] = handler
        return _orig_signal(sig, handler)

    def _sleep_fires_handler(seconds, stop_event=None):
        # _sleep is only reached once the loop is fully up and the handler is registered (it's the
        # very first blocking point after setup) — invoke the REAL captured handler exactly as a
        # delivered SIGTERM would, without risking process termination via a real os.kill.
        if "handler" in captured:
            captured["handler"](signal.SIGTERM, None)

    signal.signal = _capture
    autopilot._sleep = _sleep_fires_handler
    try:
        asyncio.run(autopilot.autopilot(cfg, once=False, interval=1))
    finally:
        signal.signal = _orig_signal
        autopilot._sleep = lambda seconds, stop_event=None: None

    reasons = _stop_reasons(cfg.audit_path)
    chk("sigterm: exactly one autopilot_stop", len(reasons) == 1, reasons)
    chk("sigterm: reason == 'sigterm'", reasons == ["sigterm"], reasons)


_test_reason_sigterm()


def _test_reason_budget() -> None:
    """Daily token budget exceeded, --once → reason='budget'."""
    _quiet_stubs()
    autopilot.usage.budget_status = lambda c: {"over": True, "alert": False, "used": 999, "cap": 1, "pct": 999.0}
    cfg = _new_cfg(_tmp, "budget")
    asyncio.run(autopilot.autopilot(cfg, once=True))
    reasons = _stop_reasons(cfg.audit_path)
    chk("budget: exactly one autopilot_stop", len(reasons) == 1, reasons)
    chk("budget: reason == 'budget'", reasons == ["budget"], reasons)


_test_reason_budget()


def _test_reason_plan_limit() -> None:
    """Claude plan (session/weekly) limit hit, --once → reason='plan-limit'."""
    _quiet_stubs()
    autopilot.usage.plan_limit_hit = lambda c: {
        "hit": True,
        "over_limits": [{"key": "session", "label": "Session", "resets_in": "1h", "resets_at": None}],
    }
    cfg = _new_cfg(_tmp, "plan_limit")
    asyncio.run(autopilot.autopilot(cfg, once=True))
    reasons = _stop_reasons(cfg.audit_path)
    chk("plan-limit: exactly one autopilot_stop", len(reasons) == 1, reasons)
    chk("plan-limit: reason == 'plan-limit'", reasons == ["plan-limit"], reasons)


_test_reason_plan_limit()


def _test_reason_keyboard_interrupt() -> None:
    """Ctrl-C (KeyboardInterrupt) mid-cycle → reason='keyboard-interrupt', exception swallowed
    (autopilot() must NOT propagate a KeyboardInterrupt raised from inside its own loop)."""
    _quiet_stubs()
    autopilot.intake.from_drain = lambda c, app, n: (_ for _ in ()).throw(KeyboardInterrupt())
    cfg = _new_cfg(_tmp, "kbd_interrupt")
    raised = False
    try:
        asyncio.run(autopilot.autopilot(cfg, once=True))
    except KeyboardInterrupt:
        raised = True
    chk("keyboard-interrupt: NOT propagated out of autopilot()", not raised)
    reasons = _stop_reasons(cfg.audit_path)
    chk("keyboard-interrupt: exactly one autopilot_stop", len(reasons) == 1, reasons)
    chk("keyboard-interrupt: reason == 'keyboard-interrupt'", reasons == ["keyboard-interrupt"], reasons)


_test_reason_keyboard_interrupt()


def _test_reason_exception() -> None:
    """An unexpected exception mid-cycle → reason='exception:<TypeName>', AND still propagates out
    (unlike KeyboardInterrupt) so the caller/daemon supervisor can see the failure."""
    _quiet_stubs()
    autopilot.intake.from_drain = lambda c, app, n: (_ for _ in ()).throw(RuntimeError("boom"))
    cfg = _new_cfg(_tmp, "exception")
    raised = None
    try:
        asyncio.run(autopilot.autopilot(cfg, once=True))
    except RuntimeError as exc:
        raised = exc
    chk("exception: DOES propagate out of autopilot() (unlike KeyboardInterrupt)", raised is not None)
    reasons = _stop_reasons(cfg.audit_path)
    chk("exception: exactly one autopilot_stop", len(reasons) == 1, reasons)
    chk("exception: reason == 'exception:RuntimeError'", reasons == ["exception:RuntimeError"], reasons)


_test_reason_exception()

# Restore module state so nothing here leaks into a later harness run in the same suite.
_quiet_stubs()


# =============================================================================
# (4) systemd unit: Wants=network-online.target beside the existing After=
# =============================================================================

_svc_text = SYSTEMD_INSTALLER.read_text(encoding="utf-8")
chk("install-service.sh still has After=network-online.target", "After=network-online.target" in _svc_text)
chk("install-service.sh adds Wants=network-online.target", "Wants=network-online.target" in _svc_text)
# Wants= must sit in the [Unit] section, beside After= (not accidentally under [Service]/[Install]).
_unit_section_m = re.search(r"\[Unit\](.*?)\n\[", _svc_text, re.DOTALL)
chk("Wants=network-online.target lives in the [Unit] section",
    _unit_section_m is not None and "Wants=network-online.target" in _unit_section_m.group(1),
    _unit_section_m.group(1) if _unit_section_m else "")


# =============================================================================
# Summary
# =============================================================================

print("\n=============== EU-232 STOP-CONTROL QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results) - passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
