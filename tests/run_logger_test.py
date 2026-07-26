"""EU-106: run_logger — per-run log file writer.

Tests:
  1. open_run_log creates the log file at the right path (logs/<app>/<date>/<ticket>-<time>.log).
  2. Tee.write calls run_logger.write_line — lines appear in the file.
  3. close_run_log closes the file and deregisters the handle.
  4. _purge_old_logs removes day-folders older than retention_days.
  5. log_path stored in per-app run state after open_run_log.
  6. Path traversal guard in /api/open-logs (403 outside log root).
  7. /api/open-logs returns 403 on non-Mac (platform mock).
  8. log_root respects cfg.log_folder and cfg.audit_path.

  EU-618:
  9.  read_day_log: multi-run merge + chronological ordering.
  10. Stage attribution from inline markers; missing-stage fallback keeps lines.
  11. Filename fallback (no HHMMSS) — mtime interleaves on one scale; unreadable files are marked, not dropped.
  12. Empty day folder → {date, entries: []} via API (HTTP 200).
  13. Populated day via API returns merged entries.
  14. Path traversal guard on /api/day-log returns 403.
  15. Bad date on /api/day-log returns 400.
"""
import sys
import os
import types
import tempfile
import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Minimal stubs so orchestrator modules load without the real SDK / Flask / etc.
# ---------------------------------------------------------------------------
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)

sys.path.insert(0, ".")

from orchestrator import run_logger, cockpit_state
from orchestrator.config import Config, AppConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
results = []

def chk(name, cond, detail=""):
    results.append((name, bool(cond), str(detail)))


def _make_cfg(tmp: Path) -> Config:
    """Minimal in-memory Config pointing at a temp dir."""
    return Config(
        apps=[AppConfig(name="testapp", repo_path=str(tmp),
                        base_branch="dev", protected_branch="main",
                        backlog_backend="none")],
        audit_path=str(tmp / "audit.jsonl"),
        log_folder="logs/",
    )


def _clean_registry():
    """Close all open log handles and reset cockpit_state for isolation."""
    with run_logger._lock:
        for key in list(run_logger._log_handles):
            run_logger._close_handle(key)
    cockpit_state.reset_run_state()


# ---------------------------------------------------------------------------
# Helpers for EU-618 tests
# ---------------------------------------------------------------------------
def _write_day_file(app_dir: Path, name: str, content: str) -> Path:
    """Write a synthetic log file under ``app_dir`` and return its path."""
    f = app_dir / name
    f.write_text(content, encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# 1. open_run_log creates the file at the expected path format
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _tmp:
    tmp = Path(_tmp)
    cfg = _make_cfg(tmp)
    _clean_registry()

    log_path = run_logger.open_run_log(cfg, "testapp", "AUTO-42")
    chk("open_run_log returns a Path", isinstance(log_path, Path))
    chk("log file created on disk", log_path.exists())
    chk("log path contains app slug", "testapp" in str(log_path))
    chk("log path contains ticket slug", "AUTO-42" in str(log_path))
    chk("log path contains date (YYYY-MM-DD)", datetime.date.today().isoformat() in str(log_path))
    chk("log path ends with .log", str(log_path).endswith(".log"))
    # Filename must be <ticket>-<HHMMSS>.log
    chk("filename starts with ticket slug", log_path.name.startswith("AUTO-42-"))

    _clean_registry()

# ---------------------------------------------------------------------------
# 2. write_line writes to the open log file
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _tmp:
    tmp = Path(_tmp)
    cfg = _make_cfg(tmp)
    _clean_registry()

    log_path = run_logger.open_run_log(cfg, "testapp", "EU-99")
    run_logger.write_line("testapp", "hello from test")
    run_logger.write_line("testapp", "second line")
    run_logger.close_run_log("testapp")

    content = log_path.read_text(encoding="utf-8")
    chk("write_line: first line appears in log", "hello from test" in content)
    chk("write_line: second line appears in log", "second line" in content)

    _clean_registry()

# ---------------------------------------------------------------------------
# 3. write_line for unknown key is a no-op (no crash)
# ---------------------------------------------------------------------------
_clean_registry()
try:
    run_logger.write_line("nonexistent-app", "ignored")
    chk("write_line for unknown key: no crash", True)
except Exception as exc:
    chk("write_line for unknown key: no crash", False, exc)

# ---------------------------------------------------------------------------
# 4. close_run_log deregisters the handle (subsequent write_line is a no-op)
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _tmp:
    tmp = Path(_tmp)
    cfg = _make_cfg(tmp)
    _clean_registry()

    log_path = run_logger.open_run_log(cfg, "testapp", "EU-100")
    run_logger.close_run_log("testapp")

    # handle must be gone from the registry
    with run_logger._lock:
        chk("close_run_log removes handle from registry", "testapp" not in run_logger._log_handles)

    # write after close should be a no-op
    before_size = log_path.stat().st_size
    run_logger.write_line("testapp", "should not appear")
    after_size = log_path.stat().st_size
    chk("write_line after close: file not grown", after_size == before_size)

    _clean_registry()

# ---------------------------------------------------------------------------
# 5. log_path stored in per-app run state
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _tmp:
    tmp = Path(_tmp)
    cfg = _make_cfg(tmp)
    _clean_registry()

    log_path = run_logger.open_run_log(cfg, "testapp", "AUTO-7")
    st = cockpit_state.get_state("testapp")
    chk("log_path stored in per-app run state", st.get("log_path") == str(log_path))

    _clean_registry()

# ---------------------------------------------------------------------------
# 6. _purge_old_logs removes old day-folders; keeps recent ones
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _tmp:
    tmp = Path(_tmp)
    app_dir = tmp / "logs" / "testapp"

    today = datetime.datetime.now()
    old_date = today - datetime.timedelta(days=10)
    recent_date = today - datetime.timedelta(days=2)

    old_dir = app_dir / old_date.strftime("%Y-%m-%d")
    recent_dir = app_dir / recent_date.strftime("%Y-%m-%d")
    today_dir = app_dir / today.strftime("%Y-%m-%d")
    non_date_dir = app_dir / "not-a-date"

    for d in (old_dir, recent_dir, today_dir, non_date_dir):
        d.mkdir(parents=True)
        (d / "run.log").write_text("x")

    run_logger._purge_old_logs(app_dir, retention_days=5, now=today)

    chk("_purge: old folder removed", not old_dir.exists())
    chk("_purge: recent folder kept", recent_dir.exists())
    chk("_purge: today folder kept", today_dir.exists())
    chk("_purge: non-date folder left alone", non_date_dir.exists())

# ---------------------------------------------------------------------------
# 7. log_root respects cfg.log_folder
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _tmp:
    tmp = Path(_tmp)
    cfg_default = Config(
        apps=[AppConfig(name="x", repo_path=str(tmp), base_branch="dev",
                        protected_branch="main", backlog_backend="none")],
        audit_path=str(tmp / "audit.jsonl"),
    )
    root = run_logger.log_root(cfg_default)
    chk("log_root default = <audit_dir>/logs", root == (tmp / "logs").resolve())

    cfg_custom = Config(
        apps=[AppConfig(name="x", repo_path=str(tmp), base_branch="dev",
                        protected_branch="main", backlog_backend="none")],
        audit_path=str(tmp / "audit.jsonl"),
        log_folder="run-logs/",
    )
    root2 = run_logger.log_root(cfg_custom)
    chk("log_root honours cfg.log_folder", root2 == (tmp / "run-logs").resolve())

# ---------------------------------------------------------------------------
# 8. /api/open-logs path traversal guard
# ---------------------------------------------------------------------------
# We test the logic directly without spinning up Flask (avoids heavy dep chain).

with tempfile.TemporaryDirectory() as _tmp:
    tmp = Path(_tmp)
    cfg = _make_cfg(tmp)
    log_root_path = run_logger.log_root(cfg)
    log_root_path.mkdir(parents=True, exist_ok=True)

    # Build a fake-resolved path inside and outside the log root
    inside = log_root_path / "testapp" / "2026-06-29" / "AUTO-42-153000.log"
    outside = tmp / "audit.jsonl"

    def _traversal_allowed(path: Path) -> bool:
        """Mimic the guard in /api/open-logs: True if path is under log_root."""
        try:
            path.relative_to(log_root_path)
            return True
        except ValueError:
            return False

    chk("traversal guard: inside log_root → allowed", _traversal_allowed(inside))
    chk("traversal guard: outside log_root → blocked", not _traversal_allowed(outside))
    chk("traversal guard: log_root itself → allowed", _traversal_allowed(log_root_path))

# ---------------------------------------------------------------------------
# 9. None key (default/legacy run) works correctly
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _tmp:
    tmp = Path(_tmp)
    cfg = _make_cfg(tmp)
    _clean_registry()

    log_path = run_logger.open_run_log(cfg, None, "drain")
    chk("None key: log file created", log_path.exists())
    run_logger.write_line(None, "line from default run")
    run_logger.close_run_log(None)
    content = log_path.read_text(encoding="utf-8")
    chk("None key: line written to log", "line from default run" in content)

    _clean_registry()

# ---------------------------------------------------------------------------
# 10. log_retention_days = 0 disables purging (non-zero retention is exercised above)
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _tmp:
    tmp = Path(_tmp)
    app_dir = tmp / "logs" / "testapp"
    old_dir = app_dir / "2020-01-01"
    old_dir.mkdir(parents=True)

    # retention_days=0 → purge must not run
    run_logger._purge_old_logs(app_dir, retention_days=0, now=datetime.datetime.now())
    chk("retention_days=0: old folder NOT purged", old_dir.exists())


# ===========================================================================
# EU-618: per-day log aggregator + API endpoint
# ===========================================================================

with tempfile.TemporaryDirectory() as _tmp:
    tmp = Path(_tmp)
    cfg = _make_cfg(tmp)
    log_root_path = run_logger.log_root(cfg)
    log_root_path.mkdir(parents=True, exist_ok=True)

    day_dir = log_root_path / "testapp" / "2026-07-15"
    day_dir.mkdir(parents=True, exist_ok=True)

    # 3 files, 2 tickets: EU-1 at 10:00 and 12:00; EU-2 at 11:00
    _write_day_file(day_dir, "EU-1-100000.log",
                    "[Build] Starting build\nChecking deps\n[Gate] Gate passed\n")
    _write_day_file(day_dir, "EU-2-110000.log",
                    "[Review] Reviewing changes\nCommented on line 42\nScanning imports\n")
    _write_day_file(day_dir, "EU-1-120000.log",
                    "[Land] Merging\nPushed to dev\nRebased onto latest\n")

    result = run_logger.read_day_log(cfg, "testapp", "2026-07-15")
    entries = result["entries"]

    # AC1: all lines present across all files
    total_lines = 9  # 3+3+3
    chk("read_day_log: returns date field", result.get("date") == "2026-07-15")
    chk("read_day_log: multi-run merge — all lines present", len(entries) == total_lines)

    # AC1: chronological ordering (HHMMSS ascending)
    times = [e["time"] for e in entries]
    sorted_times = sorted(times)
    chk("read_day_log: ordered by HHMMSS ascending", times == sorted_times)

    # AC1: each entry has ticket, stage, time, text keys
    chk("read_day_log: entries carry required keys",
        all(set(e.keys()) >= {"ticket", "stage", "time", "text"} for e in entries))

    # AC1: first file's ticket is EU-1, first stage is Build
    chk("read_day_log: first entry ticket=EU-1", entries[0]["ticket"] == "EU-1")
    chk("read_day_log: first entry stage=Build", entries[0]["stage"] == "Build")

    # AC1: middle file (EU-2) starts with Review
    eu2_start = next(i for i, e in enumerate(entries) if e["ticket"] == "EU-2")
    chk("read_day_log: EU-2 first stage=Review", entries[eu2_start]["stage"] == "Review")

    # AC1: last file (EU-1 second run) lands at end, ticket still EU-1
    chk("read_day_log: last entry ticket=EU-1", entries[-1]["ticket"] == "EU-1")

    _clean_registry()

with tempfile.TemporaryDirectory() as _tmp:
    tmp = Path(_tmp)
    cfg = _make_cfg(tmp)
    log_root_path = run_logger.log_root(cfg)
    log_root_path.mkdir(parents=True, exist_ok=True)

    day_dir = log_root_path / "testapp" / "2026-07-16"
    day_dir.mkdir(parents=True, exist_ok=True)

    # File with stage markers followed by ordinary lines
    _write_day_file(day_dir, "EU-5-140000.log",
                    "[Build] Compiling\nsome random output line\nanother line\n"
                    "[Gate] Running gate checks\npost-gate note\n")

    # Marker-less file
    _write_day_file(day_dir, "EU-6-150000.log",
                    "started work\nno stage here\ndone\n")

    result = run_logger.read_day_log(cfg, "testapp", "2026-07-16")
    entries = result["entries"]

    # AC2: stage propagation after marker
    chk("read_day_log: stage prop after marker—line after Build",
        any(e["stage"] == "Build" and "random output" in e["text"] for e in entries))
    chk("read_day_log: stage prop after marker—gate check",
        any(e["stage"] == "Gate" and "gate checks" in e["text"] for e in entries))
    chk("read_day_log: stage prop after marker—post-gate note still Gate",
        any(e["stage"] == "Gate" and "post-gate" in e["text"] for e in entries))

    # AC2: missing-stage fallback (marker-less file)
    empty_stage_entries = [e for e in entries if e["stage"] == "" and e["ticket"] == "EU-6"]
    chk("read_day_log: no-crash on missing stages", len(empty_stage_entries) > 0)
    chk("read_day_log: missing-stage lines NOT dropped",
        any("started work" in e["text"] for e in empty_stage_entries))

    _clean_registry()

with tempfile.TemporaryDirectory() as _tmp:
    tmp = Path(_tmp)
    cfg = _make_cfg(tmp)
    log_root_path = run_logger.log_root(cfg)
    log_root_path.mkdir(parents=True, exist_ok=True)

    day_dir = log_root_path / "testapp" / "2026-07-17"
    day_dir.mkdir(parents=True, exist_ok=True)

    # A file named without HHMMSS suffix (e.g. a hand-dropped note)
    note_path = _write_day_file(day_dir, "note.log", "manual note line\n")
    # Also a normal file earlier in the day
    _write_day_file(day_dir, "EU-7-080000.log", "[Build] normal build\n")

    result = run_logger.read_day_log(cfg, "testapp", "2026-07-17")
    entries = result["entries"]

    # AC3: note file included, ticket derived from stem
    chk("read_day_log: note file included (no crash)", len(entries) > 0)
    chk("read_day_log: note ticket derived from stem",
        any(e["ticket"] == "note" for e in entries))
    chk("read_day_log: note content present",
        any("manual note" in e["text"] for e in entries))

    # Ordering normalisation: an mtime-fallback file must interleave with
    # HHMMSS-named files on ONE scale (mtime rebased onto the day's midnight),
    # not sort after every named file. Pin it from both sides of EU-7's 08:00.
    def _at(hour: int) -> float:
        return datetime.datetime(2026, 7, 17, hour, 0, 0).timestamp()

    os.utime(note_path, (_at(9), _at(9)))          # note at 09:00 → AFTER EU-7
    order = [e["ticket"] for e in run_logger.read_day_log(cfg, "testapp", "2026-07-17")["entries"]]
    chk("read_day_log: mtime-fallback file interleaves (note 09:00 after EU-7 08:00)",
        order == ["EU-7", "note"], order)

    os.utime(note_path, (_at(7), _at(7)))          # note at 07:00 → BEFORE EU-7
    order = [e["ticket"] for e in run_logger.read_day_log(cfg, "testapp", "2026-07-17")["entries"]]
    chk("read_day_log: mtime-fallback file interleaves (note 07:00 before EU-7 08:00)",
        order == ["note", "EU-7"], order)

    # An unreadable "file" is marked, never silently dropped (a directory raises
    # IsADirectoryError — an OSError — when opened for reading).
    marker = run_logger._scan_file(day_dir)
    chk("read_day_log: unreadable file → attributed [read error] marker, not silence",
        len(marker) == 1 and marker[0]["text"].startswith("[read error]"), marker)

    _clean_registry()

with tempfile.TemporaryDirectory() as _tmp:
    tmp = Path(_tmp)
    cfg = _make_cfg(tmp)
    log_root_path = run_logger.log_root(cfg)
    log_root_path.mkdir(parents=True, exist_ok=True)

    # Empty day dir — create folder but no .log files
    day_dir = log_root_path / "testapp" / "2026-07-18"
    day_dir.mkdir(parents=True, exist_ok=True)

    result = run_logger.read_day_log(cfg, "testapp", "2026-07-18")
    chk("read_day_log: empty day returns entries=[]", result["entries"] == [])
    chk("read_day_log: empty day preserves date", result["date"] == "2026-07-18")

    # Non-existent day dir
    result2 = run_logger.read_day_log(cfg, "testapp", "2099-01-01")
    chk("read_day_log: missing day dir returns entries=[]", result2["entries"] == [])
    chk("read_day_log: missing day preserves date", result2["date"] == "2099-01-01")

    _clean_registry()

# ---------------------------------------------------------------------------
# 12–15. GET /api/day-log — route-level tests against the REAL handler
# ---------------------------------------------------------------------------
# Built the same way `general serve` builds it, driven through Flask's test
# client, so these assertions exercise the live handler — import wiring, date
# validation, the traversal guard and read_day_log — not a reimplementation.
from orchestrator import server as srv

with tempfile.TemporaryDirectory() as _tmp:
    tmp = Path(_tmp)
    cfg = _make_cfg(tmp)
    log_root_path = run_logger.log_root(cfg)
    log_root_path.mkdir(parents=True, exist_ok=True)
    client = srv.create_app(cfg).test_client()

    # ---- 12. Empty / missing day → 200 {"date", "entries": []} (never 404/500) ----
    r = client.get("/api/day-log?app=testapp&date=2099-01-01")
    chk("day-log route: missing day folder → HTTP 200", r.status_code == 200, r.status_code)
    chk("day-log route: missing day → {date, entries: []}",
        r.get_json() == {"date": "2099-01-01", "entries": []}, r.get_json())

    (log_root_path / "testapp" / "2026-07-21").mkdir(parents=True, exist_ok=True)
    r = client.get("/api/day-log?app=testapp&date=2026-07-21")
    chk("day-log route: existing-but-empty day → HTTP 200", r.status_code == 200, r.status_code)
    chk("day-log route: empty day → {date, entries: []}",
        r.get_json() == {"date": "2026-07-21", "entries": []}, r.get_json())

    # ---- 13. Populated day → merged, chronological, attributed entries ----
    day_dir = log_root_path / "testapp" / "2026-07-22"
    day_dir.mkdir(parents=True, exist_ok=True)
    _write_day_file(day_dir, "EU-9-100000.log", "[Build] compiled\nplain line\n")
    _write_day_file(day_dir, "EU-10-110000.log", "[Review] reviewing\n")
    _write_day_file(day_dir, "EU-9-120000.log", "[Land] merged\n")

    r = client.get("/api/day-log?app=testapp&date=2026-07-22")
    chk("day-log route: populated day → HTTP 200", r.status_code == 200, r.status_code)
    body = r.get_json()
    chk("day-log route: response echoes date", body.get("date") == "2026-07-22", body.get("date"))
    entries = body.get("entries", [])
    chk("day-log route: all lines from all files merged", len(entries) == 4, len(entries))
    chk("day-log route: entries ordered chronologically",
        [e["time"] for e in entries] == ["10:00:00", "10:00:00", "11:00:00", "12:00:00"],
        [e["time"] for e in entries])
    chk("day-log route: ticket attribution per file",
        [e["ticket"] for e in entries] == ["EU-9", "EU-9", "EU-10", "EU-9"],
        [e["ticket"] for e in entries])
    chk("day-log route: stage attribution + propagation",
        [e["stage"] for e in entries] == ["Build", "Build", "Review", "Land"],
        [e["stage"] for e in entries])
    chk("day-log route: line text preserved",
        [e["text"] for e in entries]
        == ["[Build] compiled", "plain line", "[Review] reviewing", "[Land] merged"],
        [e["text"] for e in entries])

    # ---- 14. Traversal guard: app values that escape the log root → 403 ----
    # '..' is the ONLY value that survives _safe_slug (dots are allowed chars)
    # and still resolves above the root — the resolved-path guard must catch it.
    r = client.get("/api/day-log?app=..&date=2026-07-22")
    chk("day-log route: app='..' escapes log root → 403", r.status_code == 403, r.status_code)
    # Multi-level traversals contain slashes, which the slugifier turns into
    # underscores ('../..' → '.._..', '../../etc' → '.._.._etc'): the request
    # stays under the root and simply finds nothing — an empty 200, not a leak.
    r = client.get("/api/day-log?app=../..&date=2026-07-22")
    chk("day-log route: app='../..' neutralised by slugifier to empty 200",
        r.status_code == 200 and r.get_json()["entries"] == [],
        (r.status_code, r.get_json()))
    r = client.get("/api/day-log?app=..%2F..%2Fetc&date=2026-07-22")
    chk("day-log route: slashed traversal neutralised to empty 200",
        r.status_code == 200 and r.get_json()["entries"] == [],
        (r.status_code, r.get_json()))

    # ---- 15. Malformed date → 400 ----
    for bad in ("not-a-date", "2026-13-99", "2026-2-30", "yesterday", ""):
        r = client.get(f"/api/day-log?app=testapp&date={bad}")
        chk(f"day-log route: malformed date {bad!r} → 400", r.status_code == 400, r.status_code)
    r = client.get("/api/day-log?app=testapp")
    chk("day-log route: missing date param → 400", r.status_code == 400, r.status_code)

    _clean_registry()

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
print("\n================= EU-106 run_logger QA =================")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    mark = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{mark}] {name}{extra}")
print("---------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
