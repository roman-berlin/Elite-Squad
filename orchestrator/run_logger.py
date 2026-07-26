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

Per-day aggregation (EU-618):
    ``read_day_log(cfg, app_name, date_str)`` reads every run-log file under a day folder,
    merges them into one chronological stream, and attributes each entry ``{ticket, stage,
    time, text}``.  Stage comes from optional inline stage markers; lines without a known
    marker are kept with ``stage: ""`` and never silently dropped.
"""
from __future__ import annotations

import datetime
import re
import shutil
import threading
from pathlib import Path
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

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

def log_root(cfg: Config) -> Path:
    """Resolve the log root directory from *cfg*.

    Uses ``cfg.log_folder`` when present, otherwise falls back to ``'logs/'``.
    The path is resolved relative to the directory that contains ``cfg.audit_path``
    (the same anchor the rest of the unit uses for out-of-repo artefacts).
    """
    folder = getattr(cfg, "log_folder", None) or "logs/"
    base = Path(getattr(cfg, "audit_path", "./audit.jsonl")).parent
    return (base / folder).resolve()


def _prepare_log_path(cfg: Config, app_name: str | None, ticket_key: str | None) -> Path:
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


def open_run_log(cfg: Config, app_name: str | None, ticket_key: str | None,
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


def drain_log_path() -> str | None:
    """Where THIS process's stdout actually lands — the shared drain stream (2026-07-21).

    The concurrent drain's per-ticket file is a pointer note saying "consult the drain log", but
    it never said WHERE, so following it meant guessing (it cost a live misdiagnosis: an active
    build read as a stall because the per-ticket file looked empty). Resolved from fd 1 via lsof,
    which handles the launchd/systemd redirect case that os.readlink('/dev/fd/1') cannot.
    Returns None when stdout is a terminal/pipe or the probe fails — the caller then prints the
    generic hint instead of a wrong path."""
    import os
    import subprocess
    try:
        r = subprocess.run(["lsof", "-p", str(os.getpid()), "-a", "-d", "1", "-Fn"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        if line.startswith("n") and len(line) > 2:
            target = line[1:].strip()
            # only a real file is useful to tail; a tty/pipe/socket is not a log
            if target.startswith("/") and not target.startswith(("/dev/tty", "/dev/pts")):
                return target
    return None


def write_note_log(cfg: Config, app_name: str | None, ticket_key: str | None, note: str) -> Path:
    """Write a standalone dated per-ticket log file containing *note* — NO handle registered.

    EU-253: when more than one run is active, ``_Tee.write`` collapses line attribution to
    the shared ``None`` key, so a per-ticket handle can't cleanly own this drain's lines
    (and two drains registering under ``None`` would clobber each other's handle).  Rather
    than leave a SILENTLY EMPTY per-ticket file, we drop a real, dated file that explains the
    gap and points at the shared drain stream.  Best-effort: never raises."""
    log_path = _prepare_log_path(cfg, app_name, ticket_key)
    # 2026-07-21: a pointer note has to point somewhere. Append the resolved drain-stream path
    # (and the command to follow it) so "consult the drain log" is actionable instead of a riddle.
    drain = drain_log_path()
    note = note.rstrip("\n") + (
        f"\nLive stream for this run:  tail -f {drain}" if drain else
        "\nLive stream for this run: the serve process's stdout "
        "(launchd/systemd StandardOutPath; e.g. ~/Library/Logs/General/cockpit.log on macOS)."
    )
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


# ---------------------------------------------------------------------------
# Per-day log aggregation (EU-618)
# ---------------------------------------------------------------------------
# Conservative stage-marker vocabulary.  Matches common banner forms:
#   [Build] / <Gate> / Stage: Review / [Land] — case-sensitive exact matches.
# Each token becomes its capitalised form in the output entry.
_STAGE_TOKENS = ("Build", "Gate", "Review", "Land", "Test", "Lint",
                 "Analyse", "Deploy", "Merge")
_STAGE_RE = re.compile(
    r'\[(?P<st>' + '|'.join(_STAGE_TOKENS) + r')\]'
    r'|<(?P<angle>' + '|'.join(_STAGE_TOKENS) + r')>'
    r'|Stage:\s*(?P<colon>' + '|'.join(_STAGE_TOKENS) + r')\b',
)


def _format_time(hhmmss: str) -> str:
    """Render an HHMMSS string as ``HH:MM:SS``."""
    if len(hhmmss) == 6:
        return f"{hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:]}"
    return hhmmss


def _parse_ticket_time(name: str) -> tuple[str, str]:
    """Extract ``(ticket_slug, time_str)`` from a filename stem.

    Pattern: ``<ticket>-<HHMMSS>``, where ticket may itself contain dashes.
    The HHMMSS part is exactly six digits immediately before ``.log``.
    When the suffix isn't 6-digit, the entire stem is the ticket and
    time defaults to empty-string (file ordered by mtime later).
    """
    m = re.match(r'^(.+)-(\d{6})\.log$', name)
    if m:
        return m.group(1), m.group(2)
    stem = Path(name).stem  # drops .log
    return stem, ""


def _scan_file(filepath: Path) -> list[dict]:
    """Read *filepath* line-by-line, attributing each with ticket, stage, time.

    Best-effort but NEVER silent: an unreadable file or a mid-file read failure
    yields one attributed ``[read error]`` marker entry (and keeps the lines
    already read) so a broken file is visible in the stream instead of vanishing.
    """
    entries: list[dict] = []
    ticket, hhmmss = _parse_ticket_time(filepath.name)
    time_str = _format_time(hhmmss)
    current_stage = ""
    try:
        fh = filepath.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        return [{
            "ticket": ticket,
            "stage": "",
            "time": time_str,
            "text": f"[read error] {filepath.name}: {exc.__class__.__name__}",
        }]
    with fh:
        while True:
            try:
                raw = fh.readline()
            except OSError as exc:
                # Mid-file failure: keep the lines read so far; mark the gap.
                entries.append({
                    "ticket": ticket,
                    "stage": current_stage,
                    "time": time_str,
                    "text": f"[read error] {filepath.name}: {exc.__class__.__name__}",
                })
                break
            if not raw:
                break
            text = raw.rstrip("\n\r")
            m = _STAGE_RE.search(text)
            if m:
                current_stage = m.group("st") or m.group("angle") or m.group("colon")
            entries.append({
                "ticket": ticket,
                "stage": current_stage,
                "time": time_str,
                "text": text,
            })
    return entries


def _file_sort_key(entry: Path, day_date: datetime.date | None) -> float:
    """Chronological sort key for *entry* in seconds-since-midnight of its day.

    HHMMSS-named files use the embedded time; other files fall back to their
    mtime rebased onto the day's local midnight, so both kinds interleave on
    one scale (a raw mtime is ~10⁹ while an HHMMSS int tops out at 235959,
    which used to push every fallback file behind every named file).
    """
    _, hhmmss = _parse_ticket_time(entry.name)
    if hhmmss:
        key = float(int(hhmmss[:2]) * 3600 + int(hhmmss[2:4]) * 60 + int(hhmmss[4:]))
    else:
        try:
            key = float(entry.stat().st_mtime)
        except OSError:
            key = 0.0
        if day_date is not None:
            midnight = datetime.datetime.combine(day_date, datetime.time.min).timestamp()
            key -= midnight
    return min(max(key, 0.0), 86399.0)


def read_day_log(cfg: Config, app_name: str | None, date_str: str) -> dict:
    """Read all run-log files under one day folder and return a merged chronological stream.

    Parameters
    ----------
    cfg : Config
        Configuration (provides ``log_root`` anchor).
    app_name : str | None
        Application name; slugified via ``_safe_slug`` then resolved against
        ``log_root(cfg)/<app>/YYYY-MM-DD/``.
    date_str : str
        Date in ``YYYY-MM-DD`` format.

    Returns
    -------
    dict
        ``{"date": <date_str>, "entries": [{ticket, stage, time, text}, ...]}``
        sorted chronologically (HHMMSS first, mtime fallback — both normalised
        to seconds-since-midnight so the two interleave; filename breaks ties),
        preserving in-file line order.
    """
    root = log_root(cfg)
    app_slug = _safe_slug(app_name or "default")
    day_dir = root / app_slug / date_str

    if not day_dir.is_dir():
        return {"date": date_str, "entries": []}

    try:
        day_date = datetime.date.fromisoformat(date_str)
    except ValueError:
        day_date = None

    files: list[tuple[float, str, Path]] = []  # (sort_key, full_name, path)
    for entry in day_dir.iterdir():
        if not entry.is_file() or not entry.name.lower().endswith(".log"):
            continue
        files.append((_file_sort_key(entry, day_date), entry.name, entry))

    files.sort(key=lambda t: (t[0], t[1]))

    result: list[dict] = []
    for _, _name, fpath in files:
        result.extend(_scan_file(fpath))

    return {"date": date_str, "entries": result}
