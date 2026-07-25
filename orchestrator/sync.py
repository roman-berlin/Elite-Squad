"""orchestrator/sync.py — Mac ↔ server state sync over git.

The brain-stem (councils, chat, small-talk) runs 24/7 on the VPS; implementation (ticket builds) runs
on the Mac. Each machine writes its OWN ``audit.jsonl`` locally. To give both machines one unified
picture — the server's cockpit + councils should reflect what the Mac shipped, and the Mac's cockpit
should reflect the server's councils — every machine PUBLISHES a copy of its audit to
``shared/<host>.jsonl`` and exchanges the ``shared/`` directory through git.

Design choices (and the constraints they satisfy):

* **Single-writer files.** Only one host ever writes ``shared/<host>.jsonl``. Two machines therefore
  never touch the same path, so there is no merge driver, no union-merge, and no append race — git
  fast-forwards cleanly. ``dashboard.audit_lines`` reads the local audit PLUS every other host's file
  and collapses exact-duplicate lines.

* **A dedicated orphan ``unit-state`` branch, in its own clone (``.unit-state/``).** Runtime state is
  NOT committed to ``main``/``dev`` — that would pollute code history and fight the server's
  ``git reset --hard origin/main`` promotion (exactly the "Mac/server pull conflicts" the .gitignore
  warns about). The state clone lives beside the repo, is gitignored, and tracks an orphan branch that
  carries only ``shared/`` — never code. The main working tree stays on whatever branch it was on.

* **Best-effort, never load-bearing.** Any git hiccup (offline, no push auth on the server, a push
  race) is caught and reported in the return dict; the cockpit still works from local audit alone.
  The server only needs to *read* (pull) to reflect the Mac — pushing its own councils back is a
  bonus that no-ops harmlessly if the server has no push credentials.

Host id: ``$GENERAL_HOST_ID`` if set (e.g. ``mac`` / ``server`` in the launchd/systemd env), else the
machine hostname. Set it explicitly on both machines for clean, stable file names.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NotRequired, TypedDict

from .config import Config

STATE_DIR_NAME = ".unit-state"   # the dedicated state clone, sibling of audit.jsonl (gitignored)
STATE_BRANCH = "unit-state"      # orphan branch: carries only shared/, never code
_CLONE_DEPTH = "50"              # shallow — we only ever need the tip of the state branch

# EU-526 — git gc throttle plumbing (no invocation yet; just the due-decision layer).
_GC_INTERVAL_HOURS = 24          # minimum seconds between successive ``git gc`` calls
_GC_SENTINEL_NAME = ".last_gc"   # untracked sentinel: survives ``reset --hard FETCH_HEAD``


def _safe_host(raw: str) -> str:
    """Filesystem-safe host token: alnum / - / _ only, lowercased; every other char (space, '/', '.')
    becomes '-'. Never empty. Used for every ``shared/<host>.jsonl`` token so a token can never contain
    a path separator (escaping shared/) or whitespace (breaking the whitespace-split exclude guard)."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in (raw or "")).strip("-_").lower()
    return safe or "host"


def host_id(cfg: Config | None = None) -> str:
    """Stable, filesystem-safe id for THIS machine's shared file."""
    return _safe_host((os.environ.get("GENERAL_HOST_ID") or socket.gethostname() or "host").strip())


def pull_only() -> bool:
    """A read-only consumer (e.g. the VPS without git write access): pull peers' audits but never try
    to push our own. Set ``GENERAL_SYNC_PULL_ONLY=1`` on that box so sync exits clean instead of 403-ing
    every run. The box's own audit is still read locally by ``dashboard.audit_lines`` — only the
    publish-back is skipped."""
    return os.environ.get("GENERAL_SYNC_PULL_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}


def _repo_root(cfg: Config) -> Path:
    """The General repo root, derived from audit_path so tests stay isolated in their tmp dirs.

    Review fix (2026-07-05): audit.jsonl used to live AT the repo root; QW2 moved the runtime
    state into a state/ subdirectory, so the audit dir's PARENT is the root whenever the dir uses
    that convention. Without this, .unit-state/ and the pulled memory/UNIT.live.md would land
    under state/ while every reader (memory.py, cockpit) keeps using the repo root."""
    root = Path(cfg.audit_path).resolve().parent
    if root.name == "state":
        root = root.parent
    return root


def state_dir(cfg: Config) -> Path:
    return _repo_root(cfg) / STATE_DIR_NAME


def shared_dir(cfg: Config) -> Path:
    return state_dir(cfg) / "shared"


def shared_files(cfg: Config) -> list[Path]:
    """Every host's published audit visible to this machine (from the state clone)."""
    d = shared_dir(cfg)
    return sorted(d.glob("*.jsonl")) if d.is_dir() else []


# EU-428 AC1 — transport honesty. A peer whose newest published EVENT is older than this is flagged
# STALE in the `sync` cron log. Deliberately an EVENT-age threshold, not a file-mtime threshold: a
# no-op `git pull` of unchanged data refreshes the file's mtime while the event inside stays ancient,
# which is exactly how the dead Mac heartbeat hid for 25 days behind a healthy-looking `pulled=True`.
STALE_PEER_S = 3600  # 1 hour: well past the ~15-min publish cadence, short of the watcher's 2h alert

_TS_FMTS = ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S")


def _parse_iso_epoch(ts: str) -> float | None:
    """An ISO-8601 audit ts (with or without a +HHMM offset) as a unix epoch, or None when it can't be
    parsed. Bare (no offset) timestamps are read as UTC. Used to age peer events chronologically."""
    if not ts:
        return None
    s = str(ts).strip().strip('"').strip()
    for fmt in _TS_FMTS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def peer_ages(cfg: Config) -> dict[str, float | None]:
    """Newest-EVENT age in seconds for each synced peer audit, keyed by host stem.

    The age is parsed from the newest ``ts`` INSIDE ``shared/<peer>.jsonl`` — NEVER the file's mtime.
    A successful but empty ``git pull`` refreshes mtime while the event inside stays ancient, so a
    mtime-based age would read "fresh" while transporting nothing (the EU-428 defect). ``None`` for a
    peer file with no parseable ts, so it can never be misreported as fresh."""
    out: dict[str, float | None] = {}
    now = time.time()
    for p in shared_files(cfg):
        newest: float | None = None
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            out[p.stem] = None
            continue
        for m in re.finditer(r'"ts"\s*:\s*"([^"]+)"', text):
            epoch = _parse_iso_epoch(m.group(1))
            if epoch is not None and (newest is None or epoch > newest):
                newest = epoch
        out[p.stem] = (now - newest) if newest is not None else None
    return out


def _fmt_age(seconds: float | None) -> str:
    """Compact human age for a sync-log peer: ``6m`` / ``3h`` / ``25d`` / ``?``."""
    if seconds is None:
        return "?"
    s = int(seconds)
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def peer_summary(cfg: Config) -> str:
    """The ``peers=`` fragment for the ``sync`` log line: each peer with its newest-event age, and a
    ``STALE`` marker once past ``STALE_PEER_S``, so a peer transporting nothing is visibly stale in
    the cron log (EU-428 AC1). ``'(none yet)'`` when no peers are synced yet."""
    ages = peer_ages(cfg)
    if not ages:
        return "(none yet)"
    parts: list[str] = []
    for host in sorted(ages):
        age = ages[host]
        stale = " STALE" if (age is not None and age >= STALE_PEER_S) else ""
        parts.append(f"{host}(age={_fmt_age(age)}){stale}")
    return ", ".join(parts)


def _git(cwd: Path, *args: str, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    # GIT_TERMINAL_PROMPT=0: a push over HTTPS with no cached credentials must FAIL FAST, never block
    # forever waiting for a username/password the cockpit thread can't answer (the "stuck spinner").
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    )


def _origin_url(root: Path) -> str | None:
    r = _git(root, "remote", "get-url", "origin")
    url = r.stdout.strip()
    return url if (r.returncode == 0 and url) else None


def _set_identity(sd: Path) -> None:
    """Local commit identity for the state clone, so ``commit``/``rebase`` work even on a fresh server
    clone (or in CI) where no global git ``user.*`` is configured."""
    _git(sd, "config", "user.email", "unit@localhost")
    _git(sd, "config", "user.name", "Elite Unit")


def ensure_state_clone(cfg: Config) -> Path | None:
    """Make sure ``.unit-state/`` exists as a clone tracking the orphan ``unit-state`` branch.

    Tries to clone the existing remote branch first; if it does not exist yet (first run ever),
    clones the default branch and switches to a fresh orphan ``unit-state`` (empty working tree).
    Returns the state-clone path, or ``None`` if there is no usable git origin.
    """
    sd = state_dir(cfg)
    if (sd / ".git").exists():
        return sd
    root = _repo_root(cfg)
    url = _origin_url(root)
    if not url:
        return None
    sd.parent.mkdir(parents=True, exist_ok=True)
    if sd.exists():
        shutil.rmtree(sd, ignore_errors=True)
    # Fast path: the state branch already exists on the remote.
    ok = _git(root, "clone", "--branch", STATE_BRANCH, "--single-branch",
              "--depth", _CLONE_DEPTH, url, str(sd))
    if ok.returncode == 0 and (sd / ".git").exists():
        _set_identity(sd)
        return sd
    # First run: no unit-state branch yet. Clone default, then start a clean orphan.
    if sd.exists():
        shutil.rmtree(sd, ignore_errors=True)
    base = _git(root, "clone", "--single-branch", "--depth", _CLONE_DEPTH, url, str(sd))
    if base.returncode != 0 or not (sd / ".git").exists():
        return None
    # `git switch --orphan` (git ≥ 2.23) clears the index + working tree — no code carries over.
    orphan = _git(sd, "switch", "--orphan", STATE_BRANCH)
    if orphan.returncode != 0:
        return None
    _set_identity(sd)
    (sd / "shared").mkdir(parents=True, exist_ok=True)
    return sd


# EU-526 — git gc throttle plumbing (decision layer only; invocation follows in a later ticket).

def _gc_sentinel(cfg: Config) -> Path:
    """Path to the ``.last_gc`` sentinel inside the state clone."""
    return state_dir(cfg) / _GC_SENTINEL_NAME


def _gc_is_due(cfg: Config, force: bool = False) -> bool:
    """Decide whether a ``git gc`` run is due.

    Returns **True** when:
    * ``force=True`` (caller explicitly requested it), or
    * the sentinel file is missing (fresh clone, never gc'd), or
    * the sentinel's mtime is older than ``_GC_INTERVAL_HOURS``, or
    * any OSError occurs reading the sentinel (best-effort — don't crash sync over a stale fs).

    Returns **False** when the sentinel exists and was touched within the throttle window.
    Never spawns a subprocess.
    """
    if force:
        return True
    try:
        p = _gc_sentinel(cfg)
        st = p.stat()
        age = time.time() - st.st_mtime
        return age >= _GC_INTERVAL_HOURS * 3600
    except OSError:
        return True  # absent or unreadable → safe to gc


def _mark_gc_done(cfg: Config) -> None:
    """Touch the ``.last_gc`` sentinel to refresh its mtime.

    Best-effort: an OSError is swallowed so a failed sentinel write can never
    crash a future sync run.  Uses ``Path.touch()`` which updates mtime on
    an existing file — exactly the "refresh the throttle clock" semantics
    required.
    """
    try:
        p = _gc_sentinel(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    except OSError:
        pass  # sidecar-style best-effort: failure must not crash sync


# EU-527 — run ``git gc`` inside the state clone, throttled by the sentinel.


def gc_state_clone(cfg: Config, force: bool = False) -> dict[str, bool | str]:
    """Run ``git gc --prune=now`` in the ``.unit-state`` clone.

    Throttled behind ``_gc_is_due()`` so that real git-gc only fires once per
    ``_GC_INTERVAL_HOURS`` window unless *force* overrides.  A missing .unit-state
    directory (no clone yet) is treated as "skip" rather than an error.

    Never raises.  Returns one of::

        {"ok": True,  "ran": True}            # gc ran and exited cleanly
        {"ok": True,  "ran": False, "reason": "..."}  # skipped (not due / no clone)
        {"ok": False, "ran": True,  "error": "..."}   # subprocess failed or raised
    """
    sd = state_dir(cfg)

    # Guard against running inside a non-existent clone.
    if not (sd / ".git").exists():
        return {"ok": True, "ran": False, "reason": "no-clone"}

    # Only proceed when due (or forced).
    if not _gc_is_due(cfg, force):
        return {"ok": True, "ran": False, "reason": "not-due"}

    try:
        result = _git(sd, "gc", "--prune=now")
        if result.returncode == 0:
            _mark_gc_done(cfg)
            return {"ok": True, "ran": True}
        else:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            msg = stderr or stdout or f"returncode={result.returncode}"
            return {"ok": False, "ran": True, "error": msg[:300]}
    except Exception as e:  # noqa: BLE001 — must never raise, mirrors EU-496 compaction contract
        return {"ok": False, "ran": True, "error": str(e)[:300]}


def compact_state_branch(cfg: Config) -> dict[str, bool | str | None]:
    """Rebuild the ``unit-state`` branch's history into a single commit.

    Operates on the existing state clone (the same one ``ensure_state_clone`` maintains), issuing
    an orphan-switch → stage shared/ → commit → force-push sequence.  History is rewritten; nothing
    is deleted (EU-428 guard: no ``git branch -D``, no ``rm`` of ``shared/``).

    ``git switch --orphan`` (git ≥ 2.23) removes ALL tracked files from the working tree (git-switch
    man page) — in a healthy state clone ``shared/`` is tracked, so the switch would wipe it before
    step 2 could stage it (``git add shared`` → ``pathspec 'shared' did not match any files``). The
    payload is therefore snapshotted to a scratch dir AROUND the switch — a plain filesystem copy,
    not a git command: the git sequence below is exactly the four prescribed, in order — and restored
    immediately after the switch.

    .. note:: The local clone is intentionally left on ``tmp-compact``. The next ``git_sync`` call
       will reconcile it via its internal ``fetch + reset --hard FETCH_HEAD`` cycle.

    Returns ``{ok, step, error}`` — best-effort, never raises.
    """
    out: dict[str, bool | str | None] = {"ok": False, "step": None, "error": None}
    sd = ensure_state_clone(cfg)
    if sd is None:
        out["error"] = "no git origin for state sync"
        return out

    shared = sd / "shared"
    scratch = Path(tempfile.mkdtemp(prefix="unit-state-compact-"))
    try:
        if shared.is_dir():
            shutil.copytree(shared, scratch / "shared")
        steps: list[tuple[str, ...]] = [
            ("switch", "--orphan", "tmp-compact"),
            ("add", "shared"),
            ("commit", "-m", "compact: collapse unit-state history"),
            ("push", "--force", "origin", f"HEAD:{STATE_BRANCH}"),
        ]
        for i, args in enumerate(steps):
            r = _git(sd, *args)
            if r.returncode != 0:
                out["step"] = " ".join(args)
                out["error"] = (r.stderr or r.stdout or f"{args[0]} failed").strip()[:300]
                return out
            if i == 0 and (scratch / "shared").is_dir() and not shared.is_dir():
                # The orphan switch emptied the working tree — restore the payload for `add shared`.
                shutil.copytree(scratch / "shared", shared)
        out["ok"] = True
        return out
    except (subprocess.SubprocessError, OSError) as e:
        out["error"] = str(e)[:300]
        return out
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# ---------------------------------------------------------------------------
# EU-496 — throttle compact_state_branch behind a cadence gate
# ---------------------------------------------------------------------------


def _compact_sidecar(cfg: Config) -> Path:
    """Sidecar path for tracking the last successful state-compaction timestamp."""
    return Path(cfg.audit_path).parent / "last_state_compact.txt"


def _state_commit_count(cfg: Config) -> int | None:
    """The TRUE commit count of the ``unit-state`` branch, or None when it can't be known.

    The state clone bootstraps shallow (``--depth 50``), so a raw ``rev-list --count`` caps at 50
    and the default threshold (150) could NEVER fire. Complete the history first — a one-time
    ``fetch --unshallow``; git removes the shallow graft so every later sync skips straight to the
    (local, cheap) count and ordinary fetches stay incremental. Best-effort: an offline unshallow
    simply leaves the count capped for this cycle (the cadence leg still covers it), and any
    git / FS failure yields None so the gate skips without ever failing the sync.

    Counts the LOCAL ``unit-state`` ref, which ``git_sync`` keeps at the fetched tip via its
    ``reset --hard FETCH_HEAD`` and which ``compact_state_branch_now`` advances to the compacted
    tip after a successful compaction — counting a ref nobody maintains would go stale-high the
    moment a compaction force-pushes (the clone is left on ``tmp-compact``), re-firing the gate
    on every subsequent sync.
    """
    sd = ensure_state_clone(cfg)
    if sd is None:
        return None
    try:
        shallow = _git(sd, "rev-parse", "--is-shallow-repository")
        if shallow.returncode == 0 and (shallow.stdout or "").strip() == "true":
            # One-time. rc deliberately ignored: offline, the clone stays shallow and the count
            # stays capped at the depth — leg (a) just skips this cycle, the cadence leg covers it.
            _git(sd, "fetch", "--unshallow", "origin", STATE_BRANCH)
        r = _git(sd, "rev-list", "--count", STATE_BRANCH)
        return int((r.stdout or "").strip()) if r.returncode == 0 else None
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def _should_compact_state(cfg: Config) -> bool:
    """Decide whether ``compact_state_branch(cfg)`` should run now.

    Returns True when **either**:
    (a) the ``unit-state`` branch's TRUE history exceeds ``GENERAL_STATE_COMPACT_THRESHOLD``
        commits (default 150 — the clone is unshallowed before counting, so the threshold is
        actually reachable, not capped at the depth-50 shallow window), or
    (b) more than ``GENERAL_STATE_COMPACT_DAYS`` (default 7) have elapsed since the last
        successful compaction, tracked in the sidecar file.

    Re-evaluated on EVERY call — deliberately no process-lifetime cache, so a long-lived cockpit
    process calling ``git_sync`` repeatedly can never freeze the throttle on a stale verdict.
    Best-effort — any git / filesystem error returns False so that a stale sidecar or missing
    branch never prevents the rest of the sync from completing. Also returns False when
    ``pull_only()`` is set (the VPS has no push credentials; compaction requires force-push).
    """
    if pull_only():
        return False
    try:
        threshold = int(os.environ.get("GENERAL_STATE_COMPACT_THRESHOLD", "150"))
        days = int(os.environ.get("GENERAL_STATE_COMPACT_DAYS", "7"))
    except ValueError:
        return False

    # (a) Commit-count leg — the branch's true history (unshallowed), not the shallow window.
    count = _state_commit_count(cfg)
    above = count is not None and count > threshold

    # (b) Weekly cadence leg — read the sidecar file (best-effort).
    cadence = False
    try:
        last_ts = float(_compact_sidecar(cfg).read_text(encoding="utf-8", errors="replace").strip())
        cadence = (time.time() - last_ts) >= days * 86400
    except (OSError, ValueError):
        pass  # absent / unreadable → never compacted here → don't storm a fresh clone

    return above or cadence


def compact_state_branch_now(cfg: Config) -> None:
    """Run ``compact_state_branch(cfg)`` with best-effort error handling.

    Never raises into the caller. On success: (1) advances the LOCAL ``unit-state`` ref to the
    compacted tip — the clone is left on ``tmp-compact`` and the stale ref would otherwise keep
    the commit-count leg over threshold, re-firing compaction on EVERY subsequent sync — and
    (2) writes the current epoch into the sidecar so the cadence leg skips until the next window
    (EU-496 bookkeeping).
    """
    sd = ensure_state_clone(cfg)
    if sd is None:
        return  # no origin → silently skip (same signal as _should_compact_state=False)
    rc = compact_state_branch(cfg)
    if not rc["ok"]:
        return
    _git(sd, "update-ref", f"refs/heads/{STATE_BRANCH}", "HEAD")
    try:
        _compact_sidecar(cfg).write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass  # sidecar write failure must not crash sync


def _tail_window(text: str, max_records: int, max_bytes: int) -> str:
    """Return the newest-complete-record tail of *text* bounded by *max_records* / *max_bytes*.

    Walks backwards from the end, keeping only complete newline-delimited jsonl records.
    **Always retains at least the final record** (the newest event), even if that single
    record alone would overflow ``max_bytes`` — this is what preserves EU-428 freshness
    (``peer_ages`` / ``STALE_PEER_S`` can always find a recent ``ts``).

    When the source already fits within both bounds the text is returned *byte-identical*
    (no reformatting, no added/stripped trailing newline), so callers testing verbatim
    copies on small files remain unaffected.

    Bounds come from environment variables::

        GENERAL_PUBLISH_MAX_RECORDS   # default 2000
        GENERAL_PUBLISH_MAX_BYTES     # default 1 MiB

    Invalid values silently fall back to defaults — never raise on the best-effort sync path.

    .. note:: Trade-off: the peer host (VPS cockpit) sees only the recent window of this
       host's audit via ``shared/<host>.jsonl``. Full history remains intact in the publisher's
       own local ``audit.jsonl``. This is intentional: the shared copy is a lightweight signal,
       not a full replica.
    """
    if not text:
        return ""

    # Fast path: source fits entirely — return byte-identical (no allocation churn).
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        encoded = text.encode("utf-8", errors="replace")

    n_lines = text.count("\n") + (0 if text.endswith("\n") else 1)
    if n_lines <= max_records and len(encoded) <= max_bytes:
        return text

    lines = text.split("\n")  # ['l1', 'l2', '', ...] for trailing-newline JSONL
    total = len(lines)

    # Start from the very end and walk backward, consuming records.
    pos = total
    records_kept = 0
    byte_count = 0

    while pos > 0:
        j = pos - 1
        # Strip any trailing empty element produced by a final "\n"
        while j >= 0 and not lines[j]:
            j -= 1
        if j < 0:
            break
        chunk_size = len(lines[j].encode("utf-8")) + 1  # +1 for "\n"
        if records_kept == 0 or records_kept < max_records and byte_count + chunk_size <= max_bytes:
            byte_count += chunk_size
            records_kept += 1
            pos = j
        else:
            break

    # Edge case: after dropping below the record limit, the byte limit may
    # still be exceeded (a single large record dominates). Trim from the left.
    while records_kept > 1 and byte_count > max_bytes:
        i = 0
        while i < pos and not lines[i]:
            i += 1
        if i >= pos:
            break
        removed = len(lines[i].encode("utf-8")) + 1
        byte_count -= removed
        records_kept -= 1
        pos = i + 1

    # Ensure at least one record survives (preserves freshness guarantee).
    if records_kept == 0 and pos < total:
        pos = 0

    # Reassemble, preserving the original trailing-newline pattern.
    result_parts = lines[pos:]
    result = "\n".join(result_parts)

    # Force trailing newline if the source had one (JSONL contract).
    if text.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def _get_publish_bounds() -> tuple[int, int]:
    """Read GENERAL_PUBLISH_MAX_RECORDS and GENERAL_PUBLISH_MAX_BYTES, falling back to safe
    defaults on ValueError. Returns (max_records, max_bytes)."""
    try:
        max_records = max(1, int(os.environ.get("GENERAL_PUBLISH_MAX_RECORDS", "2000")))
    except (ValueError, TypeError):
        max_records = 2000
    try:
        max_bytes = max(1, int(os.environ.get("GENERAL_PUBLISH_MAX_BYTES", str(1 << 20))))
    except (ValueError, TypeError):
        max_bytes = 1 << 20
    return max_records, max_bytes


def publish(cfg: Config, sd: Path | None = None) -> Path:
    """Publish this host's most-recent audit events to ``shared/<host>.jsonl`` in the state clone.

    The published file is a **bounded tail window** of the live ``audit.jsonl``:
    only the newest *N* complete records (up to ~M bytes) are shipped, configured via
    ``GENERAL_PUBLISH_MAX_RECORDS`` (default 2000) and ``GENERAL_PUBLISH_MAX_BYTES`` (default 1 MiB).
    A growing source audit does NOT make the published file grow past the bound — each publish
    overwrites with the latest tail window.

    **Trade-off:** the peer host (e.g. VPS cockpit reading ``shared/<host>.jsonl``) sees only the
    recent window of this host's audit. Full history remains intact in the publisher's own local
    ``audit.jsonl``, available to the local cockpit and the ``general sync`` command. This is
    deliberate: the shared file is a lightweight freshness signal, not a full audit replica.

    The newest event is always present in the published file — even a single massive record
    is kept whole, preserving ``peer_ages()`` / ``STALE_PEER_S`` freshness guarantees.

    Single-writer invariant: only ``shared/<host>.jsonl`` is written. Other hosts' files are
    untouched.
    """
    sd = sd or state_dir(cfg)
    dst = sd / "shared" / f"{host_id(cfg)}.jsonl"
    dst.parent.mkdir(parents=True, exist_ok=True)
    src = Path(cfg.audit_path)
    raw = src.read_text(encoding="utf-8") if src.exists() else ""
    max_records, max_bytes = _get_publish_bounds()
    dst.write_text(_tail_window(raw, max_records, max_bytes), encoding="utf-8")
    return dst


# Result shapes of the sync/promote operations below — fixed keys, documented once here so call
# sites read a named contract instead of a loose mapping.
class GitSyncStatus(TypedDict):
    host: str
    pulled: bool
    pushed: bool | None
    hosts: list[str]
    error: str | None
    gc: NotRequired[dict[str, bool | str]]


class ServerStatePullStatus(TypedDict):
    attempted: bool
    pulled: bool
    error: str | None


class ServerAuditPullStatus(TypedDict):
    attempted: bool
    pulled: bool
    host: str | None
    error: str | None


class PromoteStatus(TypedDict):
    ok: bool
    ahead_before: int
    pushed: bool
    error: str | None


class AppPromoteStatus(TypedDict):
    ahead: int
    base: str
    prot: str
    error: str | None


class AppShipStatus(TypedDict):
    ok: bool
    ahead_before: int
    pushed: bool
    error: str | None
    base: str
    prot: str
    app: str


def git_sync(cfg: Config) -> GitSyncStatus:
    """Exchange ``shared/`` with the remote: pull every host's latest, publish ours, push it back.

    Returns ``{host, pulled, pushed, hosts, error}``. Best-effort — a failure to push (e.g. the server
    has no credentials) still leaves ``pulled`` true, so the server has the Mac's data either way.
    """
    out: GitSyncStatus = {"host": host_id(cfg), "pulled": False, "pushed": False,
                           "hosts": [], "error": None}
    sd = ensure_state_clone(cfg)
    if sd is None:
        out["error"] = "no git origin for state sync"
        return out
    try:
        # 1) Pull others' latest. fetch + reset (not merge) keeps the branch a clean fast-forward; we
        #    publish AFTER the reset so it never clobbers our freshly-written file.
        fetch = _git(sd, "fetch", "origin", STATE_BRANCH)
        if fetch.returncode == 0:
            # Reset to FETCH_HEAD, not origin/<branch>: the bootstrap clone is --single-branch on the
            # DEFAULT branch, so its remote-tracking refspec never covers unit-state and origin/<branch>
            # may be missing/stale. FETCH_HEAD is exactly what we just fetched — always the right tip.
            _git(sd, "reset", "--hard", "FETCH_HEAD")
            out["pulled"] = True

        if pull_only():
            # Read-only consumer: we've pulled the peers' audits — never attempt a push (no recurring
            # 403, clean exit 0). Our own audit is still read locally by dashboard.audit_lines.
            out["hosts"] = [p.stem for p in shared_files(cfg)]
            out["pushed"] = None
        else:
            # 2) Publish our own audit and stage ONLY it. Single-writer: a host owns exactly
            #    shared/<its-host>.jsonl — never `git add shared` (which would stage a peer/server
            #    file we pulled in over SSH, and push it back, breaking the single-writer invariant).
            publish(cfg, sd)
            out["hosts"] = [p.stem for p in shared_files(cfg)]
            _git(sd, "add", f"shared/{host_id(cfg)}.jsonl")
            if _git(sd, "diff", "--cached", "--quiet").returncode == 0:
                out["pushed"] = True   # nothing staged since last sync — already in step with remote
            else:
                # 3) Commit + push. On a race, rebase our single commit onto the remote tip and retry once.
                _git(sd, "commit", "-m", f"sync: {host_id(cfg)} audit")
                push = _git(sd, "push", "origin", f"HEAD:{STATE_BRANCH}")
                if push.returncode != 0:
                    _git(sd, "fetch", "origin", STATE_BRANCH)
                    _git(sd, "rebase", "FETCH_HEAD")
                    push = _git(sd, "push", "origin", f"HEAD:{STATE_BRANCH}")
                out["pushed"] = push.returncode == 0
                if not out["pushed"]:
                    out["error"] = (push.stderr or push.stdout or "push failed").strip()[:200]
    except (subprocess.SubprocessError, OSError) as e:
        out["error"] = str(e)[:200]

    # ── EU-496: throttle & wrap compaction best-effort (after all pull/push paths) ──
    # Compaction must never fail the calling sync — any exception is caught AND logged with its
    # detail (type + message): a bare "swallowed" line hid the cause and made a wedged compaction
    # undiagnosable from the sync cron log.
    try:
        if _should_compact_state(cfg):
            compact_state_branch_now(cfg)
    except Exception as e:  # noqa: BLE001 — strictly best-effort by contract, never re-raise
        print(f"[sync] compaction exception swallowed: {type(e).__name__}: {e}", flush=True)

    # ── EU-530: invoke per-sync gc_state_clone (throttled sentinel makes it a no-op 99% of time) ──
    # Same best-effort pattern: caught and reported in the sync dict, never raised into caller.
    try:
        out["gc"] = gc_state_clone(cfg)
    except Exception as e:  # noqa: BLE001 — strictly best-effort by contract, never re-raise
        out["gc"] = {"ok": False, "ran": True, "error": str(e)[:300]}
        print(f"[sync] gc exception swallowed: {type(e).__name__}: {e}", flush=True)

    return out


def pull_server_state(cfg: Config) -> ServerStatePullStatus:
    """Mac-side server→Mac bridge over SSH: copy the server's officer-canonical living log down so the
    Mac's builds read the latest server-learned lessons. One-directional and read-only on the server —
    we just `scp` a file out, so the server never needs git write access.

    No-op unless ``GENERAL_SERVER_SSH`` (e.g. ``ubuntu@1.2.3.4``) is set and we're not the server itself
    (``GENERAL_SYNC_PULL_ONLY``). ``GENERAL_SERVER_REPO`` overrides the remote repo dir (default
    ``General``). Best-effort: any SSH hiccup is reported, never fatal."""
    out: ServerStatePullStatus = {"attempted": False, "pulled": False, "error": None}
    host = os.environ.get("GENERAL_SERVER_SSH", "").strip()
    if not host or pull_only():
        return out
    out["attempted"] = True
    remote_repo = os.environ.get("GENERAL_SERVER_REPO", "General").strip() or "General"
    src = f"{host}:{remote_repo}/memory/UNIT.live.md"
    dst = _repo_root(cfg) / "memory" / "UNIT.live.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", src, str(dst)],
            capture_output=True, text=True, timeout=60)
        out["pulled"] = r.returncode == 0
        if r.returncode != 0:
            out["error"] = (r.stderr or r.stdout or "scp failed").strip()[:200]
    except (subprocess.SubprocessError, OSError) as e:
        out["error"] = str(e)[:200]
    return out


def pull_server_audit(cfg: Config) -> ServerAuditPullStatus:
    """Mac-side server→Mac AUDIT bridge over SSH: scp the server's live ``audit.jsonl`` down so the Mac
    cockpit MIRRORS the server's runs (EU-181).

    The server runs sync pull-only (no git push credentials), so it never publishes its own audit to the
    ``unit-state`` git branch — without this the Mac never sees the server and the two cockpits diverge.
    We pull it over the SAME SSH bridge as ``UNIT.live.md`` (``pull_server_state``) and land it in the
    machine-LOCAL ``<audit-dir>/shared/<server>.jsonl`` — NOT the git-tracked state clone. That path is
    read by ``dashboard._audit_paths`` (the ``<audit-dir>/shared`` probe) and gitignored, so the pulled
    server audit shows in the Mac cockpit but can NEVER be re-published to the shared git branch (which
    would break the single-writer invariant and make the server double-count its own audit). Keeping it
    out of the git clone entirely sidesteps every tracking / reset --hard / exclude hazard.

    No-op unless ``GENERAL_SERVER_SSH`` is set and we are not the server (``GENERAL_SYNC_PULL_ONLY``).
    ``GENERAL_SERVER_REPO`` (default ``General``) + ``GENERAL_SERVER_AUDIT`` (default
    ``state/audit.jsonl``, relative to the repo) locate the remote file; ``GENERAL_SERVER_HOST_ID``
    (default ``server``, sanitised) names the local file. Best-effort — any hiccup is reported, never fatal."""
    out: ServerAuditPullStatus = {"attempted": False, "pulled": False, "host": None, "error": None}
    host = os.environ.get("GENERAL_SERVER_SSH", "").strip()
    if not host or pull_only():
        return out
    out["attempted"] = True
    server_host = _safe_host(os.environ.get("GENERAL_SERVER_HOST_ID", "").strip() or "server")
    out["host"] = server_host
    remote_repo = os.environ.get("GENERAL_SERVER_REPO", "General").strip() or "General"
    rel = os.environ.get("GENERAL_SERVER_AUDIT", "").strip() or "state/audit.jsonl"
    src = f"{host}:{remote_repo}/{rel}"
    # Land it beside the LOCAL audit.jsonl (gitignored, NOT the .unit-state git clone) — the cockpit
    # reads <audit-dir>/shared/*.jsonl; nothing here ever touches the shared git branch.
    dst = Path(cfg.audit_path).parent / "shared" / f"{server_host}.jsonl"
    try:
        # All filesystem writes are INSIDE the guard — a permission/FS hiccup must degrade to a
        # reported error, never raise into the `general sync` command.
        dst.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", src, str(dst)],
            capture_output=True, text=True, timeout=60)
        out["pulled"] = r.returncode == 0
        if r.returncode != 0:
            out["error"] = (r.stderr or r.stdout or "scp failed").strip()[:200]
    except (subprocess.SubprocessError, OSError) as e:
        out["error"] = str(e)[:200]
    return out


# ---------------------------------------------------------------------------
# Promote DEV -> main from the cockpit (the "Deploy" button).
# ---------------------------------------------------------------------------

def can_promote() -> bool:
    """Only a cockpit explicitly allowed to push DEV->main shows the Deploy button. The Mac launcher
    sets ``GENERAL_COCKPIT_PROMOTE=1``; the read-only server never does, so its cockpit can't promote
    (and a push there would 403 anyway)."""
    return os.environ.get("GENERAL_COCKPIT_PROMOTE", "").strip().lower() in {"1", "true", "yes", "on"}


def promote(cfg: Config) -> PromoteStatus:
    """Promote ``dev`` -> ``main`` on the REMOTE (the server auto-deploys ``main``) **without touching
    the working tree** — the unit constantly writes runtime files, so a dirty tree must never block a
    deploy (it was the old checkout-based version's "stuck spinner"). Pushes ``dev`` straight onto
    ``main``, **fast-forward only**; if they've diverged the push is rejected (never forced).
    ``{ok, ahead_before, pushed, error}``."""
    repo = _repo_root(cfg)
    out: PromoteStatus = {"ok": False, "ahead_before": 0, "pushed": False, "error": None}
    if not can_promote():
        out["error"] = "promote not allowed on this cockpit"
        return out
    # EU-367 / EU-335: the cockpit can be running a STALE local dev — autopull's fast-forward is
    # blocked whenever the unit's own runtime files leave the tree dirty — and `push dev:main` would
    # then ship OLD code to production while reporting success (the "0 shipped" daily-brief incident).
    # Fetch origin/dev and refuse unless the local dev ref is EXACTLY origin/dev, so a deploy can only
    # ever ship what is actually on the shared dev branch, and staleness surfaces instead of shipping.
    try:
        _git(repo, "fetch", "origin", "dev")
        local_dev = (_git(repo, "rev-parse", "dev").stdout or "").strip()
        origin_dev = (_git(repo, "rev-parse", "origin/dev").stdout or "").strip()
    except subprocess.TimeoutExpired:
        out["error"] = "could not verify dev is current (fetch timed out) — check network/credentials"
        return out
    except (subprocess.SubprocessError, OSError) as e:
        out["error"] = "could not verify dev is current before deploy: " + str(e)[:150]
        return out
    if local_dev and origin_dev and local_dev != origin_dev:
        behind = _git(repo, "rev-list", "--count", "dev..origin/dev")
        n_behind = int((behind.stdout or "0").strip() or "0") if behind.returncode == 0 else 0
        if n_behind > 0:
            out["error"] = (f"local dev is STALE — {n_behind} commit(s) behind origin/dev (this cockpit "
                            "is running behind). Pull dev, then deploy — refusing to ship old code.")
        else:
            out["error"] = ("local dev has commits not yet on origin/dev — push dev first, then deploy "
                            "(refusing to ship commits that aren't on the shared branch).")
        return out
    # Inlined promote_status logic: compute how far dev is ahead of main
    try:
        r = _git(repo, "rev-list", "--count", "main..dev")
        out["ahead_before"] = int((r.stdout or "0").strip() or "0") if r.returncode == 0 else 0
    except (subprocess.SubprocessError, OSError, ValueError):
        out["ahead_before"] = 0
    if out["ahead_before"] == 0:
        out["ok"] = True   # already in sync — nothing to deploy
        return out
    try:
        ps = _git(repo, "push", "origin", "dev:main")   # ff-only by default; the tree is never touched
        out["pushed"] = out["ok"] = ps.returncode == 0
        if out["ok"]:
            _git(repo, "branch", "-f", "main", "dev")   # advance the local main ref (main isn't checked out)
        else:
            err = ((ps.stderr or ps.stdout) or "").strip()
            out["error"] = ("dev and main have diverged — resolve in a terminal"
                            if ("non-fast-forward" in err or "rejected" in err)
                            else "push to origin/main failed: " + err[:150])
    except subprocess.TimeoutExpired:
        out["error"] = "push timed out — check your network / GitHub credentials"
    except (subprocess.SubprocessError, OSError) as e:
        out["error"] = str(e)[:200]
    return out


# ---------------------------------------------------------------------------
# Ship an APP's DEV -> MAIN (production) from the cockpit (the "Ship → MAIN" button).
# This is the user's own product (e.g. Automatixy), not the unit's own code.
# ---------------------------------------------------------------------------

def app_promote_status(app) -> AppPromoteStatus:
    """How far an app's base branch (DEV) is ahead of its protected branch (MAIN) — work that's tested
    on DEV but not yet shipped to production. ``{ahead, base, prot, error}``."""
    repo = Path(app.repo_path).expanduser()
    base, prot = app.base_branch, app.protected_branch
    out: AppPromoteStatus = {"ahead": 0, "base": base, "prot": prot, "error": None}
    try:
        r = _git(repo, "rev-list", "--count", f"{prot}..{base}")
        out["ahead"] = int((r.stdout or "0").strip() or "0") if r.returncode == 0 else 0
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        out["error"] = str(e)[:200]
    return out


_TICKET_KEY = re.compile(r"[A-Z][A-Z0-9]+-\d+")


def app_promote_commits(app, limit: int = 300) -> list[dict[str, str]]:
    """The commits on the app's DEV not yet on MAIN — exactly what 'Ship' will deploy — newest first:
    ``[{sha, subject, ticket}]``. ``ticket`` is the first AUTO-style key in the subject, or ''."""
    repo = Path(app.repo_path).expanduser()
    base, prot = app.base_branch, app.protected_branch
    out: list[dict[str, str]] = []
    try:
        r = _git(repo, "log", f"{prot}..{base}", "--pretty=format:%h%x1f%s", f"-{int(limit)}")
        if r.returncode != 0:
            return out
        for line in (r.stdout or "").splitlines():
            if "\x1f" not in line:
                continue
            sha, subj = line.split("\x1f", 1)
            m = _TICKET_KEY.search(subj)
            out.append({"sha": sha.strip(), "subject": subj.strip(), "ticket": m.group(0) if m else ""})
    except (subprocess.SubprocessError, OSError):
        return out
    return out


def promote_app(app) -> AppShipStatus:
    """Ship an app's DEV -> MAIN (production): a **real merge** of DEV into MAIN (MAIN keeps its own
    commits — e.g. earlier PR merges — and DEV's commits are added), then push MAIN. Done in a throwaway
    git worktree so the user's (often dirty) checkout is never touched, and so it works even when DEV
    and MAIN have diverged (a fast-forward can't). If MAIN is a protected branch the push is rejected
    and we say so — ship via a PR. The cockpit Ship button. ``{ok, ahead_before, pushed, error, ...}``."""
    repo = Path(app.repo_path).expanduser()
    base, prot = app.base_branch, app.protected_branch
    out: AppShipStatus = {"ok": False, "ahead_before": 0, "pushed": False, "error": None,
                           "base": base, "prot": prot, "app": app.name}
    if not can_promote():
        out["error"] = "shipping is disabled on this cockpit (read-only box)"
        return out
    out["ahead_before"] = app_promote_status(app).get("ahead", 0)
    if out["ahead_before"] == 0:
        out["ok"] = True   # already shipped — nothing ahead
        return out
    wt = tempfile.mkdtemp(prefix="general-ship-")
    try:
        _git(repo, "fetch", "origin", base, prot, timeout=120)
        add = _git(repo, "worktree", "add", "--detach", "--force", wt, f"origin/{prot}")
        if add.returncode != 0:
            out["error"] = "couldn't stage the merge: " + ((add.stderr or add.stdout) or "").strip()[:150]
            return out
        wtp = Path(wt)
        mg = _git(wtp, "merge", "--no-edit", "-m", f"Ship {base} -> {prot} (production)", f"origin/{base}")
        if mg.returncode != 0:
            _git(wtp, "merge", "--abort")
            out["error"] = f"merging {base} into {prot} hit conflicts — resolve in a terminal or a PR"
            return out
        ps = _git(wtp, "push", "origin", f"HEAD:{prot}")
        out["pushed"] = out["ok"] = ps.returncode == 0
        if out["ok"]:
            _git(repo, "branch", "-f", prot, f"origin/{prot}")   # advance local MAIN so the cockpit shows 0 ahead
        else:
            err = ((ps.stderr or ps.stdout) or "").strip()
            out["error"] = (f"{prot} is a protected branch — ship via a Pull Request instead"
                            if ("protected" in err.lower() or "denied" in err.lower() or "hook" in err.lower())
                            else f"push to origin/{prot} failed: " + err[:150])
    except subprocess.TimeoutExpired:
        out["error"] = f"merge/push to {prot} timed out — check your network / credentials"
    except (subprocess.SubprocessError, OSError) as e:
        out["error"] = str(e)[:200]
    finally:
        _git(repo, "worktree", "remove", "--force", wt)
        shutil.rmtree(wt, ignore_errors=True)
    return out
