"""Run log file writer — stream per-run stdout to logs/<app>/<YYYY-MM-DD>/<TICKET>-<HHMMSS>.log.

Every captured stdout line is written to the run's log file via the ``_Tee`` in
``cockpit_state``.  The log path is stored in per-app run state as ``state['log_path']``
so the UI can surface an "Open logs" button.

Config keys (set in config.yaml or the Config dataclass):
    log_folder         — root dir for all run logs (default: ``'logs/'``, relative to
                         the directory that contains ``audit_path``).
    log_retention_days — auto-purge day-folders whose date is older than this many days
                         on every run start.  0 or absent = disabled.

Typical log path:  logs/automatixy/2026-06-29/AUTO-42-153000.log
"""
from __future__ import annotations

import datetime
import shutil
import threading
from pathlib import Path
from typing import IO, Any

# ---------------------------------------------------------------------------
# Module-level handle registry
# ---------------------------------------------------------------------------
# Keyed by app_key (str app name OR None for the legacy/default run-key).
# One open file handle per concurrent run; closed by close_run_log().
# Guarded by _lock for thread safety (multiple apps can run concurrently).
_log_handles: "dict[object, IO[str]]" = {}
_lock = threading.Lock()

# Unique marker so ``run_key`` can distinguish "not supplied" (derive the registry key from
# ``app_name`` — the legacy behaviour) from an explicit ``None`` (the unit-wide/all-apps
# drain key that ``_Tee.write`` actually uses when a single run is active). EU-253.
_UNSET: object = object()


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def log_root(cfg: Any) -> Path:
    """Resolve the log root directory from *cfg*.

    Uses ``cfg.log_folder`` when present, otherwise falls back to ``'logs/'``.
    The path is resolved relative to the directory that contains ``cfg.audit_path``
    (the same anchor the rest of the unit uses for out-of-repo artefacts).
    """
    folder = getattr(cfg, "log_folder", None) or "logs/"
    base = Path(getattr(cfg, "audit_path", "./audit.jsonl")).parent
    return (base / folder).resolve()


def _prepare_log_path(cfg: Any, app_name: str | None, ticket_key: str | None) -> Path:
    """Compute (and mkdir) the dated per-ticket log path, purging old day-folders first.

    Path: ``<log_root>/<app>/<YYYY-MM-DD>/<TICKET>-<HHMMSS>.log``.  Shared by
    ``open_run_log`` (which then opens+registers a handle) and ``write_note_log`` (which
    writes a one-shot note without registering a handle).
    """
    now = datetime.datetime.now()
    app_slug = _safe_slug(app_name or "default")
    ticket_slug = _safe_slug(ticket_key or "run")
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M%S")

    root = log_root(cfg)
    day_dir = root / app_slug / date_str
    day_dir.mkdir(parents=True, exist_ok=True)

    # Auto-purge old day-folders before we write anything new.
    retention = int(getattr(cfg, "log_retention_days", 0) or 0)
    if retention > 0:
        _purge_old_logs(root / app_slug, retention, now)

    return day_dir / f"{ticket_slug}-{time_str}.log"


def _resolve_key(app_name: str | None, run_key: object) -> object:
    """The handle-registry key: the explicit ``run_key`` when supplied, else derived from
    ``app_name`` (``None`` for a falsy name) — the legacy default.

    EU-253: the registry key MUST match what ``cockpit_state._Tee.write`` looks up
    (``active_runs()[0]`` when exactly one run is active, else ``None``) or captured lines
    land under a key nobody wrote a handle for and the file stays empty.  The ON-DISK PATH
    is always derived from ``app_name`` (see ``_prepare_log_path``); only the in-memory
    registry key follows ``run_key``.
    """
    return (app_name if app_name else None) if run_key is _UNSET else run_key


def open_run_log(cfg: Any, app_name: str | None, ticket_key: str | None,
                 *, run_key: object = _UNSET) -> Path:
    """Open a log file for a new run and register it in the handle registry.

    Log path: ``<log_root>/<app>/<YYYY-MM-DD>/<TICKET>-<HHMMSS>.log`` (from ``app_name``).

    Also:
    * Auto-purges day-folders older than ``cfg.log_retention_days`` (if set).
    * Stores the log path in the per-app run state under ``state['log_path']`` via
      ``cockpit_state.get_state()``.

    ``run_key`` (EU-253): the in-memory handle-registry key to register under.  Default
    (unsupplied) keeps the legacy behaviour — key derived from ``app_name``.  Automode
    drains pass the ACTIVE run key that ``_Tee.write`` uses (``None`` for a unit-wide
    drain), so captured lines actually reach this handle while the file still lives under
    the ticket's app.

    Returns the resolved log :class:`~pathlib.Path`.  Call this *before* starting the
    background thread so every line the ``_Tee`` captures goes straight to the file.
    """
    log_path = _prepare_log_path(cfg, app_name, ticket_key)

    # Open the log file in line-buffered append mode so each line flushes immediately.
    handle: IO[str] = log_path.open("a", encoding="utf-8", buffering=1)

    key = _resolve_key(app_name, run_key)
    with _lock:
        _close_handle(key)   # close any stale handle (defensive; shouldn't happen)
        _log_handles[key] = handle

    # Store the log path in per-app run state so the UI can surface it.
    try:
        from . import cockpit_state
        cockpit_state.get_state(key)["log_path"] = str(log_path)
    except Exception:  # noqa: BLE001 — state enrichment must never block the run
        pass

    return log_path


def write_note_log(cfg: Any, app_name: str | None, ticket_key: str | None, note: str) -> Path:
    """Write a standalone dated per-ticket log file containing *note* — NO handle registered.

    EU-253: when more than one run is active, ``_Tee.write`` collapses line attribution to
    the shared ``None`` key, so a per-ticket handle can't cleanly own this drain's lines
    (and two drains registering under ``None`` would clobber each other's handle).  Rather
    than leave a SILENTLY EMPTY per-ticket file, we drop a real, dated file that explains the
    gap and points at the shared drain stream.  Best-effort: never raises."""
    log_path = _prepare_log_path(cfg, app_name, ticket_key)
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(note.rstrip("\n") + "\n")
    except Exception:  # noqa: BLE001 — a note-write failure must never abort a run
        pass
    return log_path


def close_run_log(app_name: str | None = None, *, run_key: object = _UNSET) -> None:
    """Close and de-register the open log handle for *app_name* / *run_key*.

    Call this in the run ``_bg`` finally-block alongside ``release_run()``.  ``run_key``
    (EU-253) mirrors ``open_run_log``: pass the same key you opened under so the right
    handle is closed (the default derives it from ``app_name``).
    """
    key = _resolve_key(app_name, run_key)
    with _lock:
        _close_handle(key)


def write_line(app_key: object, line: str) -> None:
    """Write one log line for *app_key* (if a log is open for it).

    Called by ``cockpit_state._Tee.write`` for every captured stdout line.
    Thread-safe and best-effort: any I/O error is swallowed so a log-write failure
    can never abort a run.
    """
    with _lock:
        handle = _log_handles.get(app_key)
    if handle is None:
        return
    try:
        handle.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _safe_slug(text: str) -> str:
    """Replace path-unsafe characters with underscores; truncate to 80 chars."""
    return "".join(c if c.isalnum() or c in "-." else "_" for c in text)[:80]


def _close_handle(key: object) -> None:
    """Close and remove the handle for *key*.  **Caller must hold** ``_lock``."""
    handle = _log_handles.pop(key, None)
    if handle is not None:
        try:
            handle.close()
        except Exception:  # noqa: BLE001
            pass


def _purge_old_logs(app_dir: Path, retention_days: int, now: datetime.datetime) -> None:
    """Delete date-named day-folders under *app_dir* older than *retention_days*.

    Only removes directories whose names parse as ``YYYY-MM-DD``; any other entries
    are left untouched.  ``retention_days <= 0`` disables purging (returns immediately).
    Deletion failures are swallowed (best-effort) so a stale locked file can never
    abort the run that triggered the purge.
    """
    if retention_days <= 0:
        return
    if not app_dir.is_dir():
        return
    cutoff = (now - datetime.timedelta(days=retention_days)).date()
    for entry in app_dir.iterdir():
        if not entry.is_dir():
            continue
        try:
            folder_date = datetime.date.fromisoformat(entry.name)
        except ValueError:
            continue   # not a YYYY-MM-DD folder — leave it alone
        if folder_date < cutoff:
            try:
                shutil.rmtree(entry)
            except Exception:  # noqa: BLE001
                pass
