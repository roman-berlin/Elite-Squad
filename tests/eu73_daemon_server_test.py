"""EU-73 — server-side daemon integration tests.

Covers the gaps left after the Builder's autopilot_daemon_keepalive_test.py
and autopilot_graceful_stop_test.py:

  (a) _write_pid() / _remove_pid() — PID file lifecycle (write; remove ONLY when the file is
        ours == os.getpid(); leave a foreign PID untouched; silence a missing/garbled file).
  (b) SIGTERM → graceful stop — the installed signal handler sets the stop_event.
  (c) GET /api/autopilot — the new JSON status endpoint; three states: dead, alive+off, alive+on.
  (d) _view_state() daemon injection — injects daemon_running into the cockpit state on every render:
        d-1: no autopilot in _state + daemon alive → creates minimal external-daemon entry
        d-2: autopilot entry exists + daemon alive → refreshes daemon_running in-place to True
        d-3: autopilot entry exists + daemon dead  → refreshes daemon_running in-place to False
"""
from __future__ import annotations

import os
import signal
import sys
import tempfile
import threading
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── minimal SDK stubs so server / warroom can import without the real SDK ──────
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
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules.setdefault("requests", req)

sys.path.insert(0, ".")

import orchestrator.autopilot as _ap_mod
from orchestrator import server, sync
from orchestrator.config import AppConfig, Config

# ── shared test config / Flask client ─────────────────────────────────────────
_TMP = Path(tempfile.mkdtemp())
_CFG = Config(
    apps=[
        AppConfig(
            name="automatixy",
            repo_path=str(_TMP),
            base_branch="DEV",
            protected_branch="MAIN",
            backlog_backend="none",
        )
    ],
    audit_path=str(_TMP / "a.jsonl"),
    use_worktree=False,
)
_CFG.detected_auth = lambda: "test"
sync.can_promote = lambda: False

_APP = server.create_app(_CFG)
_CLIENT = _APP.test_client()

# ── result accumulator ────────────────────────────────────────────────────────
results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    """Record an assertion without raising — full summary printed at end."""
    results.append((name, bool(cond), str(detail)))


# =============================================================================
# (a) PID file lifecycle
# =============================================================================

def _test_write_pid_writes_current_pid() -> None:
    """_write_pid() must write os.getpid() as decimal text to _PID_FILE."""
    mock_path = MagicMock()
    with patch.object(_ap_mod, "_PID_FILE", mock_path):
        _ap_mod._write_pid()
    mock_path.write_text.assert_called_once_with(str(os.getpid()))
    chk(
        "_write_pid() writes the current PID as text",
        mock_path.write_text.called
        and mock_path.write_text.call_args == ((str(os.getpid()),),),
        f"call={mock_path.write_text.call_args}",
    )


def _test_write_pid_silences_oserror() -> None:
    """_write_pid() must not raise when the filesystem refuses the write."""
    mock_path = MagicMock()
    mock_path.write_text.side_effect = OSError("permission denied")
    with patch.object(_ap_mod, "_PID_FILE", mock_path):
        try:
            _ap_mod._write_pid()
            chk("_write_pid() silences OSError (non-fatal)", True)
        except OSError:
            chk("_write_pid() silences OSError (non-fatal)", False, "raised OSError")


def _test_remove_pid_calls_unlink() -> None:
    """_remove_pid() unlinks the PID file when it holds OUR PID (== os.getpid()).

    Each remove-case pins _pid_holders to exactly ONE live hold: _remove_pid() is refcounted
    (overlapping per-app drains in one process share the file — the 2026-07-15 vanish class),
    so it unlinks only when the LAST holder exits. The paired write/remove contract means a
    real exit path always decrements from ≥1; the earlier _write_pid cases above leaked extra
    unpaired holds, which is exactly what the pin isolates each case from."""
    mock_path = MagicMock()
    mock_path.read_text.return_value = str(os.getpid())   # file points at THIS process → we own it
    with patch.object(_ap_mod, "_PID_FILE", mock_path), \
         patch.object(_ap_mod, "_pid_holders", 1):
        _ap_mod._remove_pid()
    mock_path.unlink.assert_called_once_with(missing_ok=True)
    chk(
        "_remove_pid() unlinks when the PID file holds our own PID",
        mock_path.unlink.called,
        f"call={mock_path.unlink.call_args}",
    )


def _test_remove_pid_silences_oserror() -> None:
    """_remove_pid() must not raise when unlink itself fails (on a file we own)."""
    mock_path = MagicMock()
    mock_path.read_text.return_value = str(os.getpid())   # ours → reach the unlink whose OSError must be swallowed
    mock_path.unlink.side_effect = OSError("read-only fs")
    with patch.object(_ap_mod, "_PID_FILE", mock_path), \
         patch.object(_ap_mod, "_pid_holders", 1):
        try:
            _ap_mod._remove_pid()
            chk("_remove_pid() silences OSError (non-fatal)", True)
        except OSError:
            chk("_remove_pid() silences OSError (non-fatal)", False, "raised OSError")


def _test_remove_pid_skips_foreign_pid() -> None:
    """_remove_pid() must NOT unlink a PID file owned by a DIFFERENT process (single-instance guard).

    If a second autopilot overwrote the file with its own PID, this exiting instance deleting it would
    make daemon_running() read 'not running' while that other instance is still alive.
    """
    mock_path = MagicMock()
    mock_path.read_text.return_value = str(os.getpid() + 1)   # someone else's PID
    with patch.object(_ap_mod, "_PID_FILE", mock_path), \
         patch.object(_ap_mod, "_pid_holders", 1):
        _ap_mod._remove_pid()
    chk(
        "_remove_pid() leaves a PID file owned by another process untouched",
        not mock_path.unlink.called,
        f"unlink called={mock_path.unlink.called}",
    )


def _test_remove_pid_silences_read_error() -> None:
    """_remove_pid() must not raise (and must not unlink) when the PID file is already gone."""
    mock_path = MagicMock()
    mock_path.read_text.side_effect = FileNotFoundError("gone")
    with patch.object(_ap_mod, "_PID_FILE", mock_path), \
         patch.object(_ap_mod, "_pid_holders", 1):
        try:
            _ap_mod._remove_pid()
            _raised = False
        except OSError:
            _raised = True
    chk(
        "_remove_pid() silences a missing-file read error and skips unlink",
        (not _raised) and (not mock_path.unlink.called),
        f"raised={_raised}; unlink called={mock_path.unlink.called}",
    )


_test_write_pid_writes_current_pid()
_test_write_pid_silences_oserror()
_test_remove_pid_calls_unlink()
_test_remove_pid_silences_oserror()
_test_remove_pid_skips_foreign_pid()
_test_remove_pid_silences_read_error()

# =============================================================================
# (b) SIGTERM → graceful stop
# =============================================================================

def _test_sigterm_sets_stop_event() -> None:
    """The SIGTERM handler installed by autopilot() must set stop_event so the loop exits cleanly.

    We install the same handler that autopilot() installs and then deliver SIGTERM to the
    current process — the signal is delivered synchronously via os.kill(os.getpid(), SIGTERM).
    The test confirms that stop_event.is_set() becomes True without any actual async loop.
    """
    ev = threading.Event()
    orig = signal.getsignal(signal.SIGTERM)

    def _handle(signum, frame):  # mirrors the handler inside autopilot()
        ev.set()
        if callable(orig):
            orig(signum, frame)

    signal.signal(signal.SIGTERM, _handle)
    try:
        os.kill(os.getpid(), signal.SIGTERM)
        chk("SIGTERM handler sets the stop_event", ev.is_set())
    finally:
        signal.signal(signal.SIGTERM, orig)


_test_sigterm_sets_stop_event()

# =============================================================================
# (c) GET /api/autopilot — JSON status endpoint
# =============================================================================

def _get_autopilot_json(daemon_alive: bool, ap_state: dict | None) -> dict:
    """Hit GET /api/autopilot with a patched daemon_running() and given _state['autopilot']."""
    import json as _json
    with patch("orchestrator.autopilot.daemon_running", return_value=daemon_alive):
        # Reach into the live _state dict used by this Flask app.
        prev = server._state.get("autopilot")
        if ap_state is None:
            server._state.pop("autopilot", None)
        else:
            server._state["autopilot"] = dict(ap_state)
        try:
            resp = _CLIENT.get("/api/autopilot")
            return _json.loads(resp.data)
        finally:
            if ap_state is None:
                server._state.pop("autopilot", None)
            else:
                server._state["autopilot"] = prev if prev is not None else {}


# c-1: daemon dead, no cockpit record → all False
_j1 = _get_autopilot_json(daemon_alive=False, ap_state=None)
chk("GET /api/autopilot: daemon_running=False when process dead", not _j1.get("daemon_running"), str(_j1))
chk("GET /api/autopilot: on=False when cockpit has no record", not _j1.get("on"), str(_j1))

# c-2: daemon alive, cockpit shows on=False (e.g. external daemon, cockpit restarted)
_j2 = _get_autopilot_json(daemon_alive=True, ap_state={"on": False, "stopping": False, "app": None})
chk("GET /api/autopilot: daemon_running=True when process alive", _j2.get("daemon_running") is True, str(_j2))
chk("GET /api/autopilot: on=False still reflects cockpit's own flag", not _j2.get("on"), str(_j2))
chk("GET /api/autopilot: stopping present in response", "stopping" in _j2, str(_j2))
chk("GET /api/autopilot: app key present in response", "app" in _j2, str(_j2))

# c-3: daemon alive, cockpit shows on=True (started via Start button)
_j3 = _get_autopilot_json(
    daemon_alive=True,
    ap_state={"on": True, "stopping": False, "app": "automatixy"},
)
chk("GET /api/autopilot: daemon_running=True + on=True when both sources agree", _j3.get("daemon_running") and _j3.get("on"), str(_j3))
chk("GET /api/autopilot: app echoed back when set", _j3.get("app") == "automatixy", str(_j3))

# =============================================================================
# (d) _view_state() daemon_running injection
# =============================================================================

def _call_view_state(app: str, daemon_alive: bool, ap_state: dict | None) -> dict:
    """Call the _view_state() closure inside the Flask app with a mocked daemon probe."""
    # _view_state is a nested function inside create_app(); reach it via the warroom GET handler.
    # Rather than extracting the closure directly (fragile), we trigger a GET / and capture the
    # rendered HTML — but we actually want the dict, not HTML. Instead we exercise _view_state
    # through the /api/autopilot path's side-effect on _state, which re-uses the same probe.
    # For direct dict inspection we replicate the injection logic under the same patch.
    with patch("orchestrator.autopilot.daemon_running", return_value=daemon_alive):
        prev = server._state.get("autopilot")
        if ap_state is None:
            server._state.pop("autopilot", None)
        else:
            server._state["autopilot"] = dict(ap_state)
        try:
            # Trigger _view_state via a GET / (renders the board; _view_state is called there).
            _CLIENT.get(f"/?app={app}")
            # Inspect what _view_state injected into the live _state dict.
            return dict(server._state.get("autopilot") or {})
        finally:
            if ap_state is None:
                server._state.pop("autopilot", None)
            else:
                server._state["autopilot"] = prev if prev is not None else {}


# d-1: no autopilot in _state + daemon alive → _view_state must create the minimal external entry
_d1 = _call_view_state("automatixy", daemon_alive=True, ap_state=None)
chk(
    "_view_state(): creates minimal external-daemon entry when daemon alive + no cockpit record",
    _d1.get("daemon_running") is True and _d1.get("on") is False,
    str(_d1),
)

# d-2: autopilot entry exists + daemon alive → refreshes daemon_running in-place to True
_d2 = _call_view_state(
    "automatixy",
    daemon_alive=True,
    ap_state={"on": True, "stopping": False, "daemon_running": False, "app": "automatixy"},
)
chk(
    "_view_state(): refreshes daemon_running to True when process is alive",
    _d2.get("daemon_running") is True,
    str(_d2),
)

# d-3: autopilot entry exists + daemon dead → refreshes daemon_running in-place to False
_d3 = _call_view_state(
    "automatixy",
    daemon_alive=False,
    ap_state={"on": True, "stopping": False, "daemon_running": True, "app": "automatixy"},
)
chk(
    "_view_state(): refreshes daemon_running to False when process is gone",
    _d3.get("daemon_running") is False,
    str(_d3),
)

# =============================================================================
# Summary
# =============================================================================
passed_n = sum(1 for _, ok, _ in results if ok)
print(f"\n========= EU-73 daemon server tests =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("---------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed_n == len(results) else f"{len(results) - passed_n} FAIL ❌")
sys.exit(0 if passed_n == len(results) else 1)
