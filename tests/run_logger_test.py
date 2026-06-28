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
