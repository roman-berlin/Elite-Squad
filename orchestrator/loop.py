"""The orchestration loop: build -> verify -> review -> land-on-dev / retry / escalate.

This is the "PM/general". It owns git, the Definition of Done, the iteration
bounds, the cost budget, every backlog transition, and the keep-dev-green merge.
`main` is never touched here — you merge that after QA in dev.
"""
from __future__ import annotations

import fcntl
import os
import re
import subprocess
import time
from collections import deque
from contextlib import ExitStack, contextmanager
from pathlib import Path

from . import backends
from . import builder as builder_mod
from . import decisions
from . import notify
from . import reviewer as reviewer_mod
from .audit import AuditLog
from .backlog.base import BacklogAdapter, NoneBacklog, make_backlog
from .config import AppConfig, Config
from .contracts import (BuildRequest, Outcome, PerTicketArtifactStore,
                       SpecArtifact, Ticket, TicketReport)
from .gate import base_gate_check, extract_failure_evidence, gate_fingerprint, run_deterministic_checks, run_gate
from . import jira_adapter as jira_commenter
from . import cockpit_state
from . import run_logger
from .git_ops import Git, GitError
from .officers import display
from .phases import BUILD, GATE, LAND, PHASES, REVIEW

# Get the module reference for explicit subprocess access
import sys as _sys
_loop_module = _sys.modules[__name__]


def _notify(cfg: Config, text: str) -> None:
    """Telegram notify with a [DRY-RUN] prefix when not live. Safe no-op if unset."""
    notify.send(("[DRY-RUN] " if cfg.dry_run else "") + text)


def _test_url(app: AppConfig, build_summary: str | None) -> str:
    """Where the Commander should test this change on DEV: the builder's 'TEST: <route>' line,
    resolved against the app's qa_url. Returns a full URL, a bare route, or '' if neither exists."""
    base = (getattr(app, "qa_url", None) or "").rstrip("/")
    route = ""
    m = re.search(r"^\s*TEST:\s*(.+?)\s*$", build_summary or "", re.IGNORECASE | re.MULTILINE)
    if m:
        route = m.group(1).strip()
    if route.lower().startswith("(no ui"):          # builder declared no UI — show the hint as-is
        return route
    if route.lower().startswith("http"):
        return route
    if base and route.startswith("/"):
        return base + route
    if base:
        return base + (("/" + route.lstrip("/")) if route else "")
    return route


def _changelog_path() -> Path:
    """Documentation/Development_Status.md in the orchestrator's OWN repo (not the app's)."""
    return Path(__file__).resolve().parent.parent / "Documentation" / "Development_Status.md"


def _record_changelog(cfg: Config, ticket: Ticket, app: AppConfig, summary: str | None,
                      test_url: str, *, today: str | None = None,
                      path: str | Path | None = None) -> bool:
    """Technical Writer release-hygiene: after a SUCCESSFUL LIVE land, append a one-line entry to
    Documentation/Development_Status.md — date · ticket · app · what-was-done · DEV test URL — so the
    unit keeps a human-readable feature changelog. Deterministic, no LLM (the data already exists at the
    land site). Skipped for dry-run and ephemeral tickets. Best-effort: a write failure is swallowed and
    never raises, so it can't break the run. Appends, never loses history; newest entry first. Returns
    True iff an entry was written.

    The read of existing entries and the write of the rebuilt file happen INSIDE one held lock via
    ``locking.locked_text_rmw`` — two lands racing this call (two drains landing at once) must not
    both read the same stale ``old`` list and clobber each other's append (EU-276)."""
    if cfg.dry_run or ticket.ephemeral:
        return False
    try:
        import datetime

        from . import dashboard as _D
        from . import locking
        date = today or datetime.date.today().isoformat()
        what = _D.brief(summary, n=220) or (ticket.summary or "").strip()
        link = f" · 🔗 {test_url}" if test_url else ""
        entry = f"- {date} · {ticket.id} · {app.name} · {what}{link}"
        header = ("# Development Status\n\n"
                  "Feature changelog — one line per successful live land to the dev branch, newest "
                  "first. Maintained automatically by the Technical Writer (orchestrator/loop.py).\n")
        target = Path(path) if path else _changelog_path()

        def _mutate(current: str) -> str:
            # `current` is read INSIDE the lock, never a pre-lock snapshot.
            old = [ln for ln in current.splitlines() if ln.startswith("- ")]
            return header + "\n" + "\n".join([entry, *old]) + "\n"

        locking.locked_text_rmw(target, _mutate, default="")
        return True
    except Exception as exc:  # noqa: BLE001 - release-hygiene logging must never break the run
        print(f"  changelog skipped: {exc}", flush=True)
        return False


_HALT_MARKERS = ("halt", "stop", "do not proceed", "precondition", "made no writes",
                 "no files were written", "no files written", "will not edit", "will not proceed",
                 "cannot proceed", "refuse", "abort", "needs your", "for the commander", "holding for")


def _is_deliberate_halt(text: str | None) -> bool:
    """A 'no changes' result is a DELIBERATE halt — the Builder verified a precondition and
    reported a blocker, not a failure — when its summary carries clear halt language. Requiring
    two markers avoids false positives on ordinary 'nothing to do' summaries."""
    t = (text or "").lower()
    return sum(1 for m in _HALT_MARKERS if m in t) >= 2


_TURN_LIMIT_MARKERS = ("maximum number of turns", "max turns", "max_turns")


def _is_turn_limit(text: str | None) -> bool:
    t = (text or "").lower()
    return any(m in t for m in _TURN_LIMIT_MARKERS)


# EU-197: Helper to wrap officer execution with transcript context
def _officer_transcript_context(app: AppConfig, ticket: Ticket, officer: str, cfg: Config):
    """Context manager for per-officer transcript writing.

    Sets up the transcript context before an officer runs and tears it down
    after completion. Captures full tool inputs, reasoning, and results when
    transcript_enabled is True.

    Usage:
        with _officer_transcript_context(app, ticket, "builder", cfg):
            result = await builder_mod.build(...)
    """
    from . import transcript
    import datetime as _dt

    # Generate timestamp in HHMMSS format
    timestamp = _dt.datetime.now().strftime("%H%M%S")

    class _TranscriptContext:
        def __enter__(self):
            try:
                transcript.set_transcript_context(app.name, ticket.id, timestamp, officer, cfg)
            except Exception:  # noqa: BLE001 — best-effort
                pass
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            try:
                transcript.clear_transcript_context()
                transcript.close_transcript(app.name, ticket.id, timestamp, officer)
            except Exception:  # noqa: BLE001 — best-effort
                pass
            return False  # Don't suppress exceptions

    return _TranscriptContext()


async def _try_scrum_split(cfg: Config, app: AppConfig, ticket: Ticket, audit: AuditLog,
                           recap: str, reason: str, split_reason: str,
                           iterations: int = 0, cost: float = 0.0,
                           branch: str | None = None) -> TicketReport | None:
    """Hand an OVERSIZED ticket to the Scrum Master to split into right-sized, independently-shippable
    sub-tickets. Returns a REQUEUED TicketReport (parent closed, fragments filed) when the split succeeds,
    else None so the caller escalates to the Commander. Shared by the two "too big" ceilings — the turn
    limit (the build ran out of turns) AND the per-ticket token/time budget — so a ticket decomposes
    regardless of WHICH ceiling it hit, instead of parking on the Commander only for one of them.
    Ephemeral tickets have no real backlog to file fragments into, so they never split (caller escalates).
    ``iterations``/``cost``/``branch`` carry the attempt's real state into the report (the budget site has
    them; the turn-limit exception site does not — its defaults are 0/0.0/None). scrum.split's own depth
    guard makes an irreducibly-too-big ticket return ok=False here, so the caller escalates, never loops.
    A DRY RUN must not mutate the real board (scrum.split files sub-tickets, comments + closes the parent),
    so in dry_run we skip the split and let the caller escalate — no Jira writes on a preview."""
    if ticket.ephemeral or getattr(cfg, "dry_run", False):
        return None
    try:
        from . import scrum as _scrum
        sp = await _scrum.split(cfg, app.name, ticket, recap=recap, reason=reason)
    except Exception as sexc:  # noqa: BLE001 - a split failure must fall through to escalate
        sp = {"ok": False, "keys": [], "error": str(sexc)}
    if sp.get("ok") and sp.get("keys"):
        kk = ", ".join(sp["keys"])
        audit.record("scrum_split", ticket_id=ticket.id, reason=split_reason, into=sp["keys"])
        _notify(cfg, f"🧩 {ticket.id} was too big for one pass — the Scrum Master split it into "
                     f"{kk} and closed the parent. The unit takes the fragments next.")
        print(f"  🧩 {ticket.id}: too big → Scrum Master split into {kk}; parent closed.", flush=True)
        return TicketReport(ticket.id, Outcome.REQUEUED, iterations, cost, app.name, branch,
                            notes=f"too big — Scrum Master split into {kk}")
    return None


async def _exception_report(cfg: Config, ticket: Ticket, app: AppConfig, exc: Exception,
                            audit: AuditLog, backlog=None) -> TicketReport:
    """Turn a ticket-level exception into a report. A turn-limit blow-out is NOT a real failure — the
    ticket was simply too big to finish in one pass. "Too big" is the Scrum Master's job, so first hand
    it to him to split into right-sized sub-tickets, and only escalate to the Commander if a split isn't
    possible (delegation off, or no backlog to file sub-tickets into, or the splitter declines)."""
    msg = str(exc)
    if _is_turn_limit(msg):
        # Auto-split first — don't ask the Commander to do by hand what the Scrum Master is for.
        split = await _try_scrum_split(
            cfg, app, ticket, audit,
            recap=f"{ticket.id} ran out of turns before finishing — too big for a single pass.",
            reason="Builder hit the turn limit — split into smaller, independently-shippable tickets.",
            split_reason="turn-limit")
        if split is not None:
            return split
        # No split possible → escalate to the Commander as before.
        note = (f"{ticket.id} ran out of turns before finishing — this ticket is likely too big for "
                "a single pass. Split it into smaller tickets, or raise the turn budget "
                "(builder_max_turns). Nothing was merged.")
        try:
            decisions.add(cfg, ticket, app.name, note)
        except Exception:  # noqa: BLE001 - never let the escalation path itself crash the run
            pass
        audit.record("needs_human", ticket_id=ticket.id, reason="turn-limit", question=note)
        _notify(cfg, f"🛑 {ticket.id} — ran out of turns (too big for one pass) and couldn't be split. "
                     "Split it, or raise builder_max_turns.\n\n" + decisions.reply_hint(ticket.id))
        print(f"  🛑 {ticket.id}: ran out of turns — too big and unsplittable; escalated to you.", flush=True)
        return TicketReport(ticket.id, Outcome.ESCALATED, 0, 0.0, app.name,
                            notes="ran out of turns — ticket too big for one pass")
    audit.record(Outcome.ERRORED.audit_event, ticket_id=ticket.id, app=app.name, error=msg)
    # Leave a Jira-visible trace of the failure (2026-07-09: errored tickets sat In Progress with
    # ZERO comment — invisible on the board). Comment only, no transition: the autopilot retries
    # ERRORED tickets from In Progress, and parks to Blocked itself after repeated failures.
    if backlog is not None and not ticket.ephemeral and not getattr(cfg, "dry_run", False):
        try:
            backlog.add_comment(ticket, f"❌ Build errored: {msg[:600]}\n"
                                        "(the unit will retry; repeated failures park it as Blocked)")
        except Exception:  # noqa: BLE001 — the trace is best-effort, never breaks error handling
            pass
    _notify(cfg, f"❌ {ticket.id} — error: {msg[:200]}")
    return TicketReport(ticket.id, Outcome.ERRORED, 0, 0.0, app.name, notes=msg)


def _bar(done: int, active: int = -1, fail: int = -1) -> None:
    """A phase progress checklist:  ✓ Build  ✓ Gate  ✓ Tests  ⏳ Review  ○ Land.

    Phases come from the shared ``PHASES`` constant (EU-55) so this terminal bar and the
    War Room web bar derive from one source and can never drift apart again. ``done`` is the
    count of completed phases; ``active``/``fail`` are PHASES indices — pass them by name
    (BUILD/GATE/REVIEW/LAND) so the call sites can't drift if the order changes.
    """
    cells = []
    for i, name in enumerate(PHASES):
        glyph = "✗" if i == fail else "✓" if i < done else "⏳" if i == active else "○"
        cells.append(f"{glyph} {name}")
    print("    " + "   ".join(cells), flush=True)


def _worktree_path(app: AppConfig, cfg: Config) -> str:
    """Where the CTO keeps this app's private worktree (a sibling of the repo)."""
    if getattr(cfg, "worktree_dir", None):
        return str(Path(cfg.worktree_dir).expanduser() / app.name)
    repo = Path(app.repo_path).expanduser().resolve()
    return str(repo.parent / ".general-worktrees" / app.name)


def _worktree_setup_command(app: AppConfig, cfg: Config, workdir: str) -> str | None:
    """Resolve the command to run ONCE when a worktree is first created, or None to skip. (EU-54)

    A per-app `worktree_setup_cmd` overrides the unit-wide `Config.worktree_setup_cmd`. A node-manifest
    install (bun/npm/pnpm/yarn) is skipped when the worktree has no package.json — so the Bun product's
    `bun install --frozen-lockfile` no longer errors on every fresh Elite-Unit (Python) worktree, which
    has no manifest and gates with python3. Mirrors the no-manifest guard in _repin_worktree_deps."""
    cmd = app.worktree_setup_cmd if getattr(app, "worktree_setup_cmd", None) is not None \
        else getattr(cfg, "worktree_setup_cmd", None)
    if not cmd:
        return None
    needs_manifest = any(tok in cmd for tok in
                         ("bun install", "npm install", "npm ci", "pnpm install", "pnpm i", "yarn"))
    if needs_manifest and not (Path(workdir) / "package.json").is_file():
        print(f"  · worktree setup skipped (no package.json for '{cmd}')", flush=True)
        return None
    return cmd


def _make_git(cfg: Config, app: AppConfig) -> Git:
    """Build the git custodian for an app. With use_worktree on, the CTO gets a
    dedicated linked worktree (based on origin/<base>) so it never fights the user's
    manual checkout. Falls back to in-tree if isolation can't engage (e.g. no origin)."""
    if getattr(cfg, "use_worktree", False):
        wt = _worktree_path(app, cfg)
        try:
            git = Git(app.repo_path, app.base_branch, app.protected_branch, worktree_path=wt)
            created = git.setup()
            app.workdir = git.workdir
            if created:
                setup_cmd = _worktree_setup_command(app, cfg, git.workdir)
                if setup_cmd:
                    print(f"  · worktree created — setup: {setup_cmd}", flush=True)
                    _loop_module.subprocess.run(setup_cmd, shell=True, cwd=git.workdir, check=False)
            print(f"  · isolated worktree → {git.workdir}", flush=True)
            return git
        except GitError as exc:
            first = str(exc).splitlines()[0] if str(exc) else "unknown"
            print(f"  · worktree isolation off ({first}); working in-tree", flush=True)
    git = Git(app.repo_path, app.base_branch, app.protected_branch)
    app.workdir = git.workdir
    return git


def _repin_worktree_deps(cfg: Config, app: AppConfig, git: Git) -> None:
    """Per-ticket dependency isolation (EU-18).

    The one-time ``worktree_setup_cmd`` runs only when a worktree is first *created*.
    But a worktree is REUSED across tickets, and its ``node_modules`` is gitignored —
    so a previous ticket that mutated/added a dependency leaves the install drifted off
    DEV's pin, and ``reset --hard``/``clean -fd`` won't restore it (clean skips ignored
    paths; the lockfile alone says nothing about what's physically installed).

    So before EVERY build we re-pin the (possibly reused) worktree to DEV:
      1. hard-restore ``bun.lock`` from the integration base (origin/<base>), which
         re-pins an already-clobbered lockfile to DEV's exact bytes;
      2. reinstall with ``--frozen-lockfile`` so ``node_modules`` matches that lock
         exactly and the command FAILS LOUDLY instead of silently rewriting the lock.

    No-op outside worktree mode (in-tree runs share the user's own checkout)."""
    if not getattr(cfg, "use_worktree", False) or not getattr(git, "isolated", False):
        return
    workdir = Path(git.workdir)
    # Only meaningful for a Bun project; skip silently if there's no lockfile/manifest.
    if not (workdir / "bun.lock").exists() and not (workdir / "package.json").exists():
        return
    base_ref = getattr(git, "base_ref", f"origin/{app.base_branch}")
    # 1) restore DEV's lockfile into the worktree (undoes any prior-ticket drift).
    restored = _loop_module.subprocess.run(
        ["git", "checkout", base_ref, "--", "bun.lock"],
        cwd=str(workdir), capture_output=True, text=True,
    )
    if restored.returncode != 0:
        why = (restored.stderr.strip().splitlines() or ["no bun.lock at base"])[0]
        print(f"  · dep isolation: bun.lock not restored from {base_ref} ({why})", flush=True)
    # 2) reinstall frozen so the install can't drift off the pinned lock.
    print("  · dep isolation — bun install --frozen-lockfile", flush=True)
    proc = _loop_module.subprocess.run(
        ["bun", "install", "--frozen-lockfile"],
        cwd=str(workdir), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        tail = (proc.stderr.strip().splitlines() or ["unknown"])[-1]
        print(f"  · dep isolation: frozen install reported a problem ({tail})", flush=True)


class Budget:
    def __init__(self, limit: float):
        self.limit = limit
        self.spent = 0.0

    def add(self, amount: float) -> None:
        self.spent += amount or 0.0

    def exceeded(self) -> bool:
        return self.limit > 0 and self.spent >= self.limit   # limit <= 0 disables the cap


class WorktreeBusy(Exception):
    """Another live run already holds this app's worktree."""


def is_worktree_locked(worktree_path: str) -> bool:
    """Check if a worktree is currently locked by another process without blocking.

    Returns True if the worktree lock is held (meaning a build is in progress),
    False if the lock is available. This is used by the pre-build gate to skip
    tickets that are already being worked on.
    """
    lock = Path(str(worktree_path) + ".lock")
    if not lock.exists():
        return False

    try:
        f = open(lock, "r")
        try:
            # Try to acquire a non-blocking exclusive lock
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            # If we got here, the lock was available - release it immediately
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            f.close()
            return False
        except OSError:
            # Lock is held by another process
            f.close()
            return True
    except (OSError, IOError):
        # Can't open lock file - assume not locked
        return False


@contextmanager
def _worktree_lock(worktree_path: str):
    """Advisory exclusive lock on an app's shared worktree, so two runs can never ``reset --hard`` /
    ``clean -fd`` it out from under each other — the data-loss where 'a parallel process reset the
    tree' and a build's work vanished. Uses ``flock``, which the OS releases automatically when the
    process exits (even on a crash), so the lock can never go stale and wedge future runs. Excludes
    both other processes AND other threads in this process (each `open()` is a distinct lock holder).
    Raises ``WorktreeBusy`` if a live run already holds it."""
    lock = Path(str(worktree_path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        f.close()
        raise WorktreeBusy(str(worktree_path)) from e
    try:
        f.write(f"{os.getpid()}\n")
        f.flush()
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()


async def run(cfg: Config, worklist: list[tuple[AppConfig, Ticket]],
              audit: AuditLog, stop_event=None) -> list[TicketReport]:
    # EU-189: pin THIS run's model backend (Opus vs GLM) for every officer SDK call. set_backend
    # writes a run-scoped contextvar that agent._run_agent_unrouted reads at the single SDK seam,
    # so all officers inherit the choice with no per-call plumbing. Each run executes in its own
    # asyncio.run() context (its own thread), so concurrent runs with different backends never
    # bleed; reset in finally keeps a reused context tidy. This MUST wrap the whole run and set the
    # var before awaiting the body — the body is one pure await-chain with no task/thread boundary,
    # so the value reaches every officer call (a future create_task/run_in_executor added *before*
    # this set would not inherit it — keep the set first).
    # EU-236: pass the cfg-anchored ModelRegistry so a registry-id pin (a custom backend) is both
    # preserved by current() and resolved hermetically by apply() at the SDK seam — never against the
    # live state/ store from within a test's tmp config.
    from .model_registry import ModelRegistry
    _bk_token = backends.set_backend(getattr(cfg, "model_backend", backends.NATIVE),
                                     registry=ModelRegistry(cfg))
    try:
        return await _run_inner(cfg, worklist, audit, stop_event)
    finally:
        backends.reset_backend(_bk_token)


def _fetch_fragments_to_worklist(cfg: Config, app: AppConfig,
                                 fragment_keys: list[str]) -> list[tuple[AppConfig, Ticket]]:
    """Convert fragment ticket keys into worklist items.

    After a Scrum Master split, the fragment keys are created in the backlog.
    This helper fetches each fragment ticket and returns a list of (app, ticket)
    pairs that can be injected into the worklist.

    EU-201: fragments are built in dependency order (the order returned by the split).
    """
    from .backlog.base import make_backlog
    backlog = make_backlog(app)
    items: list[tuple[AppConfig, Ticket]] = []
    for key in fragment_keys:
        try:
            fragment_ticket = backlog.get_task(key)
            items.append((app, fragment_ticket))
        except Exception as exc:  # noqa: BLE001 - one fragment fetch failing must not break the run
            print(f"  · fragment {key} fetch failed: {exc}", flush=True)
    return items


async def _run_inner(cfg: Config, worklist: list[tuple[AppConfig, Ticket]],
                     audit: AuditLog, stop_event=None) -> list[TicketReport]:
    budget = Budget(cfg.max_cost_usd)
    reports: list[TicketReport] = []
    gits: dict[str, Git] = {}
    backlogs: dict[str, BacklogAdapter] = {}
    ensured: set[str] = set()

    # Hold each app's worktree lock for the WHOLE run, so a parallel run can't reset/clean the tree
    # mid-build and wipe in-progress work. A busy app's tickets are DEFERRED (picked up next pass),
    # never clobbered. flock auto-releases on process exit, so a crashed run never wedges the lock.
    locks = ExitStack()
    busy: set[str] = set()
    if getattr(cfg, "use_worktree", False):
        for app in {a.name: a for a, _ in worklist}.values():
            try:
                locks.enter_context(_worktree_lock(_worktree_path(app, cfg)))
            except WorktreeBusy:
                busy.add(app.name)
                _notify(cfg, f"⏸️ {app.name}: a run is already working this app — deferring new tickets "
                             "so the active run's work isn't reset. They'll be picked up next pass.")
                print(f"  ⏸️ {app.name}: worktree busy — deferring (avoids clobbering the active run).",
                      flush=True)

    try:
        # EU-201: Use an index so we can inject fragments after the parent ticket
        i = 0
        while i < len(worklist):
            app, ticket = worklist[i]
            i += 1
            if stop_event is not None and stop_event.is_set():
                audit.record("run_stopped", reason="commander stop (before ticket)")
                print("  ■ stopped by Commander — remaining tickets skipped.", flush=True)
                break
            if budget.exceeded():
                audit.record("budget_stop", spent=budget.spent)
                break
            if app.name in busy:
                audit.record("worktree_busy_deferred", ticket_id=ticket.id, app=app.name)
                reports.append(TicketReport(ticket.id, Outcome.SKIPPED, 0, 0.0, app.name,
                                            notes="deferred — worktree busy (another run active)"))
                continue
            backlog = None   # pre-bind: an exception before assignment must not NameError the handler
            # EU-253: bracket THIS ticket with a per-ticket log file before any work starts, and
            # close it once the ticket is done (success OR caught exception — so even a
            # ticket_exception leaves a per-ticket transcript on disk). Every drain funnels through
            # this one bracket — cockpit /api/run, /api/run-tickets, AND automode's autopilot loop —
            # so automode lands (which never went through server.py's manual open/close) finally get
            # logs/<app>/<YYYY-MM-DD>/<TICKET>-<HHMMSS>.log.
            #
            # The handle MUST be registered under the SAME key cockpit_state._Tee.write looks up, or
            # the captured stdout lands under a key with no open handle and the file stays empty (the
            # iteration-1 bug: it keyed on app.name while a unit-wide drain's Tee keys on None). _Tee
            # attributes each line to active_runs()[0] when exactly one run is active, else None —
            # so we compute the SAME effective key here. The on-disk PATH still comes from the
            # ticket's app.name (run_logger derives the path from app_name, the registry key from
            # run_key). Open/close are best-effort: a log I/O problem must never abort a build.
            _active = cockpit_state.active_runs()
            _log_key = _active[0] if len(_active) == 1 else None
            _log_opened = False
            try:
                if len(_active) > 1:
                    # Concurrent drains: _Tee collapses attribution to the shared None key, so a
                    # dedicated per-ticket handle can't own only THIS drain's lines (and two drains
                    # both registering under None would clobber each other). Drop a dated note file
                    # instead of a silently-empty one, then leave the shared stream alone.
                    run_logger.write_note_log(
                        cfg, app.name, ticket.id,
                        f"[EU-253] {len(_active)} runs active {_active!r} at start of {ticket.id} "
                        f"({app.name}) — the live stdout stream attributes lines to the shared drain "
                        f"(None) key while multiple runs are in flight, so a dedicated per-ticket log "
                        f"can't be isolated for this ticket. Consult the shared drain log; per-ticket "
                        f"separation is only available when a single run is active.")
                else:
                    run_logger.open_run_log(cfg, app.name, ticket.id, run_key=_log_key)
                    _log_opened = True
            except Exception:  # noqa: BLE001 — log setup must never block a run
                _log_opened = False
            try:
                try:
                    if app.name not in gits:
                        gits[app.name] = _make_git(cfg, app)
                    git = gits[app.name]
                    # Ephemeral (free-text) tickets have no tracker -> no creds needed.
                    if ticket.ephemeral:
                        backlog = NoneBacklog()
                    else:
                        if app.name not in backlogs:
                            backlogs[app.name] = make_backlog(app)
                        backlog = backlogs[app.name]
                    if app.name not in ensured:
                        git.ensure_clean()
                        ensured.add(app.name)
                    report = await process_ticket(ticket, app, cfg, git, backlog, audit, budget, stop_event)
                except Exception as exc:  # noqa: BLE001 - one bad ticket must not kill the run
                    report = await _exception_report(cfg, ticket, app, exc, audit, backlog=backlog)
            finally:
                if _log_opened:
                    try:
                        run_logger.close_run_log(run_key=_log_key)
                    except Exception:  # noqa: BLE001 — log teardown must never block a run
                        pass
            reports.append(report)

            # EU-201: After a split, inject the fragments into the worklist at the current position
            # The fragments are built serially in dependency order before the next queue ticket
            if report.outcome == Outcome.REQUEUED and report.notes and "split into" in report.notes.lower():
                # Extract fragment keys from the notes (format: "too big — Scrum Master split into AUTO-101, AUTO-102")
                import re
                fragment_match = re.search(r'split into ([^.\n]+)', report.notes)
                if fragment_match:
                    fragment_keys_str = fragment_match.group(1).strip()
                    # Split by comma and clean up the keys
                    fragment_keys = [k.strip() for k in fragment_keys_str.split(',')]
                    if fragment_keys:
                        # Fetch the fragment tickets and convert to worklist items
                        fragment_items = _fetch_fragments_to_worklist(cfg, app, fragment_keys)
                        if fragment_items:
                            # Inject fragments at the current position (after the parent)
                            worklist[i:i] = fragment_items
                            audit.record("fragment_injection", ticket_id=ticket.id,
                                        fragment_count=len(fragment_items), fragment_keys=fragment_keys)
                            print(f"  🧩 {ticket.id}: injected {len(fragment_items)} fragment(s) into worklist: "
                                  f"{', '.join(fragment_keys)} — building them next in order.", flush=True)
                            _notify(cfg, f"🧩 {ticket.id}: {len(fragment_items)} fragment(s) will be built next: "
                                       f"{', '.join(fragment_keys)}")

            # Failure forensics: if this ticket has now failed enough times, auto-write its post-mortem.
            try:
                from . import forensics
                forensics.maybe_postmortem(cfg, report, audit)
            except Exception:  # noqa: BLE001 - diagnostics must never break the run
                pass
    finally:
        locks.close()
    return reports


async def process_ticket(ticket, app, cfg, git, backlog, audit, budget, stop_event=None) -> TicketReport:
    branch = ticket.branch_name(app.branch_prefix)
    audit.record("ticket_start", ticket_id=ticket.id, app=app.name, branch=branch,
                 dry_run=cfg.dry_run, ephemeral=ticket.ephemeral)

    # EU-153: Initialize ticket commenter for gate events
    commenter = jira_commenter.TicketCommenter(
        cfg,
        dry_run=cfg.dry_run,
        no_comment=getattr(cfg, "no_comments", False)  # Opt-out flag
    )

    # Readiness gate: an under-specified ticket (no acceptance criteria + a thin description) is handed
    # back BEFORE any build effort — the Builder would only guess and halt. Opt-in (`readiness_gate`).
    if getattr(cfg, "readiness_gate", False):
        from . import readiness
        ready, missing = readiness.assess(ticket, min_desc_chars=getattr(cfg, "readiness_min_desc", 80))
        if not ready:
            note = "Not ready to build:\n" + "\n".join(f"• {m}" for m in missing)
            if not cfg.dry_run and not ticket.ephemeral:
                try:
                    backlog.set_status(ticket, "Needs Human")
                    backlog.add_comment(ticket, "🚧 " + note + "\n\nAdd these and move it back to To Do.")
                except Exception:  # noqa: BLE001 - a comment failure must not break the run
                    pass
            # Not a decision round-trip — the readiness gate owns its own 'Needs Human' status above —
            # so record the pending decision WITHOUT re-parking the ticket to 'Blocked' (EU-61).
            decisions.add(cfg, ticket, app.name, note, block=False)
            _notify(cfg, f"🚧 {ticket.id} — handed back, not ready to build:\n\n{await notify.report_brief(cfg, note)}"
                         f"\n\n{decisions.reply_hint(ticket.id)}")
            audit.record("not_ready", ticket_id=ticket.id, missing=missing)
            print(f"  🚧 {ticket.id}: not ready — handed back (no build spent).", flush=True)
            return TicketReport(ticket.id, Outcome.ESCALATED, 0, 0.0, app.name, branch,
                                notes="not ready — handed back before build")

    # EU-61: a parked decision the Commander answered DIRECTLY on its Jira ticket resumes here — fold the
    # question + answer into the builder's context and clear the park. (A Telegram answer is already baked
    # into the description by decisions.to_worklist, so this only fires for the Jira-native path.)
    ticket = _resume_from_jira_answer(cfg, ticket, backlog, audit)

    if not cfg.dry_run and not ticket.ephemeral:
        # For a resuming ticket this transitions it out of 'Blocked' and back to 'In Progress' (EU-61).
        backlog.set_status(ticket, "In Progress")
    git.checkout_feature(branch)
    # EU-18: re-pin the (possibly reused) worktree's deps to DEV's bun.lock before any
    # build runs, so a prior ticket's dependency drift can't leak into this one.
    _repin_worktree_deps(cfg, app, git)
    print(f"\n» {ticket.id}  →  branch {branch}", flush=True)
    _notify(cfg, f"🔨 {ticket.id} started — {ticket.summary}\nbranch: {branch}")

    report: TicketReport | None = None
    try:
        report = await _attempt(ticket, app, cfg, git, backlog, audit, budget, branch, stop_event, commenter=commenter)
        return report
    finally:
        _cleanup(cfg, git, audit, report)


def _resume_from_jira_answer(cfg, ticket, backlog, audit):
    """EU-61 Jira-native decision resume. If this ticket has an OPEN parked decision and the Commander
    answered it directly on the ticket (a fresh human comment, newer than the baseline snapshotted at
    park time), fold the question + answer into the builder's context and clear the park, so the build
    continues with the decision baked in. Returns the (possibly augmented) ticket. Best-effort — never
    raises; a no-op for ephemeral tickets or when nothing is parked/answered."""
    if getattr(ticket, "ephemeral", False):
        return ticket
    try:
        pending = next((d for d in decisions.load(cfg) if d.get("id") == ticket.id), None)
        if not pending:
            return ticket
        answer = backlog.latest_answer(ticket)
        if not answer or answer == pending.get("answer_baseline"):
            return ticket   # no answer yet, or only the pre-park comment — leave it parked
        from dataclasses import replace
        question = pending.get("question", "")
        desc = (ticket.description or "") + (
            "\n\n---\nResolved decision (the Commander answered on the ticket — act on it, do not "
            f"re-raise it):\nQ: {question}\nA: {answer}")
        # Pop the park locally; the answer is already a comment on the ticket, so don't echo it back.
        decisions.resolve(cfg, answer, ticket.id, comment=False)
        audit.record("decision_resumed", ticket_id=ticket.id, via="jira-comment")
        print(f"  ▶ {ticket.id}: resuming with the Commander's answer from Jira", flush=True)
        return replace(ticket, description=desc)
    except Exception as exc:  # noqa: BLE001 - resume injection must never break the run
        print(f"  · decision resume skipped for {ticket.id}: {exc}", flush=True)
        return ticket


async def _consult_pm(cfg, ticket, app, audit, halt_report: str):
    """Ask the Product Manager about a Builder halt. Returns the PM verdict dict, or None when the PM
    is disabled or errors (the caller then escalates to the Commander, i.e. the old behaviour)."""
    if not getattr(cfg, "pm_enabled", True):
        return None
    try:
        from . import pm
        ctx = ((ticket.description or "")[:3000] + "\n\nBUILDER HALT REPORT:\n" + halt_report)[:6000]
        r = await pm.review(cfg, app.name, ticket.id, question=halt_report[:2000], context=ctx)
        audit.record("pm_review", ticket_id=ticket.id, verdict=r["verdict"])
        return r
    except Exception as e:  # noqa: BLE001 - a PM failure must never break the run
        audit.record("pm_error", ticket_id=ticket.id, error=str(e)[:200])
        return None


async def _pm_decide_before_park(cfg, ticket, app, audit, backlog, question: str, iteration: int):
    """EU-230: consult the automode PM before parking a needs_human-shaped decision (reviewer
    review.needs_human, or the EU-90 pm-findings 'decisions' bucket) — decide-first, mirroring the
    builder-halt path's inject+comment+continue machinery (loop.py ~1097-1122). Callers guard this
    with the shared per-ticket ``pm_used`` flag so the PM is consulted at most once per ticket.

    On DECIDE: returns (new_ticket, None) — the caller replaces its local ``ticket`` with new_ticket
    and ``continue``s the build loop; the decision is already injected into the ticket description,
    the durable '🤖 Automode' Jira comment (automode) already posted, and a `pm_decided` audit event
    already recorded. Roman is NOT paged.

    On explicit ESCALATE, or when the PM is unavailable/errors (_consult_pm returns None): returns
    (None, pm_outcome) so the caller falls through to its existing park-on-Commander code. When
    pm_outcome carries a 'why' (the EU-92 WHY-CANNOT-RESOLVE line), the caller should prepend it to
    the parked proposal."""
    pm_outcome = await _consult_pm(cfg, ticket, app, audit, question)
    if pm_outcome is not None and pm_outcome["verdict"] == "DECIDE":
        from dataclasses import replace
        auto = bool(getattr(cfg, "auto_mode", False))
        new_ticket = replace(ticket, description=(ticket.description or "")
            + "\n\n---\nPRODUCT MANAGER DECISION (resolves the open product question — "
              "act on it, do not re-raise it):\n" + pm_outcome["body"])
        audit.record("pm_decided", ticket_id=ticket.id, iteration=iteration, automode=auto)
        # Automode: the PM decided WITHOUT waiting for you. Leave a durable trail on the
        # ticket so you can review it (and reverse — it's on DEV, never production).
        if auto and not cfg.dry_run and not ticket.ephemeral:
            try:
                backlog.add_comment(ticket,
                    "🤖 Automode — PM decided autonomously (DEV only — review & reverse if needed).\n\n"
                    + pm_outcome["body"][:1200])
            except Exception:  # noqa: BLE001 - a comment failure must not break the run
                pass
        head = ("🤖 Automode — the PM decided autonomously" if auto
                else "🧭 the PM made the product call")
        _notify(cfg, f"{head}; {ticket.id} continuing:\n\n{pm_outcome['body'][:800]}")
        print(f"  {'🤖' if auto else '🧭'} {ticket.id}: PM decided — re-building with "
              "the decision.", flush=True)
        return new_ticket, None
    return None, pm_outcome


def _already_pm_triaged(cfg, ticket_id: str) -> bool:
    """True if this ticket already got its ONE PM triage — so a genuinely-stuck ticket escalates for
    real next time instead of looping triage -> re-queue forever."""
    try:
        import json
        from . import dashboard as _D
        for line in _D.audit_lines(cfg.audit_path):
            try:
                e = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if e.get("event") == "pm_triage" and e.get("ticket_id") == ticket_id:
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _recent_no_changes_ticket_ids(cfg: Config) -> set[str]:
    """Return the set of ticket IDs that recently had a no_changes outcome (EU-116).

    The drain uses this to skip tickets that already produced no changes — they're
    in 'Needs Human' awaiting verification/close, and re-running them would waste
    another full build cycle producing the same result."""
    try:
        import json
        from . import dashboard as _D
        no_changes_ids = set()
        for line in _D.audit_lines(cfg.audit_path):
            try:
                e = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if e.get("event") == "no_changes":
                ticket_id = e.get("ticket_id")
                if ticket_id:
                    no_changes_ids.add(ticket_id)
        return no_changes_ids
    except Exception:  # noqa: BLE001
        pass
    return set()


def _changes_sig(changes: list[str]) -> str:
    """A stable fingerprint of a review's required changes, so two passes that get the SAME blocking
    feedback can be detected as 'stuck' (the build isn't addressing it) and escalated instead of burning
    more identical passes. Order-independent; tolerant of trivial whitespace/case drift."""
    norm = sorted({" ".join((c or "").lower().split())[:160] for c in (changes or []) if (c or "").strip()})
    return "|".join(norm)


# QW3 (2026-07-05 audit): hard ceiling on builder↔reviewer rounds per attempt. Not configurable —
# config/CLI may lower it but can never raise it. Evidence: EU-174 burned 4 max-effort passes
# (15,535,048 builder tokens) against an unwinnable red base; corpus-wide, 100% of round-≥2 reviewer
# objections were NEW (moving goalposts), so passes beyond 2 buy objections, not convergence.
HARD_MAX_PASSES = 2

# EU-216 (2026-07-09 forensics, cause class 3/14): HARD_MAX_PASSES is calibrated for Opus. Weak
# (non-Opus, e.g. GLM) backends get ONE extra pass — but only when the pass-2 review FAIL carries no
# blocker-severity finding and the retry-stuck guard hasn't already tripped (see the weak-extra-pass
# guard below, just before the retry rebuild). Not configurable past this ceiling either.
HARD_MAX_PASSES_WEAK = 3


async def _attempt(ticket, app, cfg, git, backlog, audit, budget, branch, stop_event=None, commenter=None) -> TicketReport:
    if commenter is None:
        commenter = jira_commenter.TicketCommenter(
            cfg,
            dry_run=cfg.dry_run,
            no_comment=getattr(cfg, "no_comments", False)
        )
    cost = 0.0
    last_changes: list[str] = []
    # Retry guard (EU-56): remember the last few reject signatures, not just the immediately
    # previous one, so an A/B/A/B rejection oscillation — where the build alternates between two
    # unaddressed failures — trips escalation instead of burning every remaining Opus pass.
    recent_reject_sigs: deque[str] = deque(maxlen=3)
    pm_used = False
    # EU-90: fingerprint of the in-scope finding SET we last commented on this ticket, kept across
    # passes so the "Builder retrying" Jira comment is posted only when that set actually CHANGES —
    # not re-posted identically on every failing retry.
    last_in_scope_sig: str | None = None

    # EU-72: one shared per-ticket artifact pool, threaded through the officers so each reads a tight
    # structured handoff instead of re-deriving from the full diff. The SpecArtifact is derived straight
    # from the ticket (there is no separate Spec officer yet); the builder publishes its BuildArtifact
    # and the reviewer its ReviewVerdict into the same store as the iterations run.
    store = PerTicketArtifactStore()
    store.put(SpecArtifact(acceptance=list(ticket.acceptance_criteria or []),
                           scope=ticket.summary or "", non_goals=[]))

    def _burn(key: str, result_in: int, result_out: int) -> None:
        """Accumulate input+output tokens into store.token_burn[key] (EU-96).

        Called after each officer call to aggregate per-officer token spend into the
        shared store so the final audit event and TicketReport carry the full picture.
        Idempotent — safe to call multiple times for the same key (e.g. review retry).
        """
        store.token_burn[key] = store.token_burn.get(key, 0) + result_in + result_out

    def _resolve(report: TicketReport) -> TicketReport:
        """Stamp token_burn onto the report and emit the structured audit event (EU-96).

        Called at every return site in _attempt() so the burn report is always emitted
        regardless of how the ticket resolves (merge, escalate, error, skip, requeue).
        Best-effort: the audit write uses the existing AuditLog which is already
        exception-safe; the dataclasses.replace is a pure value copy.
        """
        from dataclasses import replace as _dc_replace
        tb = dict(store.token_burn)  # snapshot — prevents mutation after return
        if tb:
            # EU-96: structured per-officer burn event picked up by EU-135 dashboards.
            audit.record("token_burn_report", ticket_id=ticket.id,
                         payload=tb, total=sum(tb.values()))
        return _dc_replace(report, token_burn=tb)

    # Phase-2 §3.1: red-base short-circuit (the EU-174 killer) — gate the base tree BEFORE the
    # first build. A red base means the failure predates this ticket: block immediately
    # (Telegram + Needs-you) instead of billing max-effort builds for a failure no builder can
    # fix (EU-174 alone burned 15.5M tokens against a red base). Three guards (2026-07-06 review):
    #   • the tree must PROVABLY be at base (HEAD sha == base-ref sha) — a resumed non-isolated
    #     feature branch carries the ticket's own WIP and would be misdiagnosed as a red base,
    #     permanently deadlocking every requeue;
    #   • per-app-gated monorepos are skipped — their in-pass gate runs only the touched
    #     component's suite, so 'base health' is ticket-dependent and the repo-wide fallback
    #     would block green-app tickets on a red sibling (a per-app red base is instead caught
    #     by the fingerprint guard below);
    #   • inside base_gate_check, a red must survive a confirmation re-run and red cache entries
    #     expire — one timing flake must never halt the queue.
    _at_base = False
    if getattr(cfg, "red_base_check", True) and app.gate_commands \
            and not getattr(app, "gate_commands_by_app", None):
        try:
            _head = (getattr(git, "current_sha", lambda: "")() or "").strip()
            _base = (getattr(git, "base_sha", lambda: "")() or "").strip()
            _at_base = bool(_head) and _head == _base
        except Exception:  # noqa: BLE001 — sha probing must never break a run
            _at_base = False
    if _at_base:
        base_ok, base_fp, base_report = base_gate_check(app, cfg, git, runner=run_gate)
        if not base_ok:
            audit.record("red_base_block", ticket_id=ticket.id, fingerprint=base_fp,
                         report=(base_report or "")[:2500])
            decisions.add(cfg, ticket, app.name,
                          f"Base branch '{app.base_branch}' is RED before any build — the gate "
                          "fails on the clean base tree. Fix the base (or land the fix ticket) "
                          "before re-queuing this one.\n\n" + (base_report or "")[:1500])
            audit.record(Outcome.ESCALATED.audit_event, ticket_id=ticket.id, iterations=0,
                         reason="red base — gate fails on the clean base tree",
                         question=f"Base branch '{app.base_branch}' is red before any build; "
                                  "fix the base before re-queuing this ticket.")
            print(f"  ⛔ {ticket.id}: base branch is RED before any build — blocking (no build).",
                  flush=True)
            _notify(cfg, f"⛔ {ticket.id} blocked — base branch '{app.base_branch}' is RED before "
                         f"any build (gate fails on the clean base). {decisions.reply_hint(ticket.id)}")
            return _resolve(TicketReport(ticket.id, Outcome.ESCALATED, 0, cost, app.name, branch,
                                         notes="red base — gate fails on the clean base tree"))

    # §3.1 second half: identical gate-failure fingerprint on consecutive failed gates → stop
    # rebuilding and BREAK to the normal exhaustion path (PM triage first, then park). Breaking
    # rather than returning keeps the PM's look at the failure — run_all-style gates only expose
    # harness granularity, so an "identical" fingerprint can occasionally be two different checks
    # in one harness; the PM triage is the safety valve for that case (2026-07-06 review). The
    # review-path twin is recent_reject_sigs / EU-56.
    last_gate_fp: str = ""

    def _note_gate_stuck(fp: str, iteration: int) -> None:
        audit.record("gate_fingerprint_stuck", ticket_id=ticket.id, iteration=iteration,
                     fingerprint=fp)
        print("  gate · identical failure fingerprint two gates running — stopping rebuilds "
              "(PM triage next)", flush=True)

    # EU-216: a weak (non-Opus) backend is allowed to reach a 3rd pass — gated per-iteration below
    # on FAIL severity, not unconditionally. Opus's range never changes (min(.., HARD_MAX_PASSES)).
    weak_backend = backends.normalize(getattr(cfg, "model_backend", None)) != backends.NATIVE
    max_passes = (min(cfg.max_iterations_weak, HARD_MAX_PASSES_WEAK) if weak_backend
                  else min(cfg.max_iterations, HARD_MAX_PASSES))
    attempt_t0 = time.monotonic()
    for iteration in range(1, max_passes + 1):
        if stop_event is not None and stop_event.is_set():
            audit.record("run_stopped", ticket_id=ticket.id, iteration=iteration, phase="pre-build")
            print(f"  ■ {ticket.id}: stopped by Commander — no merge.", flush=True)
            return _resolve(TicketReport(ticket.id, Outcome.SKIPPED, iteration, cost, app.name, branch,
                                         notes="stopped by Commander"))
        if budget.exceeded():
            audit.record("budget_exceeded", ticket_id=ticket.id, spent=budget.spent)
            return _resolve(TicketReport(ticket.id, Outcome.ESCALATED, iteration, cost, app.name, branch,
                                         notes="cost budget exceeded"))
        # QW4 (2026-07-05): per-ticket token/time budget, checked before each pass (a call in
        # flight is never interrupted — the budget bounds the NEXT pass). Breach → BLOCKED via the
        # same decisions.add plumbing as exhaustion (Jira 'Blocked' + Needs-you) + Telegram, never
        # a silent continuation. Evidence: EU-174 burned 15.5M tokens/79 min with no budget check.
        _burned = sum(store.token_burn.values())
        _elapsed_min = (time.monotonic() - attempt_t0) / 60.0
        _tok_over = cfg.per_ticket_token_budget > 0 and _burned >= cfg.per_ticket_token_budget
        _time_over = cfg.per_ticket_time_budget_min > 0 and _elapsed_min >= cfg.per_ticket_time_budget_min
        if _tok_over or _time_over:
            why = (f"token budget: {_burned:,} ≥ {cfg.per_ticket_token_budget:,}" if _tok_over
                   else f"time budget: {_elapsed_min:.0f}m ≥ {cfg.per_ticket_time_budget_min}m")
            audit.record("ticket_budget_exceeded", ticket_id=ticket.id, iteration=iteration,
                         tokens_burned=_burned, token_budget=cfg.per_ticket_token_budget,
                         elapsed_min=round(_elapsed_min, 1),
                         time_budget_min=cfg.per_ticket_time_budget_min, reason=why)
            # "Too big for the budget" IS "too big" — hand it to the Scrum Master to split into right-sized
            # fragments, exactly like the turn-limit path (loop._exception_report), instead of parking on the
            # Commander. AUTO-85 burned 11.9M on one 8-page pass and parked here; splitting decomposes it. Only
            # escalate (below) when a split isn't possible (ephemeral ticket, or the splitter declines).
            _split = await _try_scrum_split(
                cfg, app, ticket, audit,
                recap=f"{ticket.id} exceeded its per-ticket budget ({why}) before finishing — too big for one attempt.",
                reason="Ticket blew the per-ticket token/time budget — split into smaller, independently-shippable tickets.",
                split_reason="budget", iterations=iteration, cost=cost, branch=branch)
            if _split is not None:
                return _resolve(_split)
            decisions.add(cfg, ticket, app.name,
                          f"Per-ticket budget exceeded ({why}) after {iteration - 1} pass(es). "
                          "Raise the budget, narrow the ticket, or answer the open review items.")
            # Review fix (2026-07-05): also record the CANONICAL terminal event — the cockpit,
            # forensics and _run_in_flight all key on Outcome audit events (needs_human), not on
            # ticket_budget_exceeded; without this the parked ticket showed "running…" forever.
            audit.record(Outcome.ESCALATED.audit_event, ticket_id=ticket.id, iterations=iteration,
                         reason=f"per-ticket budget exceeded — {why}",
                         question=f"Per-ticket budget exceeded ({why}). Raise the budget, narrow "
                                  "the ticket, or answer the open review items.")
            print(f"  ⛔ {ticket.id}: per-ticket budget exceeded ({why}) — parking.", flush=True)
            _notify(cfg, f"⛔ {ticket.id} parked — per-ticket budget exceeded ({why}). "
                         f"{decisions.reply_hint(ticket.id)}")
            return _resolve(TicketReport(ticket.id, Outcome.ESCALATED, iteration, cost, app.name, branch,
                                         notes=f"per-ticket budget exceeded — {why}"))

        # 0) PLANNER (Phase-2 §2) — one Opus design call before the build; absorbs the Architect.
        # Produces the design brief (→ adr channel), sharper TESTABLE acceptance criteria (→ the
        # SpecArtifact the Builder reads, so it writes tests against them), and the in-scope file
        # list. Iteration 1 only; retries reuse the first pass's brief. Fail-safe: plan() never
        # raises, and a non-BUILD verdict is CONSERVATIVE for now (recorded, still built — acting
        # on ANSWER/CLOSE/REFILE is a guarded follow-up; a bad auto-close is the senior_pm mistake
        # §2 is undoing). SPLIT routes to the Scrum Master, same as architect-oversized.
        adr: str | None = None
        _planned = False
        if iteration == 1 and getattr(cfg, "planner_enabled", False):
            from . import planner as _planner
            print(f"  planner · designing {ticket.id}…", flush=True)
            _pres = await _planner.plan(cfg, ticket, app=app, audit=audit)
            cost += _pres.cost_usd
            budget.add(_pres.cost_usd)
            _burn("planner", _pres.input_tokens, _pres.output_tokens)
            _planned = True
            if _pres.verdict == "SPLIT":
                from . import scrum as _scrum
                recap = _pres.answer or _pres.approach
                try:
                    sp = await _scrum.split(cfg, app.name, ticket, recap=recap,
                                            reason="Planner: too large for one build")
                    if sp.get("ok") and sp.get("keys"):
                        kk = ", ".join(sp["keys"])
                        audit.record("scrum_split", ticket_id=ticket.id, reason="planner-split", into=sp["keys"])
                        _notify(cfg, f"🧩 {ticket.id} was too big — the Planner split it into {kk} "
                                     "(on you) and closed the parent.")
                        print(f"  🧩 {ticket.id}: Planner → Scrum Master split into {kk}; parent closed.", flush=True)
                        return _resolve(TicketReport(ticket.id, Outcome.REQUEUED, iteration, cost,
                                                     app.name, branch, notes=f"Planner split into {kk}"))
                    print(f"  · Scrum Master couldn't split ({sp.get('error')}) — building instead.", flush=True)
                except Exception as exc:  # noqa: BLE001 — a split failure falls through to a normal build
                    print(f"  · Planner-triggered split crashed: {exc} — building instead.", flush=True)
            # The design brief (approach + testable AC + in-scope files) flows to the Builder via
            # the adr channel; the Planner's testable AC are rendered in it as "write a test for
            # EACH", so they drive test-writing (the deterministic gate then runs those tests)
            # WITHOUT replacing the ticket's acceptance_criteria. Overwriting store.spec.acceptance
            # here desynced the Builder (built against the sharpened AC) from the Reviewer (still
            # judges ticket.acceptance_criteria) — two officers, different contracts (2026-07-06
            # review). Keep ONE contract: the ticket AC; the testable AC are the test-writing lens.
            brief = _pres.as_builder_brief()
            if brief:
                adr = brief
            if _pres.verdict not in ("BUILD", "SPLIT"):
                audit.record("planner_nonbuild_verdict", ticket_id=ticket.id,
                             verdict=_pres.verdict, answer=(_pres.answer or "")[:600])
                print(f"  planner · verdict {_pres.verdict} — building anyway (conservative; verdict "
                      "routing is a guarded follow-up)", flush=True)
            else:
                print(f"  planner · BUILD · {len(_pres.testable_ac)} testable AC · "
                      f"{len(_pres.in_scope_files)} in-scope files", flush=True)

        # 0b) ARCHITECT — only when the Planner didn't run (the Planner absorbs it, §2).
        # (Only on first iteration; retry passes reuse the ADR from the first pass.)
        if iteration == 1 and not _planned and getattr(cfg, "architect_enabled", False):
            from . import architect as _arch
            if await _arch.should_run_architect(cfg, ticket):
                print(f"  architect · producing ADR for {ticket.id}…", flush=True)
                try:
                    adrex = await _arch.design(cfg, ticket, repo_context="", gated=True, audit=audit)
                    audit.record("architect_complete", ticket_id=ticket.id,
                                skipped=adrex.skipped, touch_points=len(adrex.touch_points))
                    if not adrex.skipped and adrex.raw:
                        adr = adrex.raw
                        print(f"  architect · ADR produced ({len(adrex.touch_points)} touch-points)", flush=True)
                        # Check if oversized — trigger Scrum Master split
                        if _arch.detect_oversized(adrex):
                            print(f"  architect · oversized design detected — triggering Scrum Master", flush=True)
                            from . import scrum as _scrum
                            recap = f"Architect ADR identified {len(adrex.touch_points)} touch-points"
                            reason = "too many touch-points/modules for one ticket"
                            try:
                                sp = await _scrum.split(cfg, app.name, ticket, recap=recap, reason=reason)
                                if sp.get("ok") and sp.get("keys"):
                                    kk = ", ".join(sp["keys"])
                                    audit.record("scrum_split", ticket_id=ticket.id, reason="architect-oversized",
                                                into=sp["keys"])
                                    _notify(cfg, f"🧩 {ticket.id} was too big — Architect triggered Scrum Master "
                                               f"split into {kk} (on you) and closed the parent.")
                                    print(f"  🧩 {ticket.id}: Architect detected oversized → Scrum Master split into "
                                          f"{kk}; parent closed.", flush=True)
                                    return _resolve(TicketReport(ticket.id, Outcome.REQUEUED,
                                                              iteration, cost, app.name, branch,
                                                              notes=f"too big — Architect triggered Scrum Master "
                                                                    f"split into {kk}"))
                                else:
                                    print(f"  · Scrum Master couldn't split ({sp.get('error')}) — continuing with build.",
                                          flush=True)
                            except Exception as exc:
                                print(f"  · Scrum Master split crashed: {exc} — continuing with build", flush=True)
                    else:
                        print(f"  architect · skipped (bug/small change)", flush=True)
                except Exception as exc:
                    print(f"  · Architect skipped: {exc}", flush=True)
                    audit.record("architect_error", ticket_id=ticket.id, error=str(exc)[:200])

        # 1) BUILD  — effort is sized from the ticket (XS→low … XL→max), then escalates on retry
        eff, eff_reason = builder_mod.effort_plan(cfg, iteration, ticket)
        print(f"  build · pass {iteration}/{cfg.max_iterations} (effort {eff} — {eff_reason}) "
              f"— builder working (can take a few minutes)…", flush=True)
        _bar(BUILD, active=BUILD)
        req = BuildRequest(ticket=ticket, branch=branch, prior_issues=last_changes, iteration=iteration, adr=adr)
        # EU-72: hand the builder the typed SpecArtifact (primary context) + the shared pool it
        # publishes its BuildArtifact into.
        # EU-197: Wrap builder with transcript context to capture full tool inputs + reasoning
        with _officer_transcript_context(app, ticket, "builder", cfg):
            build = await builder_mod.build(req, app, cfg, audit=audit, store=store, spec=store.spec)
        cost += build.cost_usd
        budget.add(build.cost_usd)
        _burn("builder", build.input_tokens, build.output_tokens)   # EU-96: accumulate builder burn
        audit.record("build", ticket_id=ticket.id, iteration=iteration, ok=build.ok,
                     cost_usd=build.cost_usd, turns=build.num_turns,
                     effort=eff, effort_reason=eff_reason,
                     tools=build.tools, summary=(build.summary or "")[:1000],
                     provider=build.provider, model=build.model_version)
        if not build.ok:
            # EU-248: max-turns exhaustion (turnCount >= the pass's turn budget) surfaces as a
            # builder process error indistinguishable from a real crash — agent.py now degrades the
            # CLI's trailing raw ProcessError to a clean is_error return instead of raising, so this
            # is the ONLY seam that sees it. Route it the same way the (now largely unreachable)
            # exception-based turn-limit handler in _exception_report always has: the ticket was too
            # big for one pass, so the Scrum Master splits it BEFORE this is misclassified as
            # Outcome.ERRORED — which would wrongly tick the EU-219 consecutive-error counter toward
            # Blocked and park a ticket that just needed to be split. Do NOT grep build.summary/raw
            # for turn-limit text — a failed build's summary holds only the last assistant message,
            # never the turn-limit phrase; num_turns is the only reliable signal.
            if build.num_turns >= builder_mod.turns_for(cfg, eff):
                split = await _try_scrum_split(
                    cfg, app, ticket, audit,
                    recap=f"{ticket.id} ran out of turns before finishing — too big for a single pass.",
                    reason="Builder hit the turn limit — split into smaller, independently-shippable tickets.",
                    split_reason="turn-limit", iterations=iteration, cost=cost, branch=branch)
                if split is not None:
                    return _resolve(split)
            # EU-153: Post build error comment
            build_comment = commenter.summarize_gate_event(
                "Build", "ERRORED",
                (build.summary or build.raw or "Builder process error")[:2000],
                ticket.id
            )
            if build_comment and backlog and not ticket.ephemeral:
                commenter.post_comment(backlog, ticket.key, build_comment)
            return _resolve(TicketReport(ticket.id, Outcome.ERRORED, iteration, cost, app.name, branch,
                                         notes="builder process errored"))
        if not git.has_changes():
            report = (build.summary or build.raw or "(no report)").strip()
            deliberate = _is_deliberate_halt(build.summary or build.raw)
            # Route ANY no-changes build to the Product Manager once per ticket — not only ones whose
            # wording trips the 2-marker halt heuristic. A genuine "no-changes" blocker phrased with <2
            # markers would otherwise be mislabeled ERRORED and parked instead of getting a product call.
            # The PM either makes the decision — and the build resumes with it injected — or it hands back
            # a recommendation we surface to the Commander. (Trade-off: one extra cheap PM call on a truly
            # empty build, which then still falls through to ERRORED below.)
            pm_outcome = None
            if not pm_used:
                pm_used = True
                pm_outcome = await _consult_pm(cfg, ticket, app, audit, report)
                if pm_outcome is not None and pm_outcome["verdict"] == "DECIDE":
                    from dataclasses import replace
                    auto = bool(getattr(cfg, "auto_mode", False))
                    ticket = replace(ticket, description=(ticket.description or "")
                        + "\n\n---\nPRODUCT MANAGER DECISION (resolves the open product question — "
                          "act on it, do not re-raise it):\n" + pm_outcome["body"])
                    audit.record("pm_decided", ticket_id=ticket.id, iteration=iteration, automode=auto)
                    # Automode: the PM decided WITHOUT waiting for you. Leave a durable trail on the
                    # ticket so you can review it (and reverse — it's on DEV, never production).
                    if auto and not cfg.dry_run and not ticket.ephemeral:
                        try:
                            backlog.add_comment(ticket,
                                "🤖 Automode — PM decided autonomously (DEV only — review & reverse if needed).\n\n"
                                + pm_outcome["body"][:1200])
                        except Exception:  # noqa: BLE001 - a comment failure must not break the run
                            pass
                    head = ("🤖 Automode — the PM decided autonomously" if auto
                            else "🧭 the PM made the product call")
                    _notify(cfg, f"{head}; {ticket.id} continuing:\n\n{pm_outcome['body'][:800]}")
                    print(f"  {'🤖' if auto else '🧭'} {ticket.id}: PM decided — re-building with "
                          "the decision.", flush=True)
                    continue
            # The PM didn't (or couldn't) make the call. Park for the Commander when this was a deliberate
            # halt OR the PM returned a recommendation; otherwise it's a truly empty build -> ERRORED.
            if deliberate or pm_outcome is not None:
                proposal = pm_outcome["body"] if pm_outcome is not None else report
                # EU-92: prepend the WHY PM CANNOT RESOLVE line (captured by parse_verdict) so the
                # Commander immediately sees the specific authority or context that's missing —
                # rather than having to read into the body to find why the PM couldn't decide.
                if pm_outcome is not None and pm_outcome.get("why"):
                    proposal = f"WHY PM CANNOT RESOLVE: {pm_outcome['why']}\n\n{proposal}"
                decisions.add(cfg, ticket, app.name, proposal[:1500])   # so you can answer it in Telegram
                if not cfg.dry_run and not ticket.ephemeral:
                    try:
                        backlog.add_comment(ticket, "⛔ Parked — needs a Commander product decision to "
                                            "continue. What's needed:\n\n" + proposal[:1200])
                    except Exception:  # noqa: BLE001 - a comment failure must not break the run
                        pass
                audit.record("needs_human", ticket_id=ticket.id, iteration=iteration,
                             question=proposal[:1500], reason="product blocker — escalated to Commander")
                _notify(cfg, f"🛑 {ticket.id} — needs your product call"
                             + (" (the PM recommends):" if pm_outcome is not None else ":")
                             + f"\n\n{await _decision_brief(cfg, ticket.id, proposal)}"
                             + f"\n\n{decisions.reply_hint(ticket.id)}")
                print(f"  🛑 {ticket.id}: parked — escalated to you; the unit moves to the next ticket.",
                      flush=True)
                return _resolve(TicketReport(ticket.id, Outcome.ESCALATED, iteration, cost, app.name, branch,
                                             notes="parked — Commander product decision needed"))
            # EU-116: a no-changes build leaves the ticket stuck In Progress and the drain re-runs it.
            # Move the ticket off In Progress to Needs Human so the Commander can verify/close it.
            # The drain guard in intake.from_drain will skip tickets with recent no_changes outcomes.
            # EU-153: Post no-changes comment
            note = "Builder produced no changes — the acceptance criteria are already satisfied or this work was already completed by another ticket."
            no_change_comment = commenter.summarize_gate_event(
                "Build", "NO_CHANGES",
                note,
                ticket.id
            )
            if no_change_comment and backlog and not ticket.ephemeral:
                commenter.post_comment(backlog, ticket.key, no_change_comment)
            if not cfg.dry_run and not ticket.ephemeral:
                try:
                    backlog.set_status(ticket, "Needs Human")
                    backlog.add_comment(ticket, f"⛔ {note}\n\nVerify and close if already satisfied, or re-open with clarification if there's still work to do.")
                except Exception:  # noqa: BLE001 - a comment failure must not break the run
                    pass
            print(f"  🔵 {ticket.id}: no changes — moved to Needs Human for verification.", flush=True)
            audit.record("no_changes", ticket_id=ticket.id, iteration=iteration)
            return _resolve(TicketReport(ticket.id, Outcome.ESCALATED, iteration, cost, app.name, branch,
                                         notes="no changes — already satisfied"))
        print(f"    builder done — {build.num_turns} steps, files changed ✓", flush=True)
        # EU-72: the builder published its BuildArtifact but can't know the changed-file list (the loop
        # owns git) — stamp the authoritative paths onto it so the Test Engineer + Reviewer read an
        # accurate handoff.
        if store.build is not None and not store.build.files_changed:
            store.build.files_changed = git.changed_paths()
        _bar(GATE, active=GATE)
        if cfg.notify_verbose:
            _notify(cfg, f"🔧 {ticket.id} — {display('field_engineer')} implemented (pass {iteration})")

        # 2) VERIFICATION GATE on the feature branch (cheap filter, before review)
        # Gate only the monorepo apps/packages this ticket actually touched (EU-19) —
        # falls back to the repo-wide gate when no per-app config matches the diff.
        # (EU-85: domain-gap classification lives solely in the squad delegation path —
        # squad._plan → detect_domain_gap — where it actually routes provisioning; the gate
        # no longer re-classifies here just to attach an advisory note.)
        gate = run_gate(app, git.changed_paths())
        if not gate.passed:
            # Flake honesty (2026-07-09): a red gate must REPRODUCE before it burns a builder pass.
            # 5 of the last 14 "max passes — PM escalated" strandings were full-suite flakes
            # (EU-129/139/201/204/206 — 4 later merged with ZERO extra fix passes); worse, the same
            # flake twice tripped the fingerprint-stuck breaker. Mirrors the base-gate confirmation
            # re-run in gate.py. Only a reproduced red proceeds to fingerprint/stuck handling.
            confirm = run_gate(app, git.changed_paths())
            if confirm.passed:
                audit.record("gate_flake_confirmed_green", ticket_id=ticket.id,
                             iteration=iteration, first_report=(gate.report or "")[:1500])
                print("  gate · red once, green on confirmation re-run — flake, proceeding", flush=True)
                gate = confirm
        audit.record("gate", ticket_id=ticket.id, iteration=iteration, passed=gate.passed,
                     report=("" if gate.passed else extract_failure_evidence(gate.report or "")))
        if not gate.passed:
            # §3.1: same failure fingerprint as the previous failed gate → the rebuild didn't
            # move it; stop building and let PM triage/exhaustion handle it (EU-174's shape).
            fp = gate_fingerprint(gate.report or "")
            if fp and fp == last_gate_fp:
                _note_gate_stuck(fp, iteration)
                last_changes = [f"Verification failed identically two gates running; fix these:\n{gate.report}"]
                break
            last_gate_fp = fp
            print("  gate · FAILED → sending fixes back to builder", flush=True)
            # EU-153: Post gate failure comment
            gate_comment = commenter.summarize_gate_event(
                "Gate", "FAILED",
                (gate.report or "Verification gate failed")[:2000],
                ticket.id
            )
            if gate_comment and backlog and not ticket.ephemeral:
                commenter.post_comment(backlog, ticket.key, gate_comment)
            _bar(GATE, fail=GATE)
            last_changes = [f"Verification failed; fix these:\n{gate.report}"]
            continue
        last_gate_fp = ""   # a passing gate breaks the "consecutive" chain (flakes ≠ stuck)
        if app.gate_commands:
            print("  gate · passed", flush=True)

        # 2.7) DETERMINISTIC CHECKS (§3.3–5): lint → secret scan → lockfile sanity. Scripts, not
        # LLMs — cheap, unhallucinatable, and no reviewer runs until they are green; a failure
        # feeds the Builder as plain text (a free review round).
        det = run_deterministic_checks(app, git.changed_paths(), git.diff_against_base())
        audit.record("deterministic_gate", ticket_id=ticket.id, iteration=iteration,
                     passed=det.passed, report=("" if det.passed else extract_failure_evidence(det.report or "")))
        if not det.passed:
            # §3.1: the stuck-fingerprint guard covers this stage too — an unchanging lint/
            # secret/lockfile failure must not burn the remaining pass budget (2026-07-06 review).
            fp = gate_fingerprint(det.report or "")
            if fp and fp == last_gate_fp:
                _note_gate_stuck(fp, iteration)
                last_changes = [f"Deterministic checks failed identically two gates running; fix these:\n{det.report}"]
                break
            last_gate_fp = fp
            print("  gate · deterministic checks FAILED → sending fixes back to builder", flush=True)
            _bar(GATE, fail=GATE)
            last_changes = [f"Deterministic checks failed; fix these:\n{det.report}"]
            continue
        last_gate_fp = ""   # all gates green this pass — reset the consecutive-failure chain

        # 3) REVIEW (spec + quality) on the diff
        _bar(REVIEW, active=REVIEW)
        print("  review · reviewer reading the diff…", flush=True)
        diff = git.diff_against_base()
        # EU-72: hand the Reviewer the Builder's BuildArtifact (primary context) + the pool it
        # publishes its ReviewVerdict into. EU-52: escalate the reviewer on re-review.
        # EU-197: Wrap reviewer with transcript context to capture full tool inputs + reasoning
        with _officer_transcript_context(app, ticket, "reviewer", cfg):
            review = await reviewer_mod.review(diff, ticket, app, cfg, iteration,
                                               store=store, build_artifact=store.build)
        cost += review.cost_usd
        budget.add(review.cost_usd)
        _burn("reviewer", review.input_tokens, review.output_tokens)   # EU-96
        # Unparseable reviewer output fails closed. Before paying for a full rebuild+review pass,
        # retry JUST the review once — re-running the read-only review is far cheaper than rebuilding
        # (EU-11). EU-52: the retry escalates one tier (iteration+1) so even when the ladder judged
        # pass 1 on Sonnet, the re-review lands on a stronger model where a parse miss is near-zero.
        if review.parse_failed:
            print("  review · unparseable verdict → re-reviewing once (no rebuild)", flush=True)
            audit.record("review_parse_retry", ticket_id=ticket.id, iteration=iteration)
            # EU-197: Wrap reviewer retry with transcript context
            with _officer_transcript_context(app, ticket, "reviewer", cfg):
                review = await reviewer_mod.review(diff, ticket, app, cfg, iteration + 1,
                                                   store=store, build_artifact=store.build)
            cost += review.cost_usd
            budget.add(review.cost_usd)
            _burn("reviewer", review.input_tokens, review.output_tokens)   # EU-96 retry
        audit.record("review", ticket_id=ticket.id, iteration=iteration,
                     verdict=review.verdict.value, spec_met=review.spec_met,
                     blocking=len(review.blocking_issues), cost_usd=review.cost_usd,
                     diff_hash=AuditLog.diff_hash(diff),
                     summary=(review.summary or "")[:1000],
                     required_changes=review.required_changes,
                     issues=[{"severity": q.severity, "area": q.area, "detail": q.detail}
                             for q in review.quality_issues],
                     provider=review.provider, model=review.model_version)
        print(f"  review · verdict {review.verdict.value}"
              + (f" — {len(review.blocking_issues)} blocking issue(s)" if review.blocking_issues else ""),
              flush=True)
        blockers = [q for q in review.quality_issues if q.severity == "blocker"]
        if blockers:
            _notify(cfg, f"🚨 {ticket.id} — Code Reviewer found a critical issue ({blockers[0].area}): "
                         f"{blockers[0].detail}")
        if cfg.notify_verbose:
            _notify(cfg, f"🔎 {ticket.id} — Code Reviewer verdict: {review.verdict.value}")

        # EU-42: real-but-off-spec findings the Reviewer flagged (===TICKETS=== block on review.raw)
        # go to the backlog — auto-filed or proposed for you — instead of being lost.
        _route_out_of_scope(cfg, ticket, app, audit, review.raw, source="reviewer")

        # A product/scope decision only the Commander can make -> EU-230: consult the automode PM
        # FIRST (decide-first, mirroring the builder-halt path) instead of parking straight on
        # Roman. Guarded by the shared per-ticket pm_used flag so a needs_human retry doesn't
        # re-invoke the PM. Park to the Commander ONLY on an explicit PM ESCALATE (or the PM being
        # unavailable/errored).
        if review.needs_human:
            question = review.question or review.summary
            pm_outcome = None
            if not pm_used:
                pm_used = True
                new_ticket, pm_outcome = await _pm_decide_before_park(
                    cfg, ticket, app, audit, backlog, question, iteration)
                if new_ticket is not None:
                    ticket = new_ticket
                    continue
            proposal = question
            # EU-92: prepend the WHY PM CANNOT RESOLVE line so the Commander immediately sees the
            # specific authority/context that's missing, mirroring the builder-halt path.
            if pm_outcome is not None and pm_outcome.get("why"):
                proposal = f"WHY PM CANNOT RESOLVE: {pm_outcome['why']}\n\n{proposal}"
            decisions.add(cfg, ticket, app.name, proposal)
            _notify(cfg, f"❓ {ticket.id} — needs YOUR decision:\n{await _decision_brief(cfg, ticket.id, proposal)}"
                         f"\n\n{decisions.reply_hint(ticket.id)}")
            if not cfg.dry_run and not ticket.ephemeral:
                # decisions.add already parked it to 'Blocked' (EU-61) — just record the open question.
                backlog.add_comment(ticket, f"Needs a product decision: {proposal}")
            audit.record("needs_human", ticket_id=ticket.id, question=proposal)
            return _resolve(TicketReport(ticket.id, Outcome.ESCALATED, iteration, cost, app.name, branch,
                                         notes=f"needs decision: {proposal[:140]}"))

        # EU-90: PM findings triage — when the Reviewer returns FAIL with quality_issues,
        # classify each finding into: in-scope fixes (Builder retries these unchanged),
        # out-of-scope pre-existing issues (auto-filed as linked backlog tickets), and
        # Commander decisions (escalated via the normal decisions queue). Placed here —
        # after needs_human has already been handled — so only genuine FAIL+quality paths
        # reach the triage. Wrapped in try/except so a PM error always falls through to
        # the normal retry; the triage is a routing aid, never a gate.
        if review.verdict.value == "FAIL" and review.quality_issues:
            try:
                import json as _json
                from . import pm as _pm_mod
                # EU-90: hand the classifier REAL diff evidence — the diff already computed for the
                # review (above) plus the authoritative changed-file list the loop stamped onto the
                # BuildArtifact. Without this the in-scope/out-of-scope split has no path basis.
                ftr = await _pm_mod.triage_findings(
                    review, ticket, cfg,
                    diff=diff,
                    files_changed=(store.build.files_changed if store.build is not None else None),
                )
                audit.record("pm_findings_triage", ticket_id=ticket.id, iteration=iteration,
                             in_scope=len(ftr.in_scope), out_of_scope=len(ftr.out_of_scope),
                             decisions=len(ftr.decisions))
                # (a) in_scope — retry logic is unchanged; post a short Jira comment so the
                # Commander can see exactly which defects the Builder is about to address. Post it
                # ONLY when the in-scope finding SET has changed since the previous pass: an identical
                # set on the next retry would just re-spam the same comment every failing iteration
                # (EU-90 rejection #2). The signature is order-independent and case/whitespace tolerant.
                if ftr.in_scope:
                    in_scope_sig = _changes_sig(
                        [f"{q.severity}/{q.area}: {q.detail}" for q in ftr.in_scope]
                    )
                    if (in_scope_sig != last_in_scope_sig
                            and not cfg.dry_run and not ticket.ephemeral):
                        try:
                            lines = "\n".join(
                                f"• [{q.severity}/{q.area}] {q.detail[:120]}"
                                for q in ftr.in_scope
                            )
                            backlog.add_comment(
                                ticket,
                                f"🔧 Builder retrying — {len(ftr.in_scope)} in-scope "
                                f"finding(s) to fix:\n{lines}")
                        except Exception:  # noqa: BLE001 - comment failure must not break the run
                            pass
                    # Remember this set so the next identical retry stays silent (tracked even in
                    # dry-run/ephemeral, where the comment is skipped, so the signature still advances).
                    last_in_scope_sig = in_scope_sig
                # (b) out_of_scope — synthesise a ===TICKETS=== block and route pre-existing
                # issues to the backlog so they are filed as linked tickets, not silently lost.
                # EU-90 double-filing guard: the Reviewer may have ALSO flagged the same issue in
                # its OWN ===TICKETS=== block (already routed above as source="reviewer"). With
                # out_of_scope_autofile defaulting on, both routes file to the same backlog — so drop
                # any pm-findings proposal whose wording a reviewer proposal already covers.
                fresh_oos = _dedupe_oos_against_reviewer(ftr.out_of_scope, review.raw)
                deduped_n = len(ftr.out_of_scope) - len(fresh_oos)
                if deduped_n:
                    audit.record("pm_findings_oos_deduped", ticket_id=ticket.id,
                                 iteration=iteration, skipped=deduped_n)
                if fresh_oos:
                    oos_proposals = [
                        {
                            "title": f"[{q['severity'].upper()}/{q['area']}] {q['detail'][:160]}",
                            "type": "Bug",
                            "severity": q["severity"].upper(),
                            "body": q["detail"],
                        }
                        for q in fresh_oos
                    ]
                    oos_block = f"===TICKETS===\n{_json.dumps(oos_proposals)}\n===END==="
                    _route_out_of_scope(cfg, ticket, app, audit, oos_block, source="pm-findings")
                # (c) decisions — scope/product ambiguities only the Commander can settle;
                # add them with a distinct entry_id so they don't overwrite the main decision.
                # EU-90: this is the ONLY findings route that pages the Commander, and it pages
                # BRIEFLY — a bulleted Needs-you line (EU-79), never the old wall-of-text dump. The
                # in-scope (Builder retries) and out-of-scope (auto-filed) routes stay silent.
                if ftr.decisions:
                    bullets = "\n".join(
                        f"• [{q.severity}/{q.area}] {q.detail}" for q in ftr.decisions
                    )
                    question = ("Reviewer raised scope/product ambiguities that need your call:\n"
                                + bullets)
                    # EU-230: consult the automode PM before paging the Commander — decide-first,
                    # mirroring the builder-halt + reviewer needs_human paths. Guarded by the shared
                    # per-ticket pm_used flag. On DECIDE the loop injects the decision and re-builds;
                    # decisions.add/notify below are skipped entirely — Roman is not paged.
                    pm_outcome = None
                    if not pm_used:
                        pm_used = True
                        new_ticket, pm_outcome = await _pm_decide_before_park(
                            cfg, ticket, app, audit, backlog, question, iteration)
                        if new_ticket is not None:
                            ticket = new_ticket
                            continue
                    if pm_outcome is not None and pm_outcome.get("why"):
                        question = f"WHY PM CANNOT RESOLVE: {pm_outcome['why']}\n\n{question}"
                    # Page the Commander ONLY when this parks a genuinely NEW decision. On a repeated
                    # retry the same question hits the EU-89 dedup gate, so decisions.add returns the id
                    # of the EXISTING entry (no new row is written) rather than a fresh one — re-paging
                    # it every failing pass is exactly the spam EU-90 removes. Snapshot the parked ids
                    # before the add and page only when a new id appears (the contract documented in
                    # eu89_stateful_chat_test._park_and_notify — decisions.add never returns None, so a
                    # bare `is not None` check would page on every retry).
                    parked_before = {e.get("id") for e in decisions.load(cfg)}
                    eid = decisions.add(
                        cfg, ticket, app.name, question,
                        entry_id=f"{ticket.id}#pm-findings-decisions",
                    )
                    if eid and eid not in parked_before:
                        _notify(cfg, f"❓ {ticket.id} — needs YOUR decision "
                                     f"({len(ftr.decisions)} reviewer finding(s)):\n{bullets}"
                                     f"\n\n{decisions.reply_hint(ticket.id)}")
            except Exception as exc:  # noqa: BLE001 - findings triage must never break the run
                print(f"  · PM findings triage skipped: {exc}", flush=True)

        # EU-215: advisory-only ship path. is_ship_ready() is False here ONLY because
        # blocking_issues (contracts.py) counts "major" severity as blocking — so this fires
        # exactly for the AUTO-98 class: verdict PASS, spec_met True, and every leftover
        # quality_issue is minor/major (never "blocker"). A done deliverable must not strand on
        # advisory/hygiene items (2026-07-09 forensics, 2/14 "max passes" escalations). File the
        # leftovers as backlog tickets through the same route the PM-findings out-of-scope path
        # uses (L1284-1293) and land anyway.
        advisory_ship = (
            review.verdict.value == "PASS"
            and review.spec_met
            and not review.is_ship_ready()
            and not any(q.severity == "blocker" for q in review.quality_issues)
        )
        if advisory_ship:
            filed_keys: list[str] = []
            if review.quality_issues:
                import json as _json
                advisory_proposals = [
                    {
                        "title": f"[{q.severity.upper()}/{q.area}] {q.detail[:160]}",
                        "type": "Bug",
                        "severity": q.severity.upper(),
                        "body": q.detail,
                    }
                    for q in review.quality_issues
                ]
                advisory_block = f"===TICKETS===\n{_json.dumps(advisory_proposals)}\n===END==="
                if cfg.dry_run or ticket.ephemeral:
                    titles = ", ".join(p["title"] for p in advisory_proposals)
                    print(f"  filing · {len(advisory_proposals)} advisory finding(s) "
                          f"(dry-run/ephemeral — not filed): {titles}", flush=True)
                else:
                    from . import filing as _filing
                    filing_result = _filing.file_findings(app, "out-of-scope", advisory_block)
                    filed_keys = list(filing_result.filed) + list(filing_result.deduped)
                    if filing_result.lines:
                        print("  filing · advisory findings (shipped-with-advisories):", flush=True)
                        for ln in filing_result.lines:
                            print(f"    {ln}", flush=True)
                    if filing_result.failed:
                        _notify(cfg, f"⚠️ {ticket.id} — {len(filing_result.failed)} advisory finding(s) "
                                     "could not be filed:\n" +
                                     "\n".join(f"• {t}: {e}" for t, e in filing_result.failed))
            audit.record("shipped_with_advisories", ticket_id=ticket.id, iteration=iteration,
                         filed=filed_keys,
                         issues=[{"severity": q.severity, "area": q.area, "detail": q.detail}
                                 for q in review.quality_issues])
            print(f"  ✓ {ticket.id}: PASS + spec_met with only advisory findings — shipping "
                  f"({len(filed_keys)} filed/deduped to backlog)", flush=True)

        # 4) DECIDE
        if review.is_ship_ready() or advisory_ship:
            if stop_event is not None and stop_event.is_set():
                audit.record("run_stopped", ticket_id=ticket.id, iteration=iteration, phase="pre-merge")
                print(f"  ■ {ticket.id}: stopped before merge by Commander — DEV untouched.", flush=True)
                return _resolve(TicketReport(ticket.id, Outcome.SKIPPED, iteration, cost, app.name, branch,
                                             notes="stopped by Commander before merge"))
            # Phase-2 §2 (2026-07-06): the LLM per-diff security gate was deleted (off by default;
            # its secret/dep half is now the deterministic gate.py scan, its judgment half a
            # Reviewer checklist section). A review that ships now lands directly.
            import inspect
            _land_sig = inspect.signature(_land)
            if "commenter" in _land_sig.parameters:
                result = _land(ticket, app, cfg, git, backlog, audit, branch, iteration, cost, build,
                               review, commenter=commenter)
            else:
                result = _land(ticket, app, cfg, git, backlog, audit, branch, iteration, cost, build,
                               review)
            if getattr(cfg, "scout_after_merge", False) and result.outcome == Outcome.MERGED:
                await _after_merge_scout(cfg, app, ticket, audit)
            return _resolve(result)

        last_changes = review.required_changes or review.spec_gaps or [
            q.detail for q in review.blocking_issues]
        audit.record("retry", ticket_id=ticket.id, iteration=iteration, required_changes=last_changes)
        # Retry guard: if a blocking feedback signature we've already seen in the last few passes
        # returns, the build isn't making progress on it — whether it repeats back-to-back (A/A) or
        # oscillates between two unaddressed failures (A/B/A/B). Either way, stop burning passes and
        # hand it to the PM / Commander instead of looping more identical/alternating passes.
        sig = _changes_sig(last_changes)
        if sig and sig in recent_reject_sigs:
            audit.record("retry_stuck", ticket_id=ticket.id, iteration=iteration,
                         repeats=recent_reject_sigs.count(sig) + 1)
            print("  ⚠ review feedback is repeating/oscillating — escalating instead of looping "
                  "another unproductive pass", flush=True)
            break
        recent_reject_sigs.append(sig)

        # EU-216: weak-backend 3rd pass gate. HARD_MAX_PASSES (2) is calibrated for Opus; on
        # finishing pass 2, only continue on to pass 3 when this is a weak (non-Opus) backend AND
        # the FAIL carries no blocker-severity finding — an Opus run, or a blocker on any backend,
        # stops at 2 exactly as before (the retry-stuck guard above already ran and stays first).
        if iteration >= HARD_MAX_PASSES:
            if not (weak_backend and not blockers):
                break
            if iteration < max_passes:
                audit.record("weak_extra_pass", ticket_id=ticket.id, iteration=iteration + 1,
                             backend=getattr(cfg, "model_backend", None))
                print(f"  ↻ weak backend + minor-only FAIL — granting an extra pass "
                      f"{iteration + 1}/{max_passes}", flush=True)

        _bar(REVIEW, fail=REVIEW)
        print("  ↻ changes requested → rebuilding", flush=True)

    # Exhausted the passes. Before bothering the Commander, let the PM TRIAGE: if the core deliverable
    # is done and the rejections are fixable scope-creep, hand back ONE corrective pass and re-queue
    # (the next drain re-runs it); else escalate just the genuine decision as a readable brief. Capped to
    # one triage per ticket so a truly-stuck ticket still lands on you.
    from . import pm as _pm
    from . import dashboard as _D
    triage = None
    if (getattr(cfg, "pm_enabled", True) and not ticket.ephemeral
            and not _already_pm_triaged(cfg, ticket.id)):
        try:
            triage = await _pm.triage(cfg, app.name, ticket.id,
                                      last_build=(build.summary or build.raw or ""), rejections=last_changes)
        except Exception:  # noqa: BLE001 - triage must never crash the run
            triage = None

    # EU-42: the PM may surface its own off-spec findings while triaging — route them to the backlog
    # (===TICKETS=== block carried in the PM's full raw reply) before we act on the triage verdict.
    if triage and triage.get("raw"):
        _route_out_of_scope(cfg, ticket, app, audit, triage["raw"], source="pm-triage")

    # Commander decision (Phase-2, 2026-07-06): the PM RESOLVE requeue is RETIRED for REVIEWER
    # rejections. It granted a ticket one extra capped attempt, silently re-opening the QW3
    # 2-pass loop cap — and §2 folds PM triage into the Planner's single decision anyway. A
    # RESOLVE verdict on a reviewer FAIL now escalates like everything else, carrying the PM's
    # corrective instruction as the Commander's brief (the `esc` below already prefers triage
    # text). SPLIT — a planning decision — is unchanged until the Planner absorbs it.
    #
    # EU-217: narrowed back OFF the gate-exhaustion path. A gate that fails identically twice
    # running (last_changes[0] set at the two "Verification failed…" sites above) is a build/CI
    # health question, not a spec disagreement — when the PM diagnoses RESOLVE there ("pre-
    # existing flake, deliverable done"), parking it on the Commander instead of requeuing was
    # the dishonest escalation this ticket was filed to fix (2026-07-09 forensics). Requeue
    # exactly ONCE: `_already_pm_triaged` (above) guarantees at most one triage per ticket, so
    # this cannot loop — a still-stuck ticket escalates for real on its next exhaustion.
    if triage and triage["action"] == "RESOLVE":
        if last_changes and last_changes[0].startswith("Verification failed"):
            audit.record(Outcome.REQUEUED.audit_event, ticket_id=ticket.id, action="RESOLVE",
                         reason="gate-flake", instruction=(triage.get("text") or "")[:600])
            print(f"  🎖️ {ticket.id}: PM said RESOLVE on a gate-exhaustion — requeuing once.",
                  flush=True)
            return _resolve(TicketReport(ticket.id, Outcome.REQUEUED, max_passes, cost, app.name, branch,
                                         notes="gate-flake — PM RESOLVE requeued once"))
        audit.record("pm_resolve_retired", ticket_id=ticket.id,
                     instruction=(triage.get("text") or "")[:600])
        print(f"  🎖️ {ticket.id}: PM said RESOLVE — requeue retired (Phase-2); escalating with "
              "its brief instead.", flush=True)

    if triage and triage["action"] == "SPLIT":
        # Too heavy for one build → the Scrum Master breaks it into small sub-tickets (filed on the
        # Commander), then closes the parent. The autopilot picks up the fragments next cycle.
        from . import scrum as _scrum
        recap = (build.summary or "") + ("\nReviewer wanted: " + "; ".join(last_changes[:6]) if last_changes else "")
        try:
            sp = await _scrum.split(cfg, app.name, ticket, recap=recap, reason=triage.get("text", ""))
        except Exception as exc:  # noqa: BLE001 - a split failure must fall through to a normal escalate
            sp = {"ok": False, "keys": [], "error": str(exc)}
        if sp.get("ok") and sp.get("keys"):
            kk = ", ".join(sp["keys"])
            audit.record(Outcome.REQUEUED.audit_event, ticket_id=ticket.id, action="SPLIT", into=sp["keys"])
            _notify(cfg, f"🧩 {ticket.id} was too heavy — the Scrum Master split it into {kk} (on you) and "
                         "closed the parent. The unit takes the fragments next.")
            print(f"  🧩 {ticket.id}: too heavy → Scrum Master split into {kk}; parent closed.", flush=True)
            return _resolve(TicketReport(ticket.id, Outcome.REQUEUED, max_passes, cost, app.name, branch,
                                         notes=f"too heavy — Scrum Master split into {kk}"))
        print(f"  · Scrum Master couldn't split ({sp.get('error')}) — escalating instead.", flush=True)

    # Escalate — with the PM's brief if it gave one, else the raw required-changes. Record a question so
    # Needs-you shows a readable ask (not an empty 'escalated' row) that you can answer -> re-run.
    esc = (triage.get("text") if triage else None) or _escalation_comment(last_changes)
    # EU-92: prepend the WHY PM CANNOT RESOLVE line (captured by parse_triage) so the Commander
    # sees immediately WHY the PM escalated rather than self-resolving — the one sentence that
    # describes the specific authority, credential, or irreducible context only he has.
    if triage and triage.get("why"):
        esc = f"WHY PM CANNOT RESOLVE: {triage['why']}\n\n{esc}"
    decisions.add(cfg, ticket, app.name, esc[:1500])
    if not cfg.dry_run and not ticket.ephemeral:
        # decisions.add already parked it to 'Blocked' (EU-61) — just leave the escalation note.
        backlog.add_comment(ticket, ("🎖️ [PM] " + esc[:1400]) if triage else esc)
    print("  ✗ escalated — needs you (max passes reached without a clean review)", flush=True)
    # QW3: structured disagreement record — who wanted what when the loop was cut. One event the
    # cockpit/forensics can read instead of re-mining build/review payloads.
    audit.record("builder_reviewer_disagreement", ticket_id=ticket.id, passes=max_passes,
                 builder_position=(build.summary or build.raw or "")[:800],
                 reviewer_position=[(c or "")[:300] for c in (last_changes or [])[:6]])
    _notify(cfg, f"🛑 {ticket.id} — needs you:\n\n{await _decision_brief(cfg, ticket.id, esc)}\n\n{decisions.reply_hint(ticket.id)}")
    audit.record("needs_human", ticket_id=ticket.id, iterations=max_passes,
                 reason="max passes — PM escalated", question=esc[:1500])
    return _resolve(TicketReport(ticket.id, Outcome.ESCALATED, max_passes, cost, app.name, branch,
                                 notes="max_iterations reached without a passing review"))


def _land(ticket, app, cfg, git, backlog, audit, branch, iteration, cost, build, review,
          commenter=None) -> TicketReport:
    if commenter is None:
        commenter = jira_commenter.TicketCommenter(
            cfg,
            dry_run=cfg.dry_run,
            no_comment=getattr(cfg, "no_comments", False)
        )
    """Passed review. Validate the merge on a THROWAWAY trial branch so DEV is never
    touched until the single, final, validated merge."""
    git.commit_all(f"{ticket.id}: {ticket.summary}\n\n{build.summary}\n\nReviewed-by: autodev-reviewer")
    temp = f"{app.branch_prefix}/_trial"
    merge_msg = f"Merge {branch} into {app.base_branch} ({ticket.id})"
    print(f"  land · trial-merging into {app.base_branch} (throwaway branch — DEV untouched)…", flush=True)
    _bar(LAND, active=LAND)

    clean = bool(cfg.merge_to_dev) and git.trial_merge(branch, temp, merge_msg)
    gate = run_gate(app, git.changed_paths()) if clean else None  # gate runs on the trial branch, not on DEV (EU-19: per-app)
    green = bool(gate and gate.passed)
    if gate is not None:
        # Keep the gate's evidence. A red post-merge gate used to leave only a terse pr_opened
        # event — the failing harnesses appeared nowhere (not in audit.jsonl, not in the PR), so
        # diagnosing meant re-running the whole suite (EU-139 run, 2026-07-05). One additive event
        # per gate run carries the verdict and, when red, the failing names + report tail.
        audit.record("dev_gate", ticket_id=ticket.id, passed=gate.passed,
                     failing=[] if gate.passed else _gate_failures(gate.report),
                     report_tail="" if gate.passed else (gate.report or "").strip()[-2000:])
    if not clean:
        reason = "could not merge cleanly into dev" if cfg.merge_to_dev else "merge_to_dev disabled"
    elif not green:
        reason = "dev gate fails after merge"
    else:
        reason = ""
    # Any failure lights Land red (the deleted security gate used to stop earlier at Security).
    fail_idx = LAND

    # DRY-RUN: previewed only — DEV is never touched.
    if cfg.dry_run:
        git.abandon_trial(temp)
        note = "would merge to dev (dev stays green)" if not reason else f"would open PR into dev ({reason})"
        print(f"  land · (dry-run) {note}", flush=True)
        _bar(len(PHASES)) if not reason else _bar(fail_idx, fail=fail_idx)
        _notify(cfg, f"🧪 {ticket.id} — {note}\n{ticket.summary}")
        audit.record(Outcome.SKIPPED.audit_event, ticket_id=ticket.id, note=note)
        return TicketReport(ticket.id, Outcome.SKIPPED, iteration, cost, app.name, branch, notes=note)

    # LIVE + validated -> fast-forward DEV to the trial and push: the ONLY moment DEV changes.
    if not reason:
        merge_sha = git.current_sha()   # the validated merge commit — SRE reverts THIS if DEV breaks
        git.land_trial(temp)
        # EU-81: the commit is now on remote <base> — the ticket's definition of done is met.
        # Everything below is best-effort post-land housekeeping (retire the merged feature
        # branch, then fast-forward the Mac checkout so the running cockpit never serves stale
        # code). A failure here must NEVER unwind or fail an already-successful land, so each
        # step is individually guarded.
        try:
            git.delete_local_branch(branch)    # merged into DEV (commits live there) -> retire the feature branch
            git.delete_remote_branch(branch)   # clean up remote autodev/* ref so the remote stays tidy
        except Exception as exc:  # noqa: BLE001 - branch cleanup must not fail a landed ticket
            print(f"  land · feature-branch cleanup skipped ({exc})", flush=True)
        # Bring the Mac checkout's <base> up to date so QA/the cockpit run the just-merged code.
        # Honours cfg.sync_base_after_merge (the opt-out knob) and never raises (convenience, not custody).
        sync = ""
        if cfg.sync_base_after_merge:
            try:
                sync = git.sync_main_base()
            except Exception as exc:  # noqa: BLE001 - the sync is a convenience, never custody
                print(f"  land · base sync skipped ({exc})", flush=True)
        print(f"  land · merged into {app.base_branch} ✓ (pushed) · feature branch retired", flush=True)
        if sync:
            print(f"  land · {sync}", flush=True)
        _bar(len(PHASES))
        turl = _test_url(app, build.summary)
        test_line = f"\n🔗 Test on {app.base_branch}: {turl}" if turl else ""
        # QA hand-off: a brief, BULLETED 'what was done' + the DEV test link — NOT the reviewer's full
        # essay (EU-79). The officers lead their summary with bullets; bullets() keeps ≤3 tight ones and
        # the test link drops onto its own line below, so the comment is scannable, never a prose wall.
        # EU-153: Post merge success comment
        merge_comment = commenter.summarize_gate_event(
            "Land", "PASSED",
            f"Merged to {app.base_branch}{test_line}",
            ticket.id
        )
        if merge_comment and backlog and not ticket.ephemeral:
            commenter.post_comment(backlog, ticket.key, merge_comment)
        if not ticket.ephemeral:
            from . import dashboard as _D
            whatdone = _D.bullets(review.summary or build.summary, limit=3, width=200)
            head = "marked Done" if cfg.mark_done_on_merge else "moved to QA"
            # Best-effort: the code IS merged at this point — a Jira hiccup here must degrade to a
            # log line, not propagate to _exception_report and mislabel a successful land as a
            # ticket_exception (which would strand the already-merged ticket In Progress).
            try:
                backlog.set_status(ticket, "Done" if cfg.mark_done_on_merge else "QA")
                backlog.add_comment(
                    ticket,
                    f"✅ Merged to {app.base_branch} → {head}.\nWhat was done:\n{whatdone}{test_line}")
            except Exception as exc:  # noqa: BLE001 — tracker trouble never un-lands a merge
                print(f"  land · ticket status update skipped ({exc})", flush=True)
        done = "" if ticket.ephemeral else (" · marked Done" if cfg.mark_done_on_merge else " · moved to QA")
        _notify(cfg, f"🧪 {ticket.id} ready for manual test on {app.base_branch}{done}\n{ticket.summary}{test_line}")
        audit.record(Outcome.MERGED.audit_event, ticket_id=ticket.id, base=app.base_branch,
                     done=cfg.mark_done_on_merge)
        # Self-update restart signal (2026-07-09): when the unit lands a change to ITS OWN repo, the
        # resident serve/autopilot processes keep executing the old code from memory (no hot-reload) —
        # that gap ran pre-EU-201 code for 11h and silently stranded split fragments. Say so, loudly.
        try:
            from pathlib import Path as _P
            if _P(app.repo_path).resolve() == _P(__file__).resolve().parent.parent:
                audit.record("self_update_pending_restart", ticket_id=ticket.id)
                _notify(cfg, f"⚠️ {ticket.id} changed the unit's own code — restart serve/autopilot "
                             "so the running processes load it (the keepalive daemons respawn on kill).")
        except Exception:  # noqa: BLE001 — the signal is best-effort
            pass
        # Technical Writer: log this land to the unit's feature changelog (best-effort, never breaks).
        _record_changelog(cfg, ticket, app, review.summary or build.summary, turl)

        # SRE: run the heavier post-merge suite on the landed DEV; if it's red, roll the merge
        # back (forward-only) and hand the ticket back rather than leave DEV broken.
        from . import sentinel
        if sentinel.should_run(cfg, app):
            ok, snote = sentinel.guard(cfg, app, ticket, git, merge_sha, audit)
            if not ok:
                if not ticket.ephemeral:
                    backlog.set_status(ticket, "Needs Human")
                    backlog.add_comment(ticket,
                        f"⚠️ SRE rolled back from {app.base_branch}.\n"
                        f"• {snote[:900]}")
                print(f"  🛡️ {ticket.id}: SRE reverted the merge — needs you.", flush=True)
                return TicketReport(ticket.id, Outcome.ESCALATED, iteration, cost, app.name, branch,
                                    notes=f"sentinel reverted: {snote[:160]}")

        # Post-merge SMOKE (EU-60): a fast, flag-only canary on the landed DEV. Unlike the SRE it never
        # reverts — it surfaces a red smoke loudly (smoke.run sends Telegram + records the audit event;
        # we add the ticket comment) so the Commander catches a broken DEV at QA. No-op unless the app
        # opts in a `smoke_command`. The merge stands either way.
        smoke_note = ""
        from . import smoke
        if smoke.should_run(cfg, app):
            sok, smnote = smoke.run(cfg, app, ticket, audit)
            if not sok:
                smoke_note = " · ⚠️ post-merge smoke FAILED"
                if not ticket.ephemeral:
                    backlog.add_comment(ticket,
                        f"🚨 Post-merge smoke FAILED on {app.base_branch}.\n"
                        f"• DEV is live with a failing smoke.\n"
                        f"• {smnote[:900]}")

        # CI-conclusion (EU-251): for CI-relevant tickets, poll the REAL GitHub Actions conclusion
        # for the merge commit instead of certifying the Builder's self-report ("CI will go green")
        # — AUTO-112 landed on exactly that false claim with nothing in the land path ever
        # consulting GitHub Actions. Complementary to the SRE/smoke LOCAL re-run gates (AUTO-57/83):
        # this reads the REMOTE conclusion, the only thing that catches CI-environment-only
        # failures. A complete no-op (zero `gh` calls) for a non-CI ticket; best-effort — a `gh`
        # hiccup here must never unwind an already-successful land.
        ci_note = ""
        try:
            from . import ci_conclusion
            if ci_conclusion.should_run(cfg, ticket):
                ci_result = ci_conclusion.check(cfg, app, ticket, merge_sha, audit)
                ci_note = ci_conclusion.report(cfg, app, ticket, backlog, ci_result, audit)
        except Exception as exc:  # noqa: BLE001 — best-effort, like sentinel/smoke
            print(f"  🔎 CI · conclusion check skipped ({exc})", flush=True)

        return TicketReport(ticket.id, Outcome.MERGED, iteration, cost, app.name, branch,
                            notes=f"merged to {app.base_branch}"
                            + (", Done" if cfg.mark_done_on_merge else ", awaiting QA")
                            + smoke_note + ci_note)

    # LIVE not validated -> DEV untouched; open a PR for you.
    git.abandon_trial(temp)
    git.push(branch)
    gate_failure = "" if (gate is None or gate.passed) else (gate.report or "").strip()
    pr_url = git.open_pr(branch, f"{ticket.id}: {ticket.summary}",
                         _pr_body(ticket, app, review, gate_failure=gate_failure)) \
        if cfg.open_pr_on_block else None
    print(f"  land · not auto-merged ({reason}) → "
          + (f"PR {pr_url}" if pr_url else "open a PR manually"), flush=True)
    _bar(fail_idx, fail=fail_idx)
    if not ticket.ephemeral:
        backlog.add_comment(ticket,
            f"⚠️ Passed review — not auto-merged.\n"
            f"• Reason: {reason}\n"
            + (f"• PR: {pr_url}" if pr_url else "• Open a PR manually."))
        if pr_url:
            backlog.attach_pr(ticket, pr_url)
    _notify(cfg, f"⚠️ {ticket.id} needs you — not auto-merged ({reason})\n"
            + (pr_url or "open a PR manually"))
    audit.record(Outcome.PR_OPENED.audit_event, ticket_id=ticket.id, reason=reason, pr_url=pr_url)
    return TicketReport(ticket.id, Outcome.PR_OPENED, iteration, cost, app.name, branch,
                        pr_url=pr_url, notes=reason)


async def _after_merge_scout(cfg, app, ticket, audit) -> None:
    """Opt-in: right after a live merge, the QA Engineer smoke-tests the running DEV app and (live) files
    any runtime/UX/a11y regressions it finds. Never raises — recon must not break the run."""
    try:
        from . import filing, scout
        print(f"  scout · smoke-testing {app.base_branch} after {ticket.id}…", flush=True)
        report = await scout.recon(cfg, app.name)
        _proposals, clean = filing.parse_tickets(report)
        _clean, block, _result = filing.present(report, app, "scout", do_file=not cfg.dry_run)
        Path(cfg.audit_path).with_name("scout-report.md").write_text(clean, encoding="utf-8")
        if audit is not None:
            audit.record("scout_smoke", ticket_id=ticket.id, app=app.name)
        _notify(cfg, f"🛰️ QA Engineer smoke after {ticket.id} on {app.base_branch}:\n{clean[:700]}"
                + (("\n\n" + block) if block else ""))
    except Exception as exc:  # noqa: BLE001 - after-merge recon must never break the run
        print(f"  scout smoke skipped: {exc}", flush=True)


def _distinctive_tokens(text: str) -> set[str]:
    """EU-90: distinctive (length ≥ 4) lowercase alphanumeric tokens of *text*. Dropping short tokens
    skips stopwords ('the', 'and', 'with', 'this') that would otherwise inflate the overlap score and
    cause false dedups — leaving the content words that actually identify a finding."""
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 4}


def _dedupe_oos_against_reviewer(out_of_scope: list[dict], reviewer_raw: str,
                                 *, threshold: float = 0.6) -> list[dict]:
    """EU-90 double-filing guard. Return the PM out-of-scope findings that are NOT already covered by
    a proposal in the Reviewer's own ===TICKETS=== block (parsed from `reviewer_raw`).

    Both the reviewer block and these PM findings feed the SAME backlog through `_route_out_of_scope`,
    so once `out_of_scope_autofile` is on a finding flagged in BOTH would be filed twice. A PM finding
    is treated as a duplicate when its distinctive wording substantially overlaps a reviewer proposal
    (≥2 shared content tokens AND ≥ `threshold` of the finding's tokens). Order is preserved, and a
    finding with too little text to compare is KEPT (EU-44: never silently drop a real finding)."""
    from . import filing
    reviewer_props, _ = filing.parse_tickets(reviewer_raw or "")
    if not reviewer_props:
        return list(out_of_scope)
    rev_token_sets = [
        _distinctive_tokens(f"{p.get('title', '')} {p.get('body', '')}")
        for p in reviewer_props
    ]
    fresh: list[dict] = []
    for q in out_of_scope:
        q_tokens = _distinctive_tokens(q.get("detail", ""))
        is_dup = False
        if q_tokens:
            for rt in rev_token_sets:
                overlap = len(q_tokens & rt)
                if overlap >= 2 and overlap / len(q_tokens) >= threshold:
                    is_dup = True
                    break
        if not is_dup:
            fresh.append(q)
    return fresh


def _route_out_of_scope(cfg, ticket, app, audit, report, source: str) -> None:
    """EU-42: route real-but-off-spec findings the Reviewer/PM flagged (their ===TICKETS=== block on
    `report`) into the backlog instead of losing them.

    EU-92 — the PM owns out-of-scope triage. A finding the PM itself classified as out-of-scope
    (``source="pm-findings"``) is AUTO-FILED unconditionally: the PM has already decided, so it must
    never page the Commander. The Reviewer's own raw ===TICKETS=== block honours the
    ``out_of_scope_autofile`` knob (default on): AUTO-FILE each finding as its own 'out-of-scope'-labeled
    ticket — de-duped against open tickets by filing's find_open_by_summary — or, when the knob is
    flipped off, PROPOSE-FIRST: record a pending decision so the proposals surface in the cockpit's
    'Needs you' for the Commander to wave through — never silently dropped. Never raises: routing
    findings out of the build must not break the run."""
    try:
        from . import filing
        proposals, _clean = filing.parse_tickets(report or "")
        if not proposals:
            return
        # PM-classified findings auto-file regardless of the knob — the PM is the decision-maker for
        # the out-of-scope class (EU-92) and never escalates it. The Reviewer's raw block follows the
        # knob (auto-file by default, propose-first only when the Commander explicitly opts out).
        autofile = getattr(cfg, "out_of_scope_autofile", False) or source == "pm-findings"
        if autofile:
            if cfg.dry_run:
                titles = ", ".join(str(p.get("title", "?")) for p in proposals)
                print(f"  filing · {len(proposals)} out-of-scope finding(s) ({source}, dry-run — not filed): "
                      f"{titles}", flush=True)
                return
            result = filing.file_findings(app, "out-of-scope", report)
            if audit is not None:
                audit.record("out_of_scope_filed", ticket_id=ticket.id, source=source,
                             filed=result.filed, deduped=result.deduped,
                             failed=[t for t, _ in result.failed])
            if result.lines:
                print(f"  filing · out-of-scope findings ({source}):", flush=True)
                for ln in result.lines:
                    print(f"    {ln}", flush=True)
            if result.failed:
                _notify(cfg, f"⚠️ {ticket.id} — {len(result.failed)} out-of-scope finding(s) could not be "
                             "filed:\n" + "\n".join(f"• {t}: {e}" for t, e in result.failed))
            # EU-284: a CRITICAL out-of-scope finding must not rot silently — the class is normally
            # AUTO-FILED with no page (EU-92), but a critical discovery pages the Commander once,
            # naming every newly-filed critical key+title. MEDIUM/LOW stay silent (current behavior).
            if result.filed_critical:
                names = "\n".join(f"• {k} — {t}" for k, t in result.filed_critical)
                _notify(cfg, f"🚨 {ticket.id} — CRITICAL out-of-scope finding auto-filed ({source}):\n{names}")
        else:
            titles = "\n".join(f"• [{p.get('severity', '?')}] {p.get('title')}" for p in proposals)
            question = ("Out-of-scope findings surfaced while working this ticket — file them as their own "
                        f"backlog tickets?\n{titles}\n\n(set out_of_scope_autofile to file these "
                        "automatically next time.)")
            # Distinct decision id so the proposal doesn't clobber (or get clobbered by) a needs_human
            # decision recorded for the SAME ticket — both must survive in the cockpit 'Needs you'.
            # EU-83: attach the raw ===TICKETS=== report so handle_reply's _file_out_of_scope_resume can
            # file these findings when the Commander approves — without it the resume has nothing to file.
            decisions.add(cfg, ticket, app.name, question, entry_id=f"{ticket.id}#out-of-scope",
                          extra={"out_of_scope_report": report})
            if audit is not None:
                audit.record("out_of_scope_proposed", ticket_id=ticket.id, source=source,
                             titles=[p.get("title") for p in proposals])
            print(f"  filing · {len(proposals)} out-of-scope finding(s) proposed → cockpit 'Needs you' "
                  f"({source})", flush=True)
    except Exception as exc:  # noqa: BLE001 - routing findings must never break the run
        print(f"  filing · out-of-scope routing skipped ({source}): {exc}", flush=True)


def _cleanup(cfg, git, audit, report) -> None:
    """Return the tree to a clean base branch between tickets. On a live escalation,
    preserve the builder's WIP on the feature branch first. In dry-run, delete the
    throwaway local branch."""
    try:
        if (not cfg.dry_run and report is not None
                and report.outcome == Outcome.ESCALATED and git.has_changes()):
            git.commit_all(f"WIP [autodev]: {report.ticket_id} needs human review")
            try:
                git.push(report.branch)
            except Exception as exc:  # noqa: BLE001
                audit.record("cleanup_push_failed", ticket_id=report.ticket_id, error=str(exc))
        git.discard_and_return_base()
        if cfg.dry_run and report is not None and report.branch:
            git.delete_local_branch(report.branch)
    except Exception as exc:  # noqa: BLE001 - cleanup must never crash the run
        audit.record("cleanup_failed", error=str(exc))


_GATE_FAIL_NAME = re.compile(r"^\s*[✗✘×]\s+(\S+)")


def _gate_failures(report: str, limit: int = 12) -> list[str]:
    """Best-effort names of the failing checks in a gate report — the per-check `✗ <name>` lines
    and the trailing `FAILED: a b c` summary that tests/run_all.py (and most runners) print.
    Empty when the runner doesn't name its failures; the report tail still carries the detail."""
    names: list[str] = []
    for ln in (report or "").splitlines():
        m = _GATE_FAIL_NAME.match(ln)
        if m:
            if m.group(1) not in names:
                names.append(m.group(1))
            continue
        s = ln.strip()
        if s.startswith("FAILED:"):
            for n in s[len("FAILED:"):].split():
                if n not in names:
                    names.append(n)
    return names[:limit]


def _pr_body(ticket: Ticket, app: AppConfig, review, gate_failure: str = "") -> str:
    ac = "\n".join(f"- [x] {c}" for c in ticket.acceptance_criteria)
    gf = ""
    if gate_failure:
        failing = _gate_failures(gate_failure)
        names = ("**Failing:** " + ", ".join(f"`{n}`" for n in failing) + "\n\n") if failing else ""
        gf = f"\n## Dev gate — FAILED after merge\n{names}```\n{gate_failure[-2000:]}\n```\n"
    return (f"Automated implementation of **{ticket.id}** for `{app.name}`, targeting "
            f"`{app.base_branch}`.\n\n{ticket.url or ''}\n{gf}\n"
            f"## Acceptance criteria\n{ac}\n\n## Reviewer summary\n{review.summary}\n")


def _escalation_comment(last_changes: list[str]) -> str:
    items = "\n".join(f"- {c}" for c in last_changes) or "- (no specific feedback captured)"
    return ("Automated pipeline could not complete this ticket within the iteration "
            f"limit. Outstanding items:\n{items}")


async def _decision_brief(cfg: Config, ticket_id: str, raw: str) -> str:
    """Distil a verbose escalation (reviewer/PM notes) into a phone-sized DECISION: the one question,
    the concrete options, and a recommendation — so the Commander reads what to DECIDE, not the whole
    review essay. Cheapest model, one shot, no file access; fails safe to the rule-based
    dashboard.brief() if the model errors so an escalation is never lost. Shares the cheap-model engine
    (notify.distill) with the report briefs (EU-62)."""
    from . import dashboard as _D
    raw = (raw or "").strip()
    if not raw:
        return ""
    system = ("Compress a stuck ticket's reviewer/PM notes into a DECISION BRIEF for a busy engineer "
              "reading it on his phone. Output ONLY, no preamble:\n"
              "first line: the single decision he must make (≤18 words)\n"
              "then up to 3 short bullets — the concrete options\n"
              "last line: 'Rec: <one-line recommendation>'\n"
              "≤55 words total; never restate the whole review.")
    return await notify.distill(
        cfg, system=system,
        user=f"Ticket {ticket_id}. Reviewer/PM notes:\n{raw[:2200]}\n\nWrite the decision brief.",
        tag="decision-brief", fallback=lambda: _D.brief(raw))
