"""EU-646 — per-project ``last_msg`` surfaces in the control bar and clears after one render.

Acceptance:
  (a) ``_control_bar`` renders ``get_state(app).get('last_msg')`` when present, falling back to
      ``_state.get('last_msg')`` otherwise.
  (b) The per-app message is cleared (set to empty string) immediately after being rendered once —
      it must not persist across the next unrelated GET /.
  (b2) The per-app tone comes from the stored ``last_msg_record`` (written via ``set_last_msg``),
       NOT from substring matches on the message text — the EU-656 record contract. Both the
       message and its record are cleared after the one render.
  (c) POST ``/api/run-selected`` with a payload that causes a refusal (app already running), then
      GET ``/`` and assert the refusal text appears in the rendered control bar.
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
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules.setdefault("requests", req)

sys.path.insert(0, ".")

from orchestrator import cockpit_state, server, sync
from orchestrator import autopilot as _ap_mod
from orchestrator.config import AppConfig, Config

# ── shared config (TWO apps for isolation testing) ─────────────────────────────
_TMP = Path(tempfile.mkdtemp())
_ap_mod._PID_FILE = _TMP / "general-autopilot.pid"
_CFG = Config(
    apps=[
        AppConfig(name="automatixy", repo_path=str(_TMP), base_branch="DEV",
                  protected_branch="MAIN", backlog_backend="none"),
        AppConfig(name="Elite-Unit", repo_path=str(_TMP), base_branch="dev",
                  protected_branch="main", backlog_backend="none"),
    ],
    audit_path=str(_TMP / "a.jsonl"),
    use_worktree=False,
)
_CFG.detected_auth = lambda: "test"
sync.can_promote = lambda: False

_APP = server.create_app(_CFG)
_CLIENT = _APP.test_client()
server.health.summary = lambda c: {"healthy": True, "checks": []}

# Fake autopilot loop: BLOCKS until released so we can hold runs alive.
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


# ── result accumulator ────────────────────────────────────────────────────────
results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


# =============================================================================
# (a) _control_bar renders per-app last_msg before global fallback
# =============================================================================
from orchestrator import cockpit_views as V


def _test_per_app_msg_shown_before_global() -> None:
    """Per-app ``last_msg`` must be read from the active project's state, NOT the global."""
    cockpit_state.reset_run_state()
    # Set ONLY the global last_msg; per-app slot should stay empty.
    cockpit_state._state["last_msg"] = "global hello"
    bar = V._control_bar(_CFG, "automatixy", True)
    chk("per-app msg priority: global shown when per-app empty",
        "global hello" in bar, bar[:200])
    # Per-app takes priority over global.
    cockpit_state.get_state("automatixy")["last_msg"] = "app hello"
    bar = V._control_bar(_CFG, "automatixy", True)
    chk("per-app msg priority: app msg shown (not global)",
        "app hello" in bar and "global hello" not in bar, bar[:200])
    # After rendering, the per-app msg is cleared so a second call falls back to global.
    bar2 = V._control_bar(_CFG, "automatixy", True)
    chk("per-app msg priority: global shown after per-app cleared",
        "global hello" in bar2, bar2[:200])
    cockpit_state.reset_run_state()


_test_per_app_msg_shown_before_global()


# =============================================================================
# (b) Per-app message is cleared after one render — must not persist
# =============================================================================
def _test_per_app_msg_cleared_after_one_render() -> None:
    """After rendering a per-app message, the next GET / must NOT repeat it."""
    cockpit_state.reset_run_state()
    st = cockpit_state.get_state("automatixy")
    st["last_msg"] = "run started on automatixy"
    bar1 = V._control_bar(_CFG, "automatixy", True)
    chk("render 1: per-app message appears",
        "run started on automatixy" in bar1, bar1[:200])
    # Verify the message was cleared from state.
    chk("state cleared after render", st.get("last_msg") == "", f"last_msg={st.get('last_msg')!r}")
    # Second render must not show the message (falls through to empty status).
    bar2 = V._control_bar(_CFG, "automatixy", True)
    chk("render 2: message does NOT persist",
        "run started on automatixy" not in bar2, bar2[:200])
    cockpit_state.reset_run_state()


_test_per_app_msg_cleared_after_one_render()


# =============================================================================
# (b2) Per-app tone comes from the stored record (set_last_msg), not the text
# =============================================================================
def _snippet(bar: str) -> str:
    """The rendered tbnote span (with context) for failure details."""
    i = bar.find("tbnote")
    return bar[max(0, i - 30):i + 120] if i >= 0 else bar[:200]


def _test_per_app_tone_from_record() -> None:
    """``set_last_msg``'s stored record — not message-text substrings — decides the tone class.

    Each case uses text the OLD substring logic would have mis-toned, so these checks fail if
    the per-app branch ever regresses to content-sniffing.
    """
    cockpit_state.reset_run_state()
    st = cockpit_state.get_state("automatixy")
    # tone='error' with text carrying NO failure keyword — old substring logic rendered 'dim'.
    server.set_last_msg("automatixy", "error", "run stopped by operator")
    bar = V._control_bar(_CFG, "automatixy", True)
    chk("per-app tone=error → span class 'bad'",
        '<span class="tbnote bad">run stopped by operator</span>' in bar, _snippet(bar))
    chk("per-app msg AND record cleared after render",
        st.get("last_msg") == "" and st.get("last_msg_record") is None,
        f"last_msg={st.get('last_msg')!r} record={st.get('last_msg_record')!r}")
    # tone='ok' with text that DOES carry a failure keyword — old substring logic rendered 'bad'.
    server.set_last_msg("automatixy", "ok", "drain finished, 1 error recovered")
    bar = V._control_bar(_CFG, "automatixy", True)
    chk("per-app tone=ok → span class 'ok'",
        '<span class="tbnote ok">drain finished, 1 error recovered</span>' in bar, _snippet(bar))
    # tone='warn' maps to the neutral 'dim'.
    server.set_last_msg("automatixy", "warn", "a run is already in progress for this project")
    bar = V._control_bar(_CFG, "automatixy", True)
    chk("per-app tone=warn → span class 'dim'",
        '<span class="tbnote dim">a run is already in progress for this project</span>' in bar,
        _snippet(bar))
    # A legacy RAW write (no record) renders neutral 'dim' and still clears after one render.
    st["last_msg"] = "legacy note"
    bar = V._control_bar(_CFG, "automatixy", True)
    chk("per-app legacy raw write → span class 'dim'",
        '<span class="tbnote dim">legacy note</span>' in bar, _snippet(bar))
    chk("per-app legacy note cleared after render",
        st.get("last_msg") == "", f"last_msg={st.get('last_msg')!r}")
    cockpit_state.reset_run_state()


_test_per_app_tone_from_record()


# =============================================================================
# (c) POST /api/run-selected refusal surfaces in the control bar via GET /
# =============================================================================
def _test_run_selected_refusal_surfaces_in_control_bar() -> None:
    """POST /api/run-selected → app already running → redirect / → GET / shows refusal in control bar."""
    cockpit_state.reset_run_state()
    _release.clear()
    _started_apps.clear()

    # Start an autopilot on automatixy to occupy the run slot.
    with patch("orchestrator.autopilot.daemon_running", return_value=False):
        r1 = _CLIENT.post("/api/autopilot",
                          data={"app": "automatixy", "action": "start", "mode": "drain"})
    chk("POST /api/autopilot start → 302 redirect",
        r1.status_code == 302, f"status={r1.status_code}")
    # The first request sets a cookie; keep it for subsequent requests.

    # Now try to POST /api/run-selected for the SAME app — should be refused.
    with patch("orchestrator.intake.from_tickets", side_effect=RuntimeError("jira unavailable")):
        r2 = _CLIENT.post("/api/run-selected",
                          data={"app": "automatixy", "ticket": "EU-123"},
                          follow_redirects=False)
    chk("POST /api/run-selected while running → 302 redirect",
        r2.status_code == 302, f"status={r2.status_code}")

    # Follow the redirect (GET /) and assert refusal text appears.
    r3 = _CLIENT.get("/", follow_redirects=True)
    chk("GET / after refused POST → 200 OK",
        r3.status_code == 200, f"status={r3.status_code}")
    body = r3.data.decode()
    # The refusal comes from _claim_cockpit_run which writes to st["last_msg"]:
    #   "a run is already in progress for this project"
    chk("refusal text appears in control bar",
        "a run is already in progress" in body or "could not start" in body,
        body[:500])
    _drain_threads()


_test_run_selected_refusal_surfaces_in_control_bar()


# =============================================================================
# Summary
# =============================================================================
passed_n = sum(1 for _, ok, _ in results if ok)
print(f"\n========= EU-646 per-project message tests =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("------------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed_n == len(results) else f"{len(results) - passed_n} FAIL ❌")
sys.exit(0 if passed_n == len(results) else 1)
