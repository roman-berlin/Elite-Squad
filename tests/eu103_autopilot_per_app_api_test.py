"""EU-103 — per-PROJECT autopilot controls (iteration 2).

Each project tab owns its own Autopilot start/stop, driven by the SELECTED app's run-state — never a
single global autopilot. This harness pins the backend that makes that real:

  (a) cockpit_state.get_autopilot_status(app) — derives ``on`` from the dedicated per-app
        ``autopilot_on`` flag, NOT bare ``active`` (so a manual run is not "Autopilot ON"):
        a-1: fresh state → on=False, stopping=False, mode=None
        a-2: autopilot_on=True + no stop_event → on=True, stopping=False
        a-3: autopilot_on=True + stop_event.is_set() → stopping=True
        a-4: a MANUAL run (active=True, autopilot_on=False) → on=False  ← de-conflation
        a-5: autopilot_mode stored and returned
  (b) GET /api/autopilot — per-app 'apps' map
  (c) POST /api/autopilot — mode param stored per-app
  (d) _tab_bar() — running dot only when THAT project's autopilot is live (autopilot_on)
  (e) _control_bar() — per-project Autopilot section; a manual run shows the OFF (start) control
  (f) POST action=start for TWO different apps → BOTH on=True independently, in parallel; stopping
        one leaves the other running, each with its own stop_event (the core EU-103 requirement)
  (g) POST action=start honours autopilot_mode: 'choose' opens the ticket picker (no autopilot
        started); 'drain' starts the continuous autopilot — the two buttons do DIFFERENT things
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

# ── shared config / Flask test client (TWO apps, so parallelism is testable) ───
_TMP = Path(tempfile.mkdtemp())
# get_autopilot_status() / _control_bar() OR in a liveness probe of the machine-global autopilot
# PID file (/tmp/general-autopilot.pid). A live daemon — or another checkout's suite running the
# real autopilot() — flips 'on' True in the unpatched sections below (the 2026-07-06 flake).
# Probe a per-harness path instead.
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
# Autopilot Start needs a healthy unit; keep it green for the whole harness.
server.health.summary = lambda c: {"healthy": True, "checks": []}

# A fake autopilot loop: records the app it was started for and BLOCKS (like a live loop) until this
# app's stop_event fires or the test releases every fake — so ``autopilot_on`` stays True long enough
# to assert on. The cockpit Start path sets ``autopilot_on`` synchronously before spawning this, so the
# flag is True the instant the POST returns. Module-level assignment (not a context patch) so the _bg
# thread always resolves it, with no un-patch race.
_release = threading.Event()
_started_apps: list = []


async def _fake_autopilot(cfg, app_name=None, once=False, interval=60, stop_event=None):
    _started_apps.append(app_name)
    # The cap is a hang-guard only (the threads are daemonic; a buggy test can never hang the
    # suite). It must be generous: the old ~5s cap expired under full-suite load, the loop
    # returned early, and _bg's finally flipped autopilot_on off under the asserts.
    for _ in range(3000):  # ~60s
        if _release.is_set() or (stop_event is not None and stop_event.is_set()):
            return
        time.sleep(0.02)


_ap_mod.autopilot = _fake_autopilot


def _drain_threads() -> None:
    """Release the fake loops and wait for the per-app run-state to clear, then reset."""
    _release.set()
    # Wait for every _bg worker to run its finally (release_run) BEFORE resetting the run-state:
    # a straggler releasing after the reset would clear the NEXT section's freshly-claimed run.
    # The fakes poll every 20ms, so this exits almost immediately; the deadline is a hang-guard.
    for _ in range(1500):   # ~30s
        if cockpit_state.active_run_count() == 0:
            break
        time.sleep(0.02)
    cockpit_state.reset_run_state()
    _release.clear()
    _started_apps.clear()


# ── result accumulator ────────────────────────────────────────────────────────
results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    """Accumulate without raising so every case runs; summary at end."""
    results.append((name, bool(cond), str(detail)))


# =============================================================================
# (a) get_autopilot_status helper — ON is autopilot-specific, not bare active
# =============================================================================

def _test_fresh_state() -> None:
    cockpit_state.reset_run_state()
    s = cockpit_state.get_autopilot_status("automatixy")
    chk("get_autopilot_status: fresh → on=False", s["on"] is False, str(s))
    chk("get_autopilot_status: fresh → stopping=False", s["stopping"] is False, str(s))
    chk("get_autopilot_status: fresh → mode=None", s["mode"] is None, str(s))


def _test_autopilot_on_no_stop_event() -> None:
    """autopilot_on=True + no stop_event → on=True, stopping=False."""
    cockpit_state.reset_run_state()
    st = cockpit_state.get_state("automatixy")
    st["active"] = True
    st["autopilot_on"] = True
    st["stop_event"] = None
    s = cockpit_state.get_autopilot_status("automatixy")
    chk("get_autopilot_status: autopilot_on=True → on=True", s["on"] is True, str(s))
    chk("get_autopilot_status: no stop_event → stopping=False", s["stopping"] is False, str(s))


def _test_stopping_when_stop_event_set() -> None:
    """autopilot_on=True + stop_event.is_set() → stopping=True."""
    cockpit_state.reset_run_state()
    st = cockpit_state.get_state("automatixy")
    st["active"] = True
    st["autopilot_on"] = True
    ev = threading.Event()
    ev.set()
    st["stop_event"] = ev
    s = cockpit_state.get_autopilot_status("automatixy")
    chk("get_autopilot_status: stop_event set → stopping=True", s["stopping"] is True, str(s))


def _test_manual_run_is_not_autopilot() -> None:
    """A MANUAL run (active=True but autopilot_on=False) must NOT read as Autopilot ON — this is the
    EU-103 iter-2 de-conflation: a plain run no longer renders as 'Autopilot ON' with dead buttons."""
    cockpit_state.reset_run_state()
    st = cockpit_state.get_state("automatixy")
    st["active"] = True          # a run is in flight…
    st["autopilot_on"] = False   # …but it is NOT the autopilot
    s = cockpit_state.get_autopilot_status("automatixy")
    chk("get_autopilot_status: manual run (active, not autopilot) → on=False", s["on"] is False, str(s))
    # Even a stop_event on a manual run must not surface as autopilot 'stopping'.
    ev = threading.Event()
    ev.set()
    st["stop_event"] = ev
    chk("get_autopilot_status: manual run → stopping=False (gated on autopilot)",
        cockpit_state.get_autopilot_status("automatixy")["stopping"] is False, "")


def _test_mode_stored_and_returned() -> None:
    cockpit_state.reset_run_state()
    cockpit_state.get_state("automatixy")["autopilot_mode"] = "choose"
    s = cockpit_state.get_autopilot_status("automatixy")
    chk("get_autopilot_status: mode='choose' returned", s["mode"] == "choose", str(s))


_test_fresh_state()
_test_autopilot_on_no_stop_event()
_test_stopping_when_stop_event_set()
_test_manual_run_is_not_autopilot()
_test_mode_stored_and_returned()

# =============================================================================
# (b) GET /api/autopilot — 'apps' map
# =============================================================================

def _get_autopilot(extra_cookies: dict | None = None) -> dict:
    with patch("orchestrator.autopilot.daemon_running", return_value=False):
        resp = _CLIENT.get("/api/autopilot", headers={
            "Cookie": "; ".join(f"{k}={v}" for k, v in (extra_cookies or {}).items())
        })
    return json.loads(resp.data)


def _test_empty_workspace_apps_map() -> None:
    j = _get_autopilot()
    chk("GET /api/autopilot: 'apps' key present", "apps" in j, str(j))
    chk("GET /api/autopilot: 'apps' is a dict", isinstance(j.get("apps"), dict), str(j))


def _test_open_tab_appears_in_map() -> None:
    with patch("orchestrator.autopilot.daemon_running", return_value=False):
        nav = _CLIENT.get("/?app=automatixy")
    sid = nav.headers.get("Set-Cookie", "")
    cookie_val = None
    for part in sid.split(";"):
        part = part.strip()
        if part.startswith("eu_cockpit_sid="):
            cookie_val = part.split("=", 1)[1]
            break
    if cookie_val is None:
        j = _get_autopilot()
    else:
        j = _get_autopilot({"eu_cockpit_sid": cookie_val})
    apps = j.get("apps", {})
    chk("GET /api/autopilot: automatixy in apps map", "automatixy" in apps, str(apps))
    entry = apps.get("automatixy", {})
    for k in ("on", "stopping", "mode", "ticket"):
        chk(f"GET /api/autopilot: entry has '{k}'", k in entry, str(entry))


_test_empty_workspace_apps_map()
_test_open_tab_appears_in_map()

# =============================================================================
# (c) POST /api/autopilot — mode param persists per-app
# =============================================================================

def _post_mode(app: str, mode: str) -> int:
    with patch("orchestrator.autopilot.daemon_running", return_value=False):
        resp = _CLIENT.post("/api/autopilot", data={"app": app, "mode": mode, "action": "toggle"})
    return resp.status_code


def _test_mode_choose_stored() -> None:
    cockpit_state.reset_run_state()
    _post_mode("automatixy", "choose")
    stored = cockpit_state.get_state("automatixy").get("autopilot_mode")
    chk("POST /api/autopilot: mode='choose' persisted", stored == "choose", f"stored={stored!r}")


def _test_mode_drain_stored() -> None:
    cockpit_state.reset_run_state()
    _post_mode("automatixy", "drain")
    stored = cockpit_state.get_state("automatixy").get("autopilot_mode")
    chk("POST /api/autopilot: mode='drain' persisted", stored == "drain", f"stored={stored!r}")


def _test_invalid_mode_rejected() -> None:
    cockpit_state.reset_run_state()
    cockpit_state.get_state("automatixy")["autopilot_mode"] = "choose"   # pre-set
    with patch("orchestrator.autopilot.daemon_running", return_value=False):
        resp = _CLIENT.post("/api/autopilot", data={"app": "automatixy", "mode": "blah", "action": "toggle"})
    stored = cockpit_state.get_state("automatixy").get("autopilot_mode")
    chk("POST /api/autopilot: invalid mode → 302 redirect", resp.status_code == 302,
        f"status={resp.status_code}")
    chk("POST /api/autopilot: invalid mode → state unchanged", stored == "choose",
        f"stored={stored!r}")


_test_mode_choose_stored()
_test_mode_drain_stored()
_test_invalid_mode_rejected()

# =============================================================================
# (d) _tab_bar() — running dot only when THAT project's autopilot is live
# =============================================================================

from orchestrator import cockpit_views as V


def _test_tab_bar_no_dot_when_idle() -> None:
    cockpit_state.reset_run_state()
    bar = V._control_bar(_CFG, "automatixy", True)
    chk("_tab_bar: no tabdot span when autopilot is idle",
        "<span class=tabdot" not in bar, bar[:120])


def _test_tab_bar_dot_when_autopilot_on() -> None:
    """The dot reflects AUTOPILOT (autopilot_on), not a bare active run."""
    cockpit_state.reset_run_state()
    st = cockpit_state.get_state("automatixy")
    st["active"] = True
    st["autopilot_on"] = True
    bar = V._control_bar(_CFG, "automatixy", True)
    chk("_tab_bar: tabdot span present when autopilot is live",
        "<span class=tabdot" in bar, bar[:120])
    cockpit_state.reset_run_state()


def _test_tab_bar_no_dot_for_manual_run() -> None:
    """A manual run (active but not autopilot) must NOT light the autopilot dot."""
    cockpit_state.reset_run_state()
    cockpit_state.get_state("automatixy")["active"] = True   # manual run, autopilot_on stays False
    bar = V._control_bar(_CFG, "automatixy", True)
    chk("_tab_bar: no tabdot for a manual (non-autopilot) run",
        "<span class=tabdot" not in bar, bar[:120])
    cockpit_state.reset_run_state()


_test_tab_bar_no_dot_when_idle()
_test_tab_bar_dot_when_autopilot_on()
_test_tab_bar_no_dot_for_manual_run()

# =============================================================================
# (e) _control_bar() — per-project Autopilot section
# =============================================================================

def _test_control_bar_ap_off_shows_start_buttons() -> None:
    cockpit_state.reset_run_state()
    bar = V._control_bar(_CFG, "automatixy", True)
    chk("_control_bar: Choose tickets button (mode=choose)", "mode value=choose" in bar, bar[:200])
    chk("_control_bar: Auto-drain button (mode=drain)", "mode value=drain" in bar, bar[:200])
    chk("_control_bar: tbap off section", 'class="tbap off"' in bar, bar[:200])
    chk("_control_bar: start action posted to /api/autopilot",
        'action=/api/autopilot' in bar, bar[:200])


def _test_control_bar_ap_on_shows_stop_buttons() -> None:
    cockpit_state.reset_run_state()
    st = cockpit_state.get_state("automatixy")
    st["active"] = True
    st["autopilot_on"] = True
    bar = V._control_bar(_CFG, "automatixy", True)
    chk("_control_bar: Finish & stop button (drain) when on",
        "action value=drain" in bar and "Finish" in bar, bar[:300])
    chk("_control_bar: Stop button when on",
        "action value=stop" in bar and "Stop" in bar, bar[:300])
    chk("_control_bar: no start buttons when on",
        "mode value=choose" not in bar and "mode value=drain" not in bar, "")
    chk("_control_bar: tbap on class present", 'class="tbap on"' in bar, bar[:300])
    cockpit_state.reset_run_state()


def _test_control_bar_manual_run_shows_off_control() -> None:
    """De-conflation (EU-103 iter-2): a MANUAL run must render the OFF (start) control, never the
    'Autopilot ON' control with Finish/Stop that would do nothing for a non-autopilot run."""
    cockpit_state.reset_run_state()
    st = cockpit_state.get_state("automatixy")
    st["active"] = True          # a manual run is in flight…
    st["autopilot_on"] = False   # …but it is NOT the autopilot
    bar = V._control_bar(_CFG, "automatixy", True)
    chk("_control_bar: manual run → OFF control (tbap off, not tbap on)",
        'class="tbap off"' in bar and 'class="tbap on"' not in bar, bar[:300])
    chk("_control_bar: manual run → no Finish/Stop autopilot controls",
        "action value=drain" not in bar and "action value=stop" not in bar, "")
    chk("_control_bar: manual run → Auto-drain start button present",
        "mode value=drain" in bar, "")
    cockpit_state.reset_run_state()


def _test_control_bar_ap_stopping_shows_label() -> None:
    cockpit_state.reset_run_state()
    st = cockpit_state.get_state("automatixy")
    st["active"] = True
    st["autopilot_on"] = True
    ev = threading.Event()
    ev.set()
    st["stop_event"] = ev
    bar = V._control_bar(_CFG, "automatixy", True)
    chk("_control_bar: stopping label shown", "Stopping" in bar or "stopping" in bar, bar[:300])
    chk("_control_bar: no start buttons while stopping",
        "mode value=choose" not in bar and "mode value=drain" not in bar, "")
    # EU-689: hard-stop control remains visible during a drain (not collapsed to passive chip).
    # EU-708 relabeled the button 'Stop autopilot' (its true effect); wiring unchanged.
    chk("_control_bar: Stop autopilot button present while stopping (EU-689; relabeled EU-708)",
        "action value=stop" in bar and "Stop&nbsp;autopilot</button>" in bar, bar)
    chk("_control_bar: tbap stopping class present", 'class="tbap stopping"' in bar, bar[:300])
    cockpit_state.reset_run_state()


_test_control_bar_ap_off_shows_start_buttons()
_test_control_bar_ap_on_shows_stop_buttons()
_test_control_bar_manual_run_shows_off_control()
_test_control_bar_ap_stopping_shows_label()

# =============================================================================
# (f) POST start for TWO apps → both ON independently, in parallel (core EU-103)
# =============================================================================

def _start(app: str, mode: str = "drain"):
    with patch("orchestrator.autopilot.daemon_running", return_value=False):
        return _CLIENT.post("/api/autopilot", data={"app": app, "action": "start", "mode": mode})


def _stop(app: str):
    with patch("orchestrator.autopilot.daemon_running", return_value=False):
        return _CLIENT.post("/api/autopilot", data={"app": app, "action": "stop"})


def _test_two_apps_run_in_parallel() -> None:
    cockpit_state.reset_run_state()
    _release.clear()
    _started_apps.clear()
    _start("automatixy")
    _start("Elite-Unit")
    on_a = cockpit_state.get_autopilot_status("automatixy")["on"]
    on_e = cockpit_state.get_autopilot_status("Elite-Unit")["on"]
    chk("two-app start: automatixy autopilot ON", on_a is True, f"automatixy={on_a}")
    chk("two-app start: Elite-Unit autopilot ON (started while automatixy runs)", on_e is True,
        f"Elite-Unit={on_e}")
    chk("two-app start: both run in parallel (active_run_count>=2)",
        cockpit_state.active_run_count() >= 2, f"count={cockpit_state.active_run_count()}")
    ev_a = cockpit_state.get_state("automatixy").get("stop_event")
    ev_e = cockpit_state.get_state("Elite-Unit").get("stop_event")
    chk("two-app start: each app has its OWN stop_event",
        ev_a is not None and ev_e is not None and ev_a is not ev_e, f"a={ev_a!r} e={ev_e!r}")

    # Stop ONE — the other keeps running, with its stop_event untouched.
    _stop("automatixy")
    chk("stop one: automatixy autopilot now OFF",
        cockpit_state.get_autopilot_status("automatixy")["on"] is False, "")
    chk("stop one: Elite-Unit autopilot STILL ON (independent)",
        cockpit_state.get_autopilot_status("Elite-Unit")["on"] is True, "")
    chk("stop one: only automatixy's stop_event fired",
        ev_a.is_set() and not ev_e.is_set(), f"a_set={ev_a.is_set()} e_set={ev_e.is_set()}")
    _drain_threads()


_test_two_apps_run_in_parallel()

# =============================================================================
# (g) POST start honours autopilot_mode — choose vs drain do DIFFERENT things
# =============================================================================

def _test_choose_mode_opens_picker() -> None:
    cockpit_state.reset_run_state()
    _release.clear()
    _started_apps.clear()
    r = _start("automatixy", mode="choose")
    loc = r.headers.get("Location", "")
    chk("choose-mode: redirects to the per-ticket picker", "/tickets?app=automatixy" in loc,
        f"Location={loc!r}")
    chk("choose-mode: does NOT start the autopilot",
        cockpit_state.get_autopilot_status("automatixy")["on"] is False, "")
    chk("choose-mode: no loop was launched", _started_apps == [], f"started={_started_apps}")


def _test_drain_mode_starts_autopilot() -> None:
    cockpit_state.reset_run_state()
    _release.clear()
    _started_apps.clear()
    r = _start("automatixy", mode="drain")
    loc = r.headers.get("Location", "")
    chk("drain-mode: turns the autopilot ON",
        cockpit_state.get_autopilot_status("automatixy")["on"] is True, "")
    chk("drain-mode: does NOT redirect to the picker", "/tickets" not in loc, f"Location={loc!r}")
    _drain_threads()


_test_choose_mode_opens_picker()
_test_drain_mode_starts_autopilot()

# =============================================================================
# Summary
# =============================================================================
passed_n = sum(1 for _, ok, _ in results if ok)
print(f"\n========= EU-103 per-project autopilot tests =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("------------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed_n == len(results) else f"{len(results) - passed_n} FAIL ❌")
sys.exit(0 if passed_n == len(results) else 1)
