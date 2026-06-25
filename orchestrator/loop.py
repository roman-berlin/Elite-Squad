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
from contextlib import ExitStack, contextmanager
from pathlib import Path

from . import builder as builder_mod
from . import decisions
from . import notify
from . import provost as provost_mod
from . import reviewer as reviewer_mod
from . import test_engineer as test_engineer_mod
from .audit import AuditLog
from .backlog.base import BacklogAdapter, NoneBacklog, make_backlog
from .config import AppConfig, Config
from .contracts import BuildRequest, Outcome, Ticket, TicketReport
from .gate import run_gate
from .git_ops import Git, GitError


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


async def _exception_report(cfg: Config, ticket: Ticket, app: AppConfig, exc: Exception,
                            audit: AuditLog) -> TicketReport:
    """Turn a ticket-level exception into a report. A turn-limit blow-out is NOT a real failure — the
    ticket was simply too big to finish in one pass. "Too big" is the Scrum Master's job, so first hand
    it to him to split into right-sized sub-tickets, and only escalate to the Commander if a split isn't
    possible (delegation off, or no backlog to file sub-tickets into, or the splitter declines)."""
    msg = str(exc)
    if _is_turn_limit(msg):
        # Auto-split first — don't ask the Commander to do by hand what the Scrum Master is for.
        # Needs a real backlog to file sub-tickets into, so only for non-ephemeral tickets.
        if not ticket.ephemeral:
            try:
                from . import scrum as _scrum
                sp = await _scrum.split(
                    cfg, app.name, ticket,
                    recap=f"{ticket.id} ran out of turns before finishing — too big for a single pass.",
                    reason="Builder hit the turn limit — split into smaller, independently-shippable tickets.")
            except Exception as sexc:  # noqa: BLE001 - a split failure must fall through to escalate
                sp = {"ok": False, "keys": [], "error": str(sexc)}
            if sp.get("ok") and sp.get("keys"):
                kk = ", ".join(sp["keys"])
                audit.record("scrum_split", ticket_id=ticket.id, reason="turn-limit", into=sp["keys"])
                _notify(cfg, f"🧩 {ticket.id} was too big for one pass — the Scrum Master split it into "
                             f"{kk} and closed the parent. The unit takes the fragments next.")
                print(f"  🧩 {ticket.id}: too big → Scrum Master split into {kk}; parent closed.", flush=True)
                return TicketReport(ticket.id, Outcome.REQUEUED, 0, 0.0, app.name,
                                    notes=f"too big — Scrum Master split into {kk}")
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
    audit.record("ticket_exception", ticket_id=ticket.id, app=app.name, error=msg)
    _notify(cfg, f"❌ {ticket.id} — error: {msg[:200]}")
    return TicketReport(ticket.id, Outcome.ERRORED, 0, 0.0, app.name, notes=msg)


_PHASES = ("Build", "Gate", "Review", "Land")


def _bar(done: int, active: int = -1, fail: int = -1) -> None:
    """A phase progress checklist:  ✓ Build   ✓ Gate   ⏳ Review   ○ Land"""
    cells = []
    for i, name in enumerate(_PHASES):
        glyph = "✗" if i == fail else "✓" if i < done else "⏳" if i == active else "○"
        cells.append(f"{glyph} {name}")
    print("    " + "   ".join(cells), flush=True)


def _worktree_path(app: AppConfig, cfg: Config) -> str:
    """Where the General keeps this app's private worktree (a sibling of the repo)."""
    if getattr(cfg, "worktree_dir", None):
        return str(Path(cfg.worktree_dir).expanduser() / app.name)
    repo = Path(app.repo_path).expanduser().resolve()
    return str(repo.parent / ".general-worktrees" / app.name)


def _make_git(cfg: Config, app: AppConfig) -> Git:
    """Build the git custodian for an app. With use_worktree on, the General gets a
    dedicated linked worktree (based on origin/<base>) so it never fights the user's
    manual checkout. Falls back to in-tree if isolation can't engage (e.g. no origin)."""
    if getattr(cfg, "use_worktree", False):
        wt = _worktree_path(app, cfg)
        try:
            git = Git(app.repo_path, app.base_branch, app.protected_branch, worktree_path=wt)
            created = git.setup()
            app.workdir = git.workdir
            if created and getattr(cfg, "worktree_setup_cmd", None):
                print(f"  · worktree created — setup: {cfg.worktree_setup_cmd}", flush=True)
                subprocess.run(cfg.worktree_setup_cmd, shell=True, cwd=git.workdir, check=False)
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
    restored = subprocess.run(
        ["git", "checkout", base_ref, "--", "bun.lock"],
        cwd=str(workdir), capture_output=True, text=True,
    )
    if restored.returncode != 0:
        why = (restored.stderr.strip().splitlines() or ["no bun.lock at base"])[0]
        print(f"  · dep isolation: bun.lock not restored from {base_ref} ({why})", flush=True)
    # 2) reinstall frozen so the install can't drift off the pinned lock.
    print("  · dep isolation — bun install --frozen-lockfile", flush=True)
    proc = subprocess.run(
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
        for app, ticket in worklist:
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
                report = await _exception_report(cfg, ticket, app, exc, audit)
            reports.append(report)
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
            decisions.add(cfg, ticket, app.name, note)
            _notify(cfg, f"🚧 {ticket.id} — handed back, not ready to build:\n\n{notify.clip(note, 600)}"
                         f"\n\n{decisions.reply_hint(ticket.id)}")
            audit.record("not_ready", ticket_id=ticket.id, missing=missing)
            print(f"  🚧 {ticket.id}: not ready — handed back (no build spent).", flush=True)
            return TicketReport(ticket.id, Outcome.ESCALATED, 0, 0.0, app.name, branch,
                                notes="not ready — handed back before build")

    if not cfg.dry_run and not ticket.ephemeral:
        backlog.set_status(ticket, "In Progress")
    git.checkout_feature(branch)
    # EU-18: re-pin the (possibly reused) worktree's deps to DEV's bun.lock before any
    # build runs, so a prior ticket's dependency drift can't leak into this one.
    _repin_worktree_deps(cfg, app, git)
    print(f"\n» {ticket.id}  →  branch {branch}", flush=True)
    _notify(cfg, f"🔨 {ticket.id} started — {ticket.summary}\nbranch: {branch}")

    report: TicketReport | None = None
    try:
        report = await _attempt(ticket, app, cfg, git, backlog, audit, budget, branch, stop_event)
        return report
    finally:
        _cleanup(cfg, git, audit, report)


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


def _changes_sig(changes: list[str]) -> str:
    """A stable fingerprint of a review's required changes, so two passes that get the SAME blocking
    feedback can be detected as 'stuck' (the build isn't addressing it) and escalated instead of burning
    more identical passes. Order-independent; tolerant of trivial whitespace/case drift."""
    norm = sorted({" ".join((c or "").lower().split())[:160] for c in (changes or []) if (c or "").strip()})
    return "|".join(norm)


async def _attempt(ticket, app, cfg, git, backlog, audit, budget, branch, stop_event=None) -> TicketReport:
    cost = 0.0
    last_changes: list[str] = []
    prev_reject_sig: str | None = None     # retry guard: detect the same rejection coming back unaddressed
    coverage_artifact = ""        # Test Engineer's PR coverage line for this ticket (latest pass)
    pm_used = False

    for iteration in range(1, cfg.max_iterations + 1):
        if stop_event is not None and stop_event.is_set():
            audit.record("run_stopped", ticket_id=ticket.id, iteration=iteration, phase="pre-build")
            print(f"  ■ {ticket.id}: stopped by Commander — no merge.", flush=True)
            return TicketReport(ticket.id, Outcome.SKIPPED, iteration, cost, app.name, branch,
                                notes="stopped by Commander")
        if budget.exceeded():
            audit.record("budget_exceeded", ticket_id=ticket.id, spent=budget.spent)
            return TicketReport(ticket.id, Outcome.ESCALATED, iteration, cost, app.name, branch,
                                notes="cost budget exceeded")

        # 1) BUILD  — effort is sized from the ticket (XS→low … XL→max), then escalates on retry
        eff, eff_reason = builder_mod.effort_plan(cfg, iteration, ticket)
        print(f"  build · pass {iteration}/{cfg.max_iterations} (effort {eff} — {eff_reason}) "
              f"— builder working (can take a few minutes)…", flush=True)
        _bar(0, active=0)
        req = BuildRequest(ticket=ticket, branch=branch, prior_issues=last_changes, iteration=iteration)
        build = await builder_mod.build(req, app, cfg, audit=audit)
        cost += build.cost_usd
        budget.add(build.cost_usd)
        audit.record("build", ticket_id=ticket.id, iteration=iteration, ok=build.ok,
                     cost_usd=build.cost_usd, turns=build.num_turns,
                     effort=eff, effort_reason=eff_reason,
                     tools=build.tools, summary=(build.summary or "")[:1000])
        if not build.ok:
            return TicketReport(ticket.id, Outcome.ERRORED, iteration, cost, app.name, branch,
                                notes="builder process errored")
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
                            backlog.add_comment(ticket, "🤖 Automode — the PM decided this "
                                "autonomously (review & reverse if needed; lands on DEV, not "
                                "production):\n\n" + pm_outcome["body"][:1200])
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
                return TicketReport(ticket.id, Outcome.ESCALATED, iteration, cost, app.name, branch,
                                    notes="parked — Commander product decision needed")
            audit.record("no_changes", ticket_id=ticket.id, iteration=iteration)
            return TicketReport(ticket.id, Outcome.ERRORED, iteration, cost, app.name, branch,
                                notes="builder produced no changes")
        print(f"    builder done — {build.num_turns} steps, files changed ✓", flush=True)
        _bar(1, active=1)
        if cfg.notify_verbose:
            _notify(cfg, f"🔧 {ticket.id} — Engineer implemented (pass {iteration})")

        # 2) VERIFICATION GATE on the feature branch (cheap filter, before review)
        # Gate only the monorepo apps/packages this ticket actually touched (EU-19) —
        # falls back to the repo-wide gate when no per-app config matches the diff.
        gate = run_gate(app, git.changed_paths())
        audit.record("gate", ticket_id=ticket.id, iteration=iteration, passed=gate.passed,
                     report=("" if gate.passed else (gate.report or "")[:2500]))
        if not gate.passed:
            print("  gate · FAILED → sending fixes back to builder", flush=True)
            _bar(1, fail=1)
            last_changes = [f"Verification failed; fix these:\n{gate.report}"]
            continue
        if app.gate_commands:
            print("  gate · passed", flush=True)
        _bar(2, active=2)

        # 2.5) TEST ENGINEER — coverage gate: after the build, before review, ensure the change is
        # proven (happy-path + regression test) and own the coverage artifact for the PR description.
        if getattr(cfg, "test_gate", True):
            print("  tests · Test Engineer covering the change…", flush=True)
            te = await test_engineer_mod.ensure_coverage(ticket, app, cfg)
            cost += te.cost_usd
            budget.add(te.cost_usd)
            audit.record("test_engineer", ticket_id=ticket.id, iteration=iteration, ok=te.ok,
                         coverage=te.coverage, cost_usd=te.cost_usd, turns=te.num_turns,
                         tools=te.tools, summary=(te.summary or "")[:1000])
            if te.coverage:
                coverage_artifact = te.coverage
                print(f"  tests · coverage {te.coverage}", flush=True)
            elif te.ok:
                print("  tests · Test Engineer added tests (no coverage delta reported)", flush=True)
            else:
                print("  tests · Test Engineer errored — proceeding to review", flush=True)
            # The Test Engineer may have added test files. Re-run the SAME verification gate the
            # build passed — run_gate over the app's configured gate_commands. That re-gate catches a
            # newly-broken test ONLY when those commands actually run the repo's tests (e.g.
            # automatixy's vitest, the EU repo's `python3 tests/run_all.py`); when the gate is
            # lint/typecheck-only it instead catches type/lint breakage the new test files introduced,
            # and a failing test would surface later (the Test Engineer's own run, or CI). Either way a
            # red here goes back to the builder now rather than as a confusing review failure.
            te_gate = run_gate(app, git.changed_paths())
            if not te_gate.passed:
                print("  gate · FAILED after tests → sending fixes back to builder", flush=True)
                _bar(1, fail=1)
                last_changes = [f"Verification failed after the coverage pass; fix these:\n{te_gate.report}"]
                continue

        # 3) REVIEW (spec + quality) on the diff
        print("  review · reviewer reading the diff…", flush=True)
        diff = git.diff_against_base()
        review = await reviewer_mod.review(diff, ticket, app, cfg)
        cost += review.cost_usd
        budget.add(review.cost_usd)
        # Unparseable reviewer output fails closed. Before paying for a full rebuild+review pass
        # (up to 4 Opus passes), retry JUST the review once — a parse miss is near-zero with Opus
        # and re-running the read-only review is far cheaper than rebuilding (EU-11).
        if review.parse_failed:
            print("  review · unparseable verdict → re-reviewing once (no rebuild)", flush=True)
            audit.record("review_parse_retry", ticket_id=ticket.id, iteration=iteration)
            review = await reviewer_mod.review(diff, ticket, app, cfg)
            cost += review.cost_usd
            budget.add(review.cost_usd)
        audit.record("review", ticket_id=ticket.id, iteration=iteration,
                     verdict=review.verdict.value, spec_met=review.spec_met,
                     blocking=len(review.blocking_issues), cost_usd=review.cost_usd,
                     diff_hash=AuditLog.diff_hash(diff),
                     summary=(review.summary or "")[:1000],
                     required_changes=review.required_changes,
                     issues=[{"severity": q.severity, "area": q.area, "detail": q.detail}
                             for q in review.quality_issues])
        print(f"  review · verdict {review.verdict.value}"
              + (f" — {len(review.blocking_issues)} blocking issue(s)" if review.blocking_issues else ""),
              flush=True)
        blockers = [q for q in review.quality_issues if q.severity == "blocker"]
        if blockers:
            _notify(cfg, f"🚨 {ticket.id} — Inspector found a critical issue ({blockers[0].area}): "
                         f"{blockers[0].detail}")
        if cfg.notify_verbose:
            _notify(cfg, f"🔎 {ticket.id} — Inspector verdict: {review.verdict.value}")

        # A product/scope decision only the Commander can make -> stop and ask, don't loop.
        if review.needs_human:
            decisions.add(cfg, ticket, app.name, review.question or review.summary)
            _notify(cfg, f"❓ {ticket.id} — needs YOUR decision:\n{await _decision_brief(cfg, ticket.id, review.question or review.summary)}"
                         f"\n\n{decisions.reply_hint(ticket.id)}")
            if not cfg.dry_run and not ticket.ephemeral:
                backlog.set_status(ticket, "Needs Human")
                backlog.add_comment(ticket, f"Needs a product decision: {review.question}")
            audit.record("needs_human", ticket_id=ticket.id, question=review.question)
            return TicketReport(ticket.id, Outcome.ESCALATED, iteration, cost, app.name, branch,
                                notes=f"needs decision: {review.question[:140]}")

        # 4) DECIDE
        if review.is_ship_ready():
            if stop_event is not None and stop_event.is_set():
                audit.record("run_stopped", ticket_id=ticket.id, iteration=iteration, phase="pre-merge")
                print(f"  ■ {ticket.id}: stopped before merge by Commander — DEV untouched.", flush=True)
                return TicketReport(ticket.id, Outcome.SKIPPED, iteration, cost, app.name, branch,
                                    notes="stopped by Commander before merge")
            security_block = None
            if getattr(cfg, "security_gate", False):
                print("  security · Security Engineer gating the diff…", flush=True)
                sec_ok, sec_report = await provost_mod.gate(cfg, app, diff)
                if not sec_ok:
                    print("  security · Security Engineer BLOCK (CRITICAL/HIGH) → PR for you, DEV untouched", flush=True)
                    _notify(cfg, f"🛡️ {ticket.id} — Security Engineer blocked the merge (security).\n\n{sec_report[:1200]}")
                    audit.record("security_block", ticket_id=ticket.id, iteration=iteration,
                                 reason=(sec_report or "")[:2500])
                    security_block = sec_report
                else:
                    print("  security · Security Engineer PASS ✓", flush=True)
            result = _land(ticket, app, cfg, git, backlog, audit, branch, iteration, cost, build,
                           review, security_block=security_block, coverage=coverage_artifact)
            if getattr(cfg, "scout_after_merge", False) and result.outcome == Outcome.MERGED:
                await _after_merge_scout(cfg, app, ticket, audit)
            return result

        last_changes = review.required_changes or review.spec_gaps or [
            q.detail for q in review.blocking_issues]
        audit.record("retry", ticket_id=ticket.id, iteration=iteration, required_changes=last_changes)
        # Retry guard: if the SAME blocking feedback returns a second time, the build isn't making
        # progress on it — stop burning identical passes and hand it to the PM / Commander instead.
        sig = _changes_sig(last_changes)
        if sig and sig == prev_reject_sig:
            audit.record("retry_stuck", ticket_id=ticket.id, iteration=iteration, repeats=2)
            print("  ⚠ same review feedback twice — escalating instead of looping another identical pass",
                  flush=True)
            break
        prev_reject_sig = sig
        _bar(2, fail=2)
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

    if triage and triage["action"] == "RESOLVE":
        audit.record("pm_triage", ticket_id=ticket.id, action="RESOLVE", instruction=triage["text"][:600])
        if not cfg.dry_run and not ticket.ephemeral:
            try:
                backlog.add_comment(ticket, "🎖️ [PM] One focused pass to finish — stay strictly in "
                                    "scope:\n\n" + triage["text"][:1500])
                backlog.set_status(ticket, "To Do")   # re-queue; the next drain re-runs it with this note
            except Exception:  # noqa: BLE001
                pass
        _notify(cfg, f"🎖️ {ticket.id} — the PM is finishing it (one corrective pass):\n\n{_D.brief(triage['text'])}")
        print(f"  🎖️ {ticket.id}: PM triage → re-queued for one corrective pass.", flush=True)
        return TicketReport(ticket.id, Outcome.REQUEUED, cfg.max_iterations, cost, app.name, branch,
                            notes="PM triage — re-queued for one corrective pass")

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
            audit.record("pm_triage", ticket_id=ticket.id, action="SPLIT", into=sp["keys"])
            _notify(cfg, f"🧩 {ticket.id} was too heavy — the Scrum Master split it into {kk} (on you) and "
                         "closed the parent. The unit takes the fragments next.")
            print(f"  🧩 {ticket.id}: too heavy → Scrum Master split into {kk}; parent closed.", flush=True)
            return TicketReport(ticket.id, Outcome.REQUEUED, cfg.max_iterations, cost, app.name, branch,
                                notes=f"too heavy — Scrum Master split into {kk}")
        print(f"  · Scrum Master couldn't split ({sp.get('error')}) — escalating instead.", flush=True)

    # Escalate — with the PM's brief if it gave one, else the raw required-changes. Record a question so
    # Needs-you shows a readable ask (not an empty 'escalated' row) that you can answer -> re-run.
    esc = (triage.get("text") if triage else None) or _escalation_comment(last_changes)
    decisions.add(cfg, ticket, app.name, esc[:1500])
    if not cfg.dry_run and not ticket.ephemeral:
        backlog.set_status(ticket, "Needs Human")
        backlog.add_comment(ticket, ("🎖️ [PM] " + esc[:1400]) if triage else esc)
    print("  ✗ escalated — needs you (max passes reached without a clean review)", flush=True)
    _notify(cfg, f"🛑 {ticket.id} — needs you:\n\n{await _decision_brief(cfg, ticket.id, esc)}\n\n{decisions.reply_hint(ticket.id)}")
    audit.record("needs_human", ticket_id=ticket.id, iterations=cfg.max_iterations,
                 reason="max passes — PM escalated", question=esc[:1500])
    return TicketReport(ticket.id, Outcome.ESCALATED, cfg.max_iterations, cost, app.name, branch,
                        notes="max_iterations reached without a passing review")


def _land(ticket, app, cfg, git, backlog, audit, branch, iteration, cost, build, review,
          security_block=None, coverage="") -> TicketReport:
    """Passed review. Validate the merge on a THROWAWAY trial branch so DEV is never
    touched until the single, final, validated merge."""
    git.commit_all(f"{ticket.id}: {ticket.summary}\n\n{build.summary}\n\nReviewed-by: autodev-reviewer")
    temp = f"{app.branch_prefix}/_trial"
    merge_msg = f"Merge {branch} into {app.base_branch} ({ticket.id})"
    print(f"  land · trial-merging into {app.base_branch} (throwaway branch — DEV untouched)…", flush=True)
    _bar(3, active=3)

    clean = bool(cfg.merge_to_dev) and git.trial_merge(branch, temp, merge_msg)
    green = clean and run_gate(app, git.changed_paths()).passed   # gate runs on the trial branch, not on DEV (EU-19: per-app)
    if not clean:
        reason = "could not merge cleanly into dev" if cfg.merge_to_dev else "merge_to_dev disabled"
    elif not green:
        reason = "dev gate fails after merge"
    elif security_block:
        reason = "Security Engineer blocked — CRITICAL/HIGH security finding"
    else:
        reason = ""

    # DRY-RUN: previewed only — DEV is never touched.
    if cfg.dry_run:
        git.abandon_trial(temp)
        note = "would merge to dev (dev stays green)" if not reason else f"would open PR into dev ({reason})"
        print(f"  land · (dry-run) {note}", flush=True)
        _bar(4) if not reason else _bar(3, fail=3)
        _notify(cfg, f"🧪 {ticket.id} — {note}\n{ticket.summary}")
        audit.record("dryrun_land", ticket_id=ticket.id, note=note)
        return TicketReport(ticket.id, Outcome.SKIPPED, iteration, cost, app.name, branch, notes=note)

    # LIVE + validated -> fast-forward DEV to the trial and push: the ONLY moment DEV changes.
    if not reason:
        merge_sha = git.current_sha()   # the validated merge commit — Sentinel reverts THIS if DEV breaks
        git.land_trial(temp)
        git.delete_local_branch(branch)   # merged into DEV (commits live there) -> retire the feature branch
        sync = git.sync_main_base() if getattr(cfg, "sync_base_after_merge", False) else ""
        print(f"  land · merged into {app.base_branch} ✓ (pushed) · feature branch retired", flush=True)
        if sync:
            print(f"  land · {sync}", flush=True)
        _bar(4)
        turl = _test_url(app, build.summary)
        test_line = f"\n🔗 Test on {app.base_branch}: {turl}" if turl else ""
        # QA hand-off: a brief 'what was done' + the DEV test link — NOT the reviewer's full essay.
        if not ticket.ephemeral:
            from . import dashboard as _D
            whatdone = _D.brief(review.summary or build.summary, n=220)
            head = "marked Done" if cfg.mark_done_on_merge else "moved to QA"
            backlog.set_status(ticket, "Done" if cfg.mark_done_on_merge else "QA")
            backlog.add_comment(
                ticket, f"✅ Merged to {app.base_branch} — {head}.\nWhat was done: {whatdone}{test_line}")
        done = "" if ticket.ephemeral else (" · marked Done" if cfg.mark_done_on_merge else " · moved to QA")
        _notify(cfg, f"🧪 {ticket.id} ready for manual test on {app.base_branch}{done}\n{ticket.summary}{test_line}")
        audit.record("merged", ticket_id=ticket.id, base=app.base_branch, done=cfg.mark_done_on_merge)

        # Sentinel: run the heavier post-merge suite on the landed DEV; if it's red, roll the merge
        # back (forward-only) and hand the ticket back rather than leave DEV broken.
        from . import sentinel
        if sentinel.should_run(cfg, app):
            ok, snote = sentinel.guard(cfg, app, ticket, git, merge_sha, audit)
            if not ok:
                if not ticket.ephemeral:
                    backlog.set_status(ticket, "Needs Human")
                    backlog.add_comment(ticket, f"⚠️ SRE rolled this back from {app.base_branch}. {snote[:900]}")
                print(f"  🛡️ {ticket.id}: SRE reverted the merge — needs you.", flush=True)
                return TicketReport(ticket.id, Outcome.ESCALATED, iteration, cost, app.name, branch,
                                    notes=f"sentinel reverted: {snote[:160]}")

        return TicketReport(ticket.id, Outcome.MERGED, iteration, cost, app.name, branch,
                            notes=f"merged to {app.base_branch}"
                            + (", Done" if cfg.mark_done_on_merge else ", awaiting QA"))

    # LIVE not validated -> DEV untouched; open a PR for you.
    git.abandon_trial(temp)
    git.push(branch)
    pr_url = git.open_pr(branch, f"{ticket.id}: {ticket.summary}", _pr_body(ticket, app, review, coverage)) \
        if cfg.open_pr_on_block else None
    print(f"  land · not auto-merged ({reason}) → "
          + (f"PR {pr_url}" if pr_url else "open a PR manually"), flush=True)
    _bar(3, fail=3)
    if not ticket.ephemeral:
        backlog.add_comment(ticket, f"Passed review but not auto-merged ({reason})."
                            + (f" PR: {pr_url}" if pr_url else " Open a PR manually."))
        if pr_url:
            backlog.attach_pr(ticket, pr_url)
    _notify(cfg, f"⚠️ {ticket.id} needs you — not auto-merged ({reason})\n"
            + (pr_url or "open a PR manually"))
    audit.record("pr_opened", ticket_id=ticket.id, reason=reason, pr_url=pr_url)
    return TicketReport(ticket.id, Outcome.PR_OPENED, iteration, cost, app.name, branch,
                        pr_url=pr_url, notes=reason)


async def _after_merge_scout(cfg, app, ticket, audit) -> None:
    """Opt-in: right after a live merge, the Scout smoke-tests the running DEV app and (live) files
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


def _pr_body(ticket: Ticket, app: AppConfig, review, coverage: str = "") -> str:
    ac = "\n".join(f"- [x] {c}" for c in ticket.acceptance_criteria)
    cov = f"\n## Coverage\n{coverage}\n" if coverage else ""
    return (f"Automated implementation of **{ticket.id}** for `{app.name}`, targeting "
            f"`{app.base_branch}`.\n\n{ticket.url or ''}\n\n"
            f"## Acceptance criteria\n{ac}\n{cov}\n## Reviewer summary\n{review.summary}\n")


def _escalation_comment(last_changes: list[str]) -> str:
    items = "\n".join(f"- {c}" for c in last_changes) or "- (no specific feedback captured)"
    return ("Automated pipeline could not complete this ticket within the iteration "
            f"limit. Outstanding items:\n{items}")


async def _decision_brief(cfg: Config, ticket_id: str, raw: str) -> str:
    """Distil a verbose escalation (reviewer/PM notes) into a phone-sized DECISION: the one question,
    the concrete options, and a recommendation — so the Commander reads what to DECIDE, not the whole
    review essay. Cheapest model, one shot, no file access; fails safe to the rule-based
    dashboard.brief() if the model errors so an escalation is never lost."""
    from . import dashboard as _D
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        from claude_agent_sdk import ClaudeAgentOptions
        from .agent import run_agent
        system = ("Compress a stuck ticket's reviewer/PM notes into a DECISION BRIEF for a busy engineer "
                  "reading it on his phone. Output ONLY, no preamble:\n"
                  "first line: the single decision he must make (≤18 words)\n"
                  "then up to 3 short bullets — the concrete options\n"
                  "last line: 'Rec: <one-line recommendation>'\n"
                  "≤55 words total; never restate the whole review.")
        run = await run_agent(
            f"Ticket {ticket_id}. Reviewer/PM notes:\n{raw[:2200]}\n\nWrite the decision brief.",
            ClaudeAgentOptions(model=getattr(cfg, "smalltalk_model", None) or cfg.discussion_model,
                               system_prompt=system, permission_mode="bypassPermissions",
                               allowed_tools=[], setting_sources=[], max_turns=1, effort="low"),
            tag="decision-brief")
        return (run.final or run.text or "").strip() or _D.brief(raw)
    except Exception:  # noqa: BLE001 - summarisation must never break the escalation path
        return _D.brief(raw)
