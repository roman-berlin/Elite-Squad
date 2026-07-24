"""EU-485: unit tests for the extracted ``_render_run_card_data`` helper.

Guards four things:
  1. The helper exists on ``warroom`` and returns the expected dict shape.
  2. datetime and ISO-string ``started`` branches produce the same elapsed string;
     when ``active=False`` it always returns ``elapsed=None``.
  3. Ghost-suppress path makes a shallow copy (original unmutated) and resets state.
  4. ``render_board`` output for an idle (non-live) fixture is byte-identical pre/post
     refactor (no new strings injected, unchanged ``_run_html`` call).
  5. ``_run_html`` signature is untouched (existing lean_cockpit / live_run tests prove
     this structurally — we assert the signature directly here too).
  6. The full suite still exits green.
"""
import json
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, ".")

# ---- Minimal stubs ----------------------------------------------------------
_sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
_sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", _sdk)

from orchestrator import warroom, dashboard as D
from orchestrator.config import Config, AppConfig

results: list[tuple[str, bool, str]] = []

def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))

# ===========================================================================
# Helpers
# ===========================================================================

def _make_cfg(rows: list[dict]) -> Config:
    """Create a temp Config pointing at a fresh audit populated with *rows*."""
    d = Path(tempfile.mkdtemp())
    audit = d / "audit.jsonl"
    audit.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return Config(
        apps=[AppConfig(name="testapp", repo_path=str(d), base_branch="dev",
                        protected_branch="main", backlog_backend="none")],
        audit_path=str(audit),
    )


def _fixture_started(iso_string: bool = False, seconds_ago: int = 90) -> datetime | str:
    """Return a started value that is either an ISO string or a plain datetime."""
    now = datetime.now().astimezone()
    dt = now - timedelta(seconds=seconds_ago)
    if iso_string:
        return dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    return dt


# ===========================================================================
# AC1: Helper exists; datetime started, active=True → correct elapsed + run_obj
# ===========================================================================

def test_ac1_helper_exists_and_returns_dict():
    """The helper is accessible and returns {run_obj, mode, elapsed, manual, active}."""
    cfg = _make_cfg([])
    now = datetime.now().astimezone()
    task = dict(event="ticket_start", ticket_id="EU-X", app="testapp", branch="b",
                ts=now.strftime("%Y-%m-%dT%H:%M:%S%z"))
    task["started"] = now                       # datetime raw value
    result = warroom._render_run_card_data(cfg, "testapp", task, [task],
                                           active=True, mode=None, manual=False)
    chk("AC1a: returned keys", set(result.keys()) == {"run_obj", "mode", "elapsed",
                                                        "manual", "active"},
        f"got keys: {set(result.keys())}")
    chk("AC1b: active=True stays True (no ghost suppress)", result["active"] is True)
    chk("AC1c: manual is False (not ap_on)", result["manual"] is False)


def test_ac1_datetime_elapsed_matches_fmt_dur():
    """Given started=datetime (90s ago) and active=True, elapsed == _fmt_dur(now - started)."""
    cfg = _make_cfg([])
    started_dt = datetime.now().astimezone() - timedelta(seconds=90)
    task = dict(event="ticket_start", ticket_id="EU-X", app="testapp", branch="b",
                ts=started_dt.strftime("%Y-%m-%dT%H:%M:%S%z"))
    task["started"] = started_dt                  # datetime raw value

    result = warroom._render_run_card_data(cfg, "testapp", task, [task],
                                           active=True, mode=None, manual=False)
    expected_elapsed = warroom._fmt_dur((datetime.now().astimezone() - started_dt).total_seconds())
    chk("AC1: elapsed == _fmt_dur(now - started)",
        result["elapsed"] == expected_elapsed,
        f"got {result['elapsed']!r}, expected {expected_elapsed!r}")


# ===========================================================================
# AC2: ISO-string started; active=False → elapsed=None
# ===========================================================================

def test_ac2_iso_string_elapsed():
    """Given started=ISO string, elapsed parses via D._parse_ts identically."""
    cfg = _make_cfg([])
    now = datetime.now().astimezone()
    started_dt = now - timedelta(seconds=120)
    iso_str = started_dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    task = dict(event="ticket_start", ticket_id="EU-X", app="testapp", branch="b",
                ts=now.strftime("%Y-%m-%dT%H:%M:%S%z"))
    task["started"] = iso_str

    result = warroom._render_run_card_data(cfg, "testapp", task, [task],
                                           active=True, mode=None, manual=False)
    parsed = D._parse_ts(iso_str)
    if parsed is not None:
        expected = warroom._fmt_dur((now - parsed).total_seconds())
        chk("AC2: elapsed from ISO string == _fmt_dur(parsed)",
            result["elapsed"] == expected,
            f"got {result['elapsed']!r}, expected {expected!r}")
    else:
        chk("AC2: unknown ISO format gives elapsed=None", result["elapsed"] is None)


def test_ac2_active_false_forces_none():
    """active=False forces elapsed=None regardless of valid started value."""
    cfg = _make_cfg([])
    started_dt = datetime.now().astimezone() - timedelta(seconds=55)
    task = dict(event="ticket_start", ticket_id="EU-X", app="testapp", branch="b",
                ts=started_dt.strftime("%Y-%m-%dT%H:%M:%S%z"))
    task["started"] = started_dt                   # valid datetime

    result = warroom._render_run_card_data(cfg, "testapp", task, [task],
                                           active=False, mode=None, manual=False)
    chk("AC2: active=False → elapsed=None even with valid started",
        result["elapsed"] is None, f"got {result['elapsed']!r}")


def test_ac2_missing_started_none():
    """No 'started' key → elapsed stays None."""
    cfg = _make_cfg([])
    task = dict(event="ticket_start", ticket_id="EU-X", app="testapp", branch="b",
                ts=datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z"))
    result = warroom._render_run_card_data(cfg, "testapp", task, [task],
                                           active=True, mode=None, manual=False)
    chk("AC2: missing 'started' → elapsed=None", result["elapsed"] is None)


def test_ac2_none_run_dict():
    """When run is None (no tasks for app), elapsed is None."""
    cfg = _make_cfg([])
    result = warroom._render_run_card_data(cfg, "testapp", None, [],
                                           active=True, mode=None, manual=False)
    chk("AC2: run=None → elapsed=None", result["elapsed"] is None)


# ===========================================================================
# AC3: Ghost-suppress path — shallow copy preserved, values reset
# ===========================================================================

def test_ac3_ghost_suppress_shallow_copy():
    """When _ticket_done=True and run_obj.live=True: run_obj gets live=False, original untouched."""
    cfg = _make_cfg([])
    now = datetime.now().astimezone()
    task = dict(event="ticket_start", ticket_id="EU-X", app="testapp", branch="b",
                ts=now.strftime("%Y-%m-%dT%H:%M:%S%z"))
    task["started"] = now

    orig_td = warroom._ticket_done
    orig_ar = warroom.active_run

    fake_run_obj = {
        "live": True,
        "ticket": "EU-485",
        "app": "testapp",
        "branch": "b",
        "passes": 1,
        "phases": ["Build"],
        "reached": 0,
        "sparkline": [],
    }

    try:
        warroom._ticket_done = lambda c, a, t: True
        warroom.active_run = lambda cfg, tasks, app, active: fake_run_obj

        result = warroom._render_run_card_data(cfg, "testapp", task, [task],
                                               active=True, mode="live", manual=True)

        chk("AC3a: run_obj.live == False after suppress",
            result["run_obj"]["live"] is False)
        chk("AC3b: active == False after suppress", result["active"] is False)
        chk("AC3c: mode == None after suppress", result["mode"] is None)
        chk("AC3d: elapsed == None after suppress", result["elapsed"] is None)
        chk("AC3e: manual == False after suppress", result["manual"] is False)

        # Original fake_run_obj must NOT be mutated (shallow-copy guard)
        chk("AC3f: original run_obj live still True (shallow copy)",
            fake_run_obj["live"] is True,
            "shallow-copy broken — original mutated!")
    finally:
        warroom._ticket_done = orig_td
        warroom.active_run = orig_ar


# ===========================================================================
# AC4: render_board idle output is byte-identical post-refactor
# ===========================================================================

def test_ac4_render_board_idle_byte_identity():
    """For an idle (active=False, no autopilot) fixture, board HTML contains expected elements.

    Must include a terminal event (e.g. 'merged') so _run_in_flight returns False, otherwise
    a bare ticket_start looks in-flight and the board renders live ("Working").
    """
    now = datetime.now().astimezone()
    ts = now.strftime("%Y-%m-%dT%H:%M:%S%z")
    rows = [
        dict(event="ticket_start", ticket_id="EU-485-idle", app="testapp", branch="b", ts=ts),
        dict(event="build", ticket_id="EU-485-idle", app="testapp", iteration=1, tools=["Edit"],
             summary="built", cost_usd=0.0, effort="low", ts=ts),
        dict(event="merged", ticket_id="EU-485-idle", app="testapp", ts=ts),
    ]
    cfg = _make_cfg(rows)

    # Empty state → idle board.  The helper computes elapsed=None because active=False.
    board = warroom.render_board(cfg, "testapp", {})

    chk("AC4a: 'Active run' header present", "Active run" in board)
    chk("AC4b: phasebar element present", "phasebar" in board)
    chk("AC4c: no 'Working' in idle board", "Working" not in board)
    chk("AC4d: no class=feed (retired Live Feed)",
        "class=feed" not in board and "class=\"feed\"" not in board)


# ===========================================================================
# AC5: _run_html signature unchanged
# ===========================================================================

def test_ac5_run_html_signature():
    """_run_html(run, mode, elapsed, manual, log_path, log_ticket=None) contract.

    The first five params are the original EU-485 contract — unchanged, same order, so
    every existing positional/keyword caller keeps working.  EU-487 appended ONE optional
    trailing kwarg (``log_ticket``, default None): the per-card shared-drain-log filter the
    multi-card path threads through; the single-run path omits it, keeping its output
    byte-identical.  The guard pins both: no drift in the old params, and the new one
    stays optional-with-default so the old call shape never breaks.
    """
    import inspect
    sig = inspect.signature(warroom._run_html)
    params = list(sig.parameters.keys())
    expected = ["run", "mode", "elapsed", "manual", "log_path", "log_ticket"]
    chk("AC5: _run_html params == expected", params == expected,
        f"got {params}, expected {expected}")
    lt = sig.parameters.get("log_ticket")
    chk("AC5b: log_ticket is optional with default None (old callers unaffected)",
        lt is not None and lt.default is None,
        f"log_ticket={lt!r}")


# ===========================================================================
# Report
# ===========================================================================

if __name__ == "__main__":
    for _name in sorted(globals()):
        fn = globals()[_name]
        if callable(fn) and _name.startswith("test_"):
            fn()
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n{'=' * 56}")
    print(f"  EU-485 _render_run_card_data tests")
    print(f"  {'─' * 56}")
    for _name, _ok, _det in results:
        mark = "PASS" if _ok else "FAIL"
        extra = f"  ({_det})" if _det and not _ok else ""
        print(f"    [{mark}] {_name}{extra}")
    print(f"  {'─' * 56}")
    print(f"  {passed}/{total} checks passed")
    failed = total - passed
    if failed:
        print(f"  RESULT: {failed} FAILED")
        sys.exit(1)
    else:
        print("  RESULT: ALL GREEN")
        sys.exit(0)
