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


def _exception_report(cfg: Config, ticket: Ticket, app: AppConfig, exc: Exception,
                      audit: AuditLog) -> TicketReport:
    """Turn a ticket-level exception into a report. A turn-limit blow-out is NOT a real failure —
    the ticket was simply too big to finish in one pass — so surface it as an actionable 'needs
    you' (split it / raise the budget), not a confusing 'errored'."""
    msg = str(exc)
    if _is_turn_limit(msg):
        note = (f"{ticket.id} ran out of turns before finishing — this ticket is likely too big for "
                "a single pass. Split it into smaller tickets, or raise the turn budget "
                "(builder_max_turns). Nothing was merged.")
        try:
            decisions.add(cfg, ticket, app.name, note)
        except Exception:  # noqa: BLE001 - never let the escalation path itself crash the run
            pass
        audit.record("needs_human", ticket_id=ticket.id, reason="turn-limit", question=note)
        _notify(cfg, f"🛑 {ticket.id} — ran out of turns (too big to finish in one pass). "
                     "Split it, or raise builder_max_turns.")
        print(f"  🛑 {ticket.id}: ran out of turns — ticket too big; escalated to you.", flush=True)
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
                report = _exception_report(cfg, ticket, app, exc, audit)
            reports.append(report)
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
            _notify(cfg, f"🚧 {ticket.id} — handed back, not ready to build:\n\n{note}")
            audit.record("not_ready", ticket_id=ticket.id, missing=missing)
            print(f"  🚧 {ticket.id}: not ready — handed back (no build spent).", flush=True)
            return TicketReport(ticket.id, Outcome.ESCALATED, 0, 0.0, app.name, branch,
                                notes="not ready — handed back before build")

    if not cfg.dry_run and not ticket.ephemeral:
        backlog.set_status(ticket, "In Progress")
    git.checkout_feature(branch)
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


async def _attempt(ticket, app, cfg, git, backlog, audit, budget, branch, stop_event=None) -> TicketReport:
    cost = 0.0
    last_changes: list[str] = []
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
            if _is_deliberate_halt(build.summary or build.raw):
                report = (build.summary or build.raw or "(no report)").strip()
                # Route the product/precondition blocker to the Product Manager (once per ticket). It
                # either makes the call — and the build resumes with that decision injected — or
                # escalates ONE recommendation to the Commander, in which case we park with a clear
                # comment and the run moves on to the next ticket.
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
                             + f"\n\n{proposal[:1400]}")
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
        gate = run_gate(app)
        audit.record("gate", ticket_id=ticket.id, iteration=iteration, passed=gate.passed)
        if not gate.passed:
            print("  gate · FAILED → sending fixes back to builder", flush=True)
            _bar(1, fail=1)
            last_changes = [f"Verification failed; fix these:\n{gate.report}"]
            continue
        if app.gate_commands:
            print("  gate · passed", flush=True)
        _bar(2, active=2)

        # 3) REVIEW (spec + quality) on the diff
        print("  review · reviewer reading the diff…", flush=True)
        diff = git.diff_against_base()
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
            _notify(cfg, f"❓ {ticket.id} — needs YOUR decision:\n{review.question or review.summary}\n"
                         f"Reply in Telegram:  {ticket.id}: <your decision>")
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
                print("  security · Provost gating the diff…", flush=True)
                sec_ok, sec_report = await provost_mod.gate(cfg, app, diff)
                if not sec_ok:
                    print("  security · Provost BLOCK (CRITICAL/HIGH) → PR for you, DEV untouched", flush=True)
                    _notify(cfg, f"🛡️ {ticket.id} — Provost blocked the merge (security).\n\n{sec_report[:1200]}")
                    audit.record("security_block", ticket_id=ticket.id, iteration=iteration)
                    security_block = sec_report
                else:
                    print("  security · Provost PASS ✓", flush=True)
            result = _land(ticket, app, cfg, git, backlog, audit, branch, iteration, cost, build,
                           review, security_block=security_block)
            if getattr(cfg, "scout_after_merge", False) and result.outcome == Outcome.MERGED:
                await _after_merge_scout(cfg, app, ticket, audit)
            return result

        last_changes = review.required_changes or review.spec_gaps or [
            q.detail for q in review.blocking_issues]
        audit.record("retry", ticket_id=ticket.id, iteration=iteration, required_changes=last_changes)
        _bar(2, fail=2)
        print("  ↻ changes requested → rebuilding", flush=True)

    # Exhausted iterations -> escalate to a human
    if not cfg.dry_run and not ticket.ephemeral:
        backlog.set_status(ticket, "Needs Human")
        backlog.add_comment(ticket, _escalation_comment(last_changes))
    print("  ✗ escalated — needs you (max passes reached without a clean review)", flush=True)
    _notify(cfg, f"🛑 {ticket.id} escalated — {cfg.max_iterations} passes without a clean review. "
                 f"Needs you.\n{ticket.summary}")
    audit.record("escalated", ticket_id=ticket.id, iterations=cfg.max_iterations)
    return TicketReport(ticket.id, Outcome.ESCALATED, cfg.max_iterations, cost, app.name, branch,
                        notes="max_iterations reached without a passing review")


def _land(ticket, app, cfg, git, backlog, audit, branch, iteration, cost, build, review,
          security_block=None) -> TicketReport:
    """Passed review. Validate the merge on a THROWAWAY trial branch so DEV is never
    touched until the single, final, validated merge."""
    git.commit_all(f"{ticket.id}: {ticket.summary}\n\n{build.summary}\n\nReviewed-by: autodev-reviewer")
    temp = f"{app.branch_prefix}/_trial"
    merge_msg = f"Merge {branch} into {app.base_branch} ({ticket.id})"
    print(f"  land · trial-merging into {app.base_branch} (throwaway branch — DEV untouched)…", flush=True)
    _bar(3, active=3)

    clean = bool(cfg.merge_to_dev) and git.trial_merge(branch, temp, merge_msg)
    green = clean and run_gate(app).passed          # gate runs on the trial branch, not on DEV
    if not clean:
        reason = "could not merge cleanly into dev" if cfg.merge_to_dev else "merge_to_dev disabled"
    elif not green:
        reason = "dev gate fails after merge"
    elif security_block:
        reason = "Provost blocked — CRITICAL/HIGH security finding"
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
        ctest = f" Test: {turl}." if turl else ""
        if not ticket.ephemeral:
            if cfg.mark_done_on_merge:
                backlog.set_status(ticket, "Done")
                backlog.add_comment(ticket, f"Merged to {app.base_branch} and marked Done.{ctest} {review.summary}")
            else:
                backlog.set_status(ticket, "QA")   # your QA column; you move it Done or back To Do
                backlog.add_comment(ticket, f"Merged to {app.base_branch}; moved to QA for your review.{ctest} {review.summary}")
        if ticket.ephemeral:
            done = ""   # ad-hoc task: no Jira ticket to move
        else:
            done = " · marked Done" if cfg.mark_done_on_merge else " · moved to QA"
        test_line = f"\n🔗 Test on {app.base_branch}: {turl}" if turl else ""
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
                    backlog.add_comment(ticket, f"⚠️ Sentinel rolled this back from {app.base_branch}. {snote[:900]}")
                print(f"  🛡️ {ticket.id}: Sentinel reverted the merge — needs you.", flush=True)
                return TicketReport(ticket.id, Outcome.ESCALATED, iteration, cost, app.name, branch,
                                    notes=f"sentinel reverted: {snote[:160]}")

        return TicketReport(ticket.id, Outcome.MERGED, iteration, cost, app.name, branch,
                            notes=f"merged to {app.base_branch}"
                            + (", Done" if cfg.mark_done_on_merge else ", awaiting QA"))

    # LIVE not validated -> DEV untouched; open a PR for you.
    git.abandon_trial(temp)
    git.push(branch)
    pr_url = git.open_pr(branch, f"{ticket.id}: {ticket.summary}", _pr_body(ticket, app, review)) \
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
        _clean, block = filing.present(report, app, "scout", do_file=not cfg.dry_run)
        Path(cfg.audit_path).with_name("scout-report.md").write_text(clean, encoding="utf-8")
        if audit is not None:
            audit.record("scout_smoke", ticket_id=ticket.id, app=app.name)
        _notify(cfg, f"🛰️ Scout smoke after {ticket.id} on {app.base_branch}:\n{clean[:700]}"
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


def _pr_body(ticket: Ticket, app: AppConfig, review) -> str:
    ac = "\n".join(f"- [x] {c}" for c in ticket.acceptance_criteria)
    return (f"Automated implementation of **{ticket.id}** for `{app.name}`, targeting "
            f"`{app.base_branch}`.\n\n{ticket.url or ''}\n\n"
            f"## Acceptance criteria\n{ac}\n\n## Reviewer summary\n{review.summary}\n")


def _escalation_comment(last_changes: list[str]) -> str:
    items = "\n".join(f"- {c}" for c in last_changes) or "- (no specific feedback captured)"
    return ("Automated pipeline could not complete this ticket within the iteration "
            f"limit. Outstanding items:\n{items}")
