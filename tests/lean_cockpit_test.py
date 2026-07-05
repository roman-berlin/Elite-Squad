"""EU-106 lean cockpit + log-layout tests.

(1) Panel-removal: render_board() must NOT contain the live-feed, activity, or tickets-to-work
    sections that were retired in EU-106; must still contain the needs-you panel and phase bar.
(2) Log path layout: open_run_log() returns a path that matches the expected
    logs/<app>/<YYYY-MM-DD>/<ticket>-<HHMMSS>.log pattern under the configured log folder;
    the file must exist and be writable.
(3) Mac gate: _control_bar() includes '📂 Open logs' link only when is_mac=True;
    absent when is_mac=False.
(4) Retention purge: _purge_old_logs() deletes day-folders older than retention_days and
    leaves recent ones untouched.
"""
from __future__ import annotations

import datetime
import re
import sys
import tempfile
import types
from pathlib import Path

# ---------------------------------------------------------------------------
# Minimal stubs so orchestrator modules load without the real SDK / Flask / etc.
# ---------------------------------------------------------------------------
_sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass  # noqa: N807
    def __call__(s, *a, **k): return s  # noqa: N807


_sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", _sdk)

_req = types.ModuleType("requests")
_req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules.setdefault("requests", _req)

sys.path.insert(0, ".")

from orchestrator import cockpit_state, run_logger, warroom  # noqa: E402
from orchestrator import cockpit_views  # noqa: E402
from orchestrator.config import AppConfig, Config  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    """Record a named assertion result."""
    results.append((name, bool(cond), str(detail)))


def _make_cfg(tmp: Path, log_folder: str = "logs/", seed_run: bool = False) -> Config:
    """Minimal Config pointing at *tmp* with an (optionally seeded) audit file.

    When *seed_run* is True a single completed run is written to the audit so
    ``active_run()`` has something to render — otherwise the board shows the
    "No runs yet" empty state which has no phase bar.
    """
    import json as _json

    audit = tmp / "audit.jsonl"
    if not audit.exists():
        if seed_run:
            _ts = datetime.datetime.now().isoformat()
            audit.write_text(
                _json.dumps({
                    "event": "ticket_start",
                    "ticket_id": "EU-106",
                    "app": "EU",
                    "branch": "dev/EU-106",
                    "ts": _ts,
                })
                + "\n",
                encoding="utf-8",
            )
        else:
            audit.write_text("")
    return Config(
        apps=[
            AppConfig(
                name="EU",
                repo_path=str(tmp),
                base_branch="dev",
                protected_branch="main",
                backlog_backend="none",
            )
        ],
        audit_path=str(audit),
        log_folder=log_folder,
    )


def _clean_log_registry() -> None:
    """Reset run_logger handles and cockpit_state between tests."""
    with run_logger._lock:
        for key in list(run_logger._log_handles):
            run_logger._close_handle(key)
    cockpit_state.reset_run_state()


# ===========================================================================
# 1. Panel-removal
#    render_board() must not contain the retired live-feed / activity /
#    tickets-to-work sections, and must still include needs-you and phase bar.
# ===========================================================================
with tempfile.TemporaryDirectory() as _tmp:
    _tmp_path = Path(_tmp)
    # Seed the audit with one task so active_run() has data to render (including the phase bar).
    cfg1 = _make_cfg(_tmp_path, seed_run=True)

    # Empty state — no active run, no autopilot.
    _state: dict = {}
    board_html = warroom.render_board(cfg1, "EU", _state)

    # --- Retired sections must be absent ---
    # Live-feed panel used class=feed and a panel header containing "Live feed".
    chk(
        "board: no live-feed panel (class=feed absent)",
        "class=feed" not in board_html and "class=\"feed\"" not in board_html,
    )
    chk(
        "board: no Live feed header text",
        "Live feed" not in board_html and "live feed" not in board_html.lower(),
    )
    # Activity panel used id=actpanel.
    chk(
        "board: no activity panel (actpanel absent)",
        "actpanel" not in board_html,
    )
    # Tickets-to-work panel used class=backlog and class=blrow.
    chk(
        "board: no tickets-to-work panel (class=backlog absent)",
        "class=backlog" not in board_html and "class=\"backlog\"" not in board_html,
    )
    chk(
        "board: no blrow (ticket-row elements absent)",
        "class=blrow" not in board_html and "class=\"blrow\"" not in board_html,
    )

    # --- Retained sections must still be present ---
    # (The needs-you side panel was removed with the rest of the Needs-you board UI in 5a882a6;
    # /needs is the inbox surface now.)
    chk(
        "board: needs-you panel stays removed (5a882a6)",
        "class=\"panel needspanel\"" not in board_html and "class='panel needspanel'" not in board_html,
    )
    chk(
        "board: phase bar present (phasebar)",
        "phasebar" in board_html,
    )

# ===========================================================================
# 2. Log path layout
#    open_run_log() must return a path matching:
#      <log_folder>/EU/<YYYY-MM-DD>/EU-106-<HHMMSS>.log
#    The file must exist and be writable after the call.
# ===========================================================================
with tempfile.TemporaryDirectory() as _tmp:
    _tmp_path = Path(_tmp)
    cfg2 = _make_cfg(_tmp_path)   # log_folder defaults to 'logs/'
    _clean_log_registry()

    _before = datetime.datetime.now()
    log_path = run_logger.open_run_log(cfg2, "EU", "EU-106")
    _after = datetime.datetime.now()

    chk("log path: returns a Path object", isinstance(log_path, Path))
    chk("log path: file exists on disk", log_path.exists())

    # Pattern: logs/EU/<YYYY-MM-DD>/EU-106-<HHMMSS>.log relative to the tmp dir.
    # resolve() to normalise any symlinks (macOS /var -> /private/var etc.).
    _log_str = str(log_path.resolve())
    _tmp_str = str(_tmp_path.resolve())
    chk(
        "log path: under the configured log_folder (logs/ relative to audit dir)",
        _log_str.startswith(_tmp_str),
    )
    # Regex for the path suffix:  logs/EU/YYYY-MM-DD/EU-106-HHMMSS.log
    _DATE = r"\d{4}-\d{2}-\d{2}"
    _TIME = r"\d{6}"
    _PATTERN = re.compile(
        r"[/\\]logs[/\\]EU[/\\]" + _DATE + r"[/\\]EU-106-" + _TIME + r"\.log$"
    )
    chk(
        "log path: matches logs/EU/<YYYY-MM-DD>/EU-106-<HHMMSS>.log pattern",
        bool(_PATTERN.search(_log_str)),
        _log_str,
    )

    # Date folder must reflect today's date.
    _today = datetime.date.today().isoformat()
    chk("log path: date folder is today", _today in _log_str, _log_str)

    # File must be writable (handle is open in the registry).
    try:
        run_logger.write_line("EU", "lean-cockpit-test probe")
        run_logger.close_run_log("EU")
        _content = log_path.read_text(encoding="utf-8")
        chk("log path: file is writable (probe line written)", "lean-cockpit-test probe" in _content)
    except Exception as exc:  # noqa: BLE001
        chk("log path: file is writable (probe line written)", False, str(exc))

    _clean_log_registry()

# ===========================================================================
# 3. Mac gate
#    _control_bar() includes the '📂 Open logs' button when is_mac=True,
#    and omits it when is_mac=False.
# ===========================================================================
with tempfile.TemporaryDirectory() as _tmp:
    _tmp_path = Path(_tmp)
    cfg3 = _make_cfg(_tmp_path)

    # Reset global _state so the control bar renders without an active run.
    cockpit_state.reset_run_state()

    bar_mac = cockpit_views._control_bar(cfg3, current_app="EU", healthy=True, is_mac=True)
    bar_no_mac = cockpit_views._control_bar(cfg3, current_app="EU", healthy=True, is_mac=False)

    # The button text in the HTML is "&#128194; Open logs" (📂 is U+1F4C2 = &#128194;).
    chk(
        "mac gate: 'Open logs' button present when is_mac=True",
        "Open logs" in bar_mac,
        bar_mac[:200] if "Open logs" not in bar_mac else "",
    )
    chk(
        "mac gate: 'Open logs' button absent when is_mac=False",
        "Open logs" not in bar_no_mac,
    )
    # Also confirm the /api/open-logs endpoint URL appears in the Mac variant only.
    chk(
        "mac gate: /api/open-logs href present when is_mac=True",
        "open-logs" in bar_mac,
    )
    chk(
        "mac gate: /api/open-logs href absent when is_mac=False",
        "open-logs" not in bar_no_mac,
    )

# ===========================================================================
# 4. Retention purge
#    _purge_old_logs() removes day-folders older than retention_days and
#    leaves recent ones alone.  Non-date-named folders are never touched.
# ===========================================================================
with tempfile.TemporaryDirectory() as _tmp:
    _tmp_path = Path(_tmp)
    app_dir = _tmp_path / "logs" / "EU"

    _now = datetime.datetime.now()
    # retention_days=1 → only folders whose date < yesterday should be deleted.
    _old_date = (_now - datetime.timedelta(days=5)).strftime("%Y-%m-%d")
    _recent_date = (_now - datetime.timedelta(days=0)).strftime("%Y-%m-%d")   # today → kept

    old_dir = app_dir / _old_date
    recent_dir = app_dir / _recent_date
    non_date_dir = app_dir / "not-a-date"

    for d in (old_dir, recent_dir, non_date_dir):
        d.mkdir(parents=True, exist_ok=True)
        (d / "run.log").write_text("x", encoding="utf-8")

    run_logger._purge_old_logs(app_dir, retention_days=1, now=_now)

    chk("purge: old day-folder (5 days ago) is deleted", not old_dir.exists())
    chk("purge: today's folder is kept", recent_dir.exists())
    chk("purge: non-date folder is left alone", non_date_dir.exists())

    # Edge: retention_days=0 disables purging entirely.
    _very_old = app_dir / "2020-01-01"
    _very_old.mkdir(parents=True, exist_ok=True)
    run_logger._purge_old_logs(app_dir, retention_days=0, now=_now)
    chk("purge: retention_days=0 leaves even ancient folders alone", _very_old.exists())

# ===========================================================================
# Report
# ===========================================================================
print("\n================= EU-106 lean cockpit QA =================")
_passed = sum(1 for _, ok, _ in results if ok)
for _name, _ok, _det in results:
    _mark = "PASS" if _ok else "FAIL"
    _extra = f"  ({_det})" if _det and not _ok else ""
    print(f"  [{_mark}] {_name}{_extra}")
print("-----------------------------------------------------------")
print(f"  {_passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if _passed == len(results) else f"{len(results) - _passed} FAIL")
sys.exit(0 if _passed == len(results) else 1)
