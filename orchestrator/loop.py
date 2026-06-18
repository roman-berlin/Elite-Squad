"""The orchestration loop: build -> verify -> review -> land-on-dev / retry / escalate.

This is the "PM/general". It owns git, the Definition of Done, the iteration
bounds, the cost budget, every backlog transition, and the keep-dev-green merge.
`main` is never touched here — you merge that after QA in dev.
"""
from __future__ import annotations

import subprocess
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


_HALT_MARKERS = ("halt", "stop", "do not proceed", "precondition", "made no writes",
                 "no files were written", "no files written", "will not edit", "will not proceed",
                 "cannot proceed", "refuse", "abort", "needs your", "for the commander", "holding for")


def _is_deliberate_halt(text: str | None) -> bool:
    """A 'no changes' result is a DELIBERATE halt — the Builder verified a precondition and
    reported a blocker, not a failure — when its summary carries clear halt language. Requiring
    two markers avoids false positives on ordinary 'nothing to do' summaries."""
    t = (text or "").lower()
    return sum(1 for m in _HALT_MARKERS if m in t) >= 2


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


async def run(cfg: Config, worklist: list[tuple[AppConfig, Ticket]],
              audit: AuditLog, stop_event=None) -> list[TicketReport]:
    budget = Budget(cfg.max_cost_usd)
    reports: list[TicketReport] = []
    gits: dict[str, Git] = {}
    backlogs: dict[str, BacklogAdapter] = {}
    ensured: set[str] = set()

    for app, ticket in worklist:
        if stop_event is not None and stop_event.is_set():
            audit.record("run_stopped", reason="commander stop (before ticket)")
            print("  ■ stopped by Commander — remaining tickets skipped.", flush=True)
            break
        if budget.exceeded():
            audit.record("budget_stop", spent=budget.spent)
            break
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
            audit.record("ticket_exception", ticket_id=ticket.id, app=app.name, error=str(exc))
            _notify(cfg, f"❌ {ticket.id} — error: {str(exc)[:200]}")
            report = TicketReport(ticket.id, Outcome.ERRORED, 0, 0.0, app=app.name, notes=str(exc))
        reports.append(report)
    return reports


async def process_ticket(ticket, app, cfg, git, backlog, audit, budget, stop_event=None) -> TicketReport:
    branch = ticket.branch_name(app.branch_prefix)
    audit.record("ticket_start", ticket_id=ticket.id, app=app.name, branch=branch,
                 dry_run=cfg.dry_run, ephemeral=ticket.ephemeral)

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


async def _attempt(ticket, app, cfg, git, backlog, audit, budget, branch, stop_event=None) -> TicketReport:
    cost = 0.0
    last_changes: list[str] = []

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
        build = await builder_mod.build(req, app, cfg)
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
                decisions.add(cfg, ticket, app.name, report[:1500])   # so you can answer it
                audit.record("needs_human", ticket_id=ticket.id, iteration=iteration,
                             question=report[:1500], reason="builder halted — precondition/blocker")
                _notify(cfg, f"🛑 {ticket.id} — the Field Engineer HALTED before any write "
                             f"(precondition / blocker). Your call:\n\n{report[:1500]}")
                print(f"  🛑 {ticket.id}: Builder halted (precondition/blocker) — escalated to you.",
                      flush=True)
                return TicketReport(ticket.id, Outcome.ESCALATED, iteration, cost, app.name, branch,
                                    notes="Builder halted (precondition/blocker) — awaiting Commander")
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
            return _land(ticket, app, cfg, git, backlog, audit, branch, iteration, cost, build, review,
                         security_block=security_block)

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
        git.land_trial(temp)
        git.delete_local_branch(branch)   # merged into DEV (commits live there) -> retire the feature branch
        sync = git.sync_main_base() if getattr(cfg, "sync_base_after_merge", False) else ""
        print(f"  land · merged into {app.base_branch} ✓ (pushed) · feature branch retired", flush=True)
        if sync:
            print(f"  land · {sync}", flush=True)
        _bar(4)
        if not ticket.ephemeral:
            if cfg.mark_done_on_merge:
                backlog.set_status(ticket, "Done")
                backlog.add_comment(ticket, f"Merged to {app.base_branch} and marked Done. {review.summary}")
            else:
                backlog.set_status(ticket, "QA")   # your QA column; you move it Done or back To Do
                backlog.add_comment(ticket, f"Merged to {app.base_branch}; moved to QA for your review. {review.summary}")
        if ticket.ephemeral:
            done = ""   # ad-hoc task: no Jira ticket to move
        else:
            done = " · marked Done" if cfg.mark_done_on_merge else " · moved to QA"
        _notify(cfg, f"🧪 {ticket.id} ready for manual test on {app.base_branch}{done}\n{ticket.summary}")
        audit.record("merged", ticket_id=ticket.id, base=app.base_branch, done=cfg.mark_done_on_merge)
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
