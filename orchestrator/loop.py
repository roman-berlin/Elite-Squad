"""The orchestration loop: build -> verify -> review -> land-on-dev / retry / escalate.

This is the "PM/general". It owns git, the Definition of Done, the iteration
bounds, the cost budget, every backlog transition, and the keep-dev-green merge.
`main` is never touched here — you merge that after QA in dev.
"""
from __future__ import annotations

import asyncio
import threading
import copy
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
                       SpecArtifact, Ticket, TicketReport, Verdict)
from .gate import (base_gate_check, base_gate_timed_out, evict_base_green,
                   extract_failure_evidence, gate_fingerprint, publish_base_green,
                   run_deterministic_checks, run_gate, select_gate_groups)
from . import jira_adapter as jira_commenter
from . import cockpit_state
from . import run_logger
from . import usage
from .git_ops import Git, GitError, LandRaceError
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


def _git_commit_changelog(target: Path, base: str = "dev") -> None:
    """EU-335: best-effort commit (+ push) of the changelog append so the primary checkout doesn't
    re-dirty on every land — that dirtiness was blocking ~/bin/general-autopull.sh's clean-tree
    fast-forward guard (git status --porcelain never came back empty), leaving the cockpit serving
    stale code all night. Mirrors sync.py's ``_git`` helper: GIT_TERMINAL_PROMPT=0 so a push with no
    cached credentials fails fast instead of hanging, capture_output, a timeout, and every failure
    swallowed rather than raised. No-ops cleanly when ``target`` isn't inside a real git work tree
    (e.g. the eu41 tests' bare tmp paths) so those tests keep passing unchanged.

    ITERATION 2 — DIVERGENCE SAFETY (the whole reason iteration 1 was rejected): committing the
    append unconditionally on whatever the local tip happened to be created a LOCAL commit on a stale
    base, which diverged from origin/<base> and permanently blocked the very ff-only pull this fix
    exists to unblock (the land then failed to merge cleanly). So we now:
      1) only act on the ``base`` branch (never main / a feature branch / detached HEAD);
      2) first ``git fetch`` + ``git merge --ff-only origin/<base>`` — commit ONLY if the local tip is
         a clean fast-forward of origin (so our commit is a pure descendant, never a divergence). If
         the ff can't happen while an origin/<base> exists, we log + no-op and leave the append
         uncommitted rather than risk a divergent stray commit;
      3) commit with a git identity scoped to the single invocation (``-c user.name=...``) instead of
         writing persistent repo git config on every land."""
    try:
        cwd = target.parent

        def _git(*args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )

        probe = _git("rev-parse", "--is-inside-work-tree")
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            return  # not a repo (arbitrary tmp path, as the eu41 tests use) -> nothing to commit

        # (1) Branch guard: only ever commit/push on the base branch. On main, a feature branch, or a
        # detached HEAD we no-op (leaving the append for the normal base-branch land to pick up).
        # Case-INSENSITIVE (2026-07-20, caught live on the AUTO-59 land): automatixy's configured
        # base is 'DEV' while git reports 'dev' on this case-insensitive FS — the exact-match guard
        # skipped the changelog on every one of that repo's lands.
        branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if branch.lower() != str(base).lower():
            print(f"  changelog commit skipped: on '{branch}', not base '{base}'", flush=True)
            return
        # P0 (2026-07-21 production audit): from here on, every origin operation uses the branch
        # GIT reports — never the caller's `base`, which on a cross-repo land is the PRODUCT app's
        # branch name ('DEV'): in this repo origin/DEV doesn't exist, so the ff sync silently
        # no-oped and `push origin DEV` failed — leaving the changelog commit local-only, arming
        # the exact EU-335 stale-cockpit divergence this function exists to prevent.
        base = branch

        # (2) Sync-before-commit so our commit is a clean descendant of origin, never a divergence.
        has_remote_base = _git("rev-parse", "--verify", "--quiet", f"origin/{base}").returncode == 0
        fetched = _git("fetch", "origin", base, timeout=60)
        if fetched.returncode == 0:
            has_remote_base = _git("rev-parse", "--verify", "--quiet", f"origin/{base}").returncode == 0
        if has_remote_base:
            ff = _git("merge", "--ff-only", f"origin/{base}")
            if ff.returncode != 0:
                # Local has diverged from origin/<base>; committing here would deepen the divergence
                # and block autopull's ff. Leave the append uncommitted — safe, recoverable.
                print(f"  changelog commit skipped: cannot ff onto origin/{base} "
                      f"(divergence) — {ff.stderr.strip()}", flush=True)
                return
        # else: no origin/<base> at all (offline / no remote configured) — nothing to diverge from,
        # so a local commit is safe and keeps the tree clean.

        _git("add", "--", str(target))
        if _git("diff", "--cached", "--quiet").returncode == 0:
            return  # nothing staged (e.g. path gitignored, or already committed by the ff) -> done
        commit = _git(
            "-c", "user.name=Elite Unit", "-c", "user.email=unit@localhost",
            "commit", "-m", f"chore: changelog append ({target.name})",
        )
        if commit.returncode != 0:
            print(f"  changelog commit skipped: {commit.stderr.strip()}", flush=True)
            return
        push = _git("push", "origin", base, timeout=60)
        if push.returncode != 0:
            print(f"  changelog push skipped: {push.stderr.strip()}", flush=True)
    except Exception as exc:  # noqa: BLE001 - best-effort, must never break the land
        print(f"  changelog commit skipped: {exc}", flush=True)


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
    both read the same stale ``old`` list and clobber each other's append (EU-276).

    EU-335: the append is then best-effort git-committed (and pushed) in place, so the primary
    checkout's tree goes back to clean immediately rather than re-dirtying and re-blocking the
    autopull ff on the very next land."""
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
        _git_commit_changelog(target, base=app.base_branch or "dev")
        return True
    except Exception as exc:  # noqa: BLE001 - release-hygiene logging must never break the run
        print(f"  changelog skipped: {exc}", flush=True)
        return False


def _gate_retry_feedback(cfg, app: AppConfig, ticket: Ticket, kind: str, result) -> str:
    """EU-342 (tee-on-failure): save the COMPLETE gate output to a run-log file and hand the Builder
    signal-sliced evidence + that file's path, instead of the raw last-4000-char tail. The Builder
    then reads the real failure from the file rather than re-running the suite inside its own turns
    to rediscover it — the single biggest retry-token contributor (EU-38) and a driver of the
    turn-exhaustion class (EU-248). Falls back to the truncated report if the full output or the log
    write isn't available; never raises."""
    full = (getattr(result, "full_report", "") or getattr(result, "report", "") or "")
    evidence = extract_failure_evidence(full)
    ref = ""
    try:
        from . import run_logger
        if full.strip() and not (cfg and getattr(cfg, "dry_run", False)):
            path = run_logger.write_note_log(cfg, app.name, ticket.id,
                                             f"=== {kind} full output (EU-342) ===\n{full}")
            ref = (f"\n\nFull {kind.lower()} output saved to: {path}\n"
                   "(read that file for the COMPLETE output — do not re-run the suite to rediscover it).")
    except Exception:  # noqa: BLE001 — the tee is best-effort; the evidence above always ships
        ref = ""
    return f"{kind} failed; fix these:\n{evidence}{ref}"


def _gate_execution_evidence(app: AppConfig, gate, changed_paths: list[str] | None = None) -> str:
    """EU-265: machine-sourced execution evidence for the Reviewer's EU-268 execution-AC gate.

    By the time the loop calls ``reviewer.review()``, ``run_gate`` has ALREADY run this pass's
    verification suite green (a red gate bounces back to the Builder and never reaches review) —
    yet that proof used to stay loop-side, so `_enforce_execution_gate` saw only the Builder's
    prose and forced FAIL on every "all tests pass"-shaped AC, looping the ticket to max-passes/
    escalation (triage 2026-07-17: the honesty gate's spec-mandated conservative '' default).
    This renders the loop's own subprocess result — the gate verdict + the commands that ran — as
    a short trusted string the reviewer accepts as first-class evidence.

    Returns '' (NO evidence) unless a REAL gate ran and passed:
      • a red / absent gate → '' (never reached in the loop, but the helper is defensive);
      • a trivially-green no-op gate — ``run_gate``'s "(no gate commands configured)" /
        ``run_commands``'s "(no commands configured)" → '' — an app with no gate must NEVER get
        free execution evidence, or the AUTO-109 hole EU-268 closed re-opens;
      • no resolvable commands → '' (same reasoning, stub-shaped passes included).
    The command list mirrors run_gate's selection (EU-19): the touched components' per-app groups
    when a monorepo mapping matched, else the repo-wide gate_commands."""
    if gate is None or not getattr(gate, "passed", False):
        return ""
    report = (getattr(gate, "report", "") or "").strip()
    if not report or report.startswith("(no "):
        return ""
    groups = select_gate_groups(app, changed_paths or [])
    cmds = [c for _, cs in groups for c in cs] if groups else list(app.gate_commands or [])
    if not cmds:
        return ""
    return "\n".join([f"verification gate PASSED — {report}", "commands run:"]
                     + [f"$ {c}" for c in cmds])


def _maybe_close_epic(backlog, ticket: Ticket, audit: AuditLog) -> None:
    """EU-374 (EU-301 step 5): close the parent Epic when its final VERIFY child lands.

    scrum.split decomposes an oversized ticket into an Epic + Task children (linked via `parent`)
    with a mandatory last "Verify & close" child carrying the parent's AC — but nothing ever
    transitioned the Epic itself, so auto-split Epics accumulated open forever. Called from the
    land path right after the child's own QA/Done transition; the Epic closes only when:
      • the landed child IS the verify child (scrum.is_verify_child — it runs LAST by design), and
      • it has an Epic parent (jira.parent_epic_key — non-Epic parents never count), and
      • the child snapshot actually CONTAINS the landed child (an empty/partial search result —
        auth-blind 200, board hiccup — must never vacuously "prove" the siblings done), and
      • every OTHER child is already Done/QA/Closed (raw status name or statusCategory=done; the
        landed child itself is exempt — its just-written QA transition may not be visible in the
        search snapshot yet).
    Best-effort by contract: any error leaves the Epic open for a human and NEVER un-lands the
    merge. No-op for backends without the Epic helpers (getattr-guarded)."""
    try:
        if getattr(ticket, "ephemeral", False):
            return
        from . import scrum as _scrum
        if not _scrum.is_verify_child(ticket.summary, ticket.description):
            return
        get_parent = getattr(backlog, "parent_epic_key", None)
        get_children = getattr(backlog, "epic_children", None)
        close = getattr(backlog, "close_ticket", None)
        if not (get_parent and get_children and close):
            return
        epic_key = get_parent(ticket.key)
        if not epic_key:
            return
        children = get_children(epic_key) or []
        if not any((c.get("key") or "") == ticket.key for c in children):
            return
        _done = ("qa", "done", "closed")
        open_sibs = [c.get("key") for c in children
                     if (c.get("key") or "") != ticket.key
                     and not ((c.get("status") or "").strip().lower() in _done
                              or (c.get("status_category") or "").strip().lower() == "done")]
        if open_sibs:
            print(f"  land · Epic {epic_key} stays open — sibling(s) not Done/QA yet: "
                  + ", ".join(str(k) for k in open_sibs), flush=True)
            return
        kk = ", ".join((c.get("key") or "?") for c in children)
        ok = close(epic_key,
                   comment=(f"✅ All children landed ({kk}) and the verify child {ticket.id} just "
                            "merged — closing this Epic (EU-374, auto-close on verify-child land)."),
                   audit=audit)
        if ok:
            audit.record("epic_autoclosed", ticket_id=ticket.id, epic=epic_key,
                         children=[(c.get("key") or "?") for c in children])
            print(f"  land · Epic {epic_key} closed — all children Done/QA "
                  f"(verify child {ticket.id} landed).", flush=True)
    except Exception as exc:  # noqa: BLE001 — a board hiccup leaves the Epic open, never breaks a land
        print(f"  land · epic auto-close skipped ({exc})", flush=True)


def _already_landed(app: AppConfig, ticket_id: str) -> str | None:
    """EU-225: deterministic, read-only evidence that ``ticket_id`` already SHIPPED — its id in the
    Technical Writer's changelog (written only on a successful live land) AND a commit on
    ``origin/<base>`` whose message references it. BOTH must hit (double-keyed) so a stale changelog
    line or an unrelated "relates to X" commit mention alone can't trigger a close. Returns a short
    evidence string (commit sha) or None. Best-effort: any error → None (fall back to building)."""
    tid = (ticket_id or "").strip()
    if not tid:
        return None
    # Changelog hit — match the `· <TID> ·` field, not a loose substring (so EU-19 ≠ EU-191).
    try:
        cl = _changelog_path().read_text()
    except Exception:  # noqa: BLE001
        cl = ""
    changelog_hit = any(f"· {tid} ·" in ln for ln in cl.splitlines() if ln.startswith("- "))
    # P0 (2026-07-21 production audit): a crash in the seconds between the dev push and the
    # changelog write leaves the changelog line UNWRITTEN forever — exactly the case this valve
    # exists for. The durable `land_pushed` audit event (written the moment the push succeeds)
    # is the equivalent first key; git-log corroboration below stays mandatory either way.
    if not changelog_hit:
        try:
            with open("./state/audit.jsonl", encoding="utf-8") as _fh:
                for _ln in _fh:
                    if '"land_pushed"' in _ln and f'"{tid}"' in _ln:
                        changelog_hit = True
                        break
        except OSError:
            pass
    if not changelog_hit:
        return None
    # Git-log corroboration on origin/<base> in the app repo.
    try:
        base = f"origin/{app.base_branch}"
        r = subprocess.run(
            ["git", "log", base, "--grep", tid, "--fixed-strings", "--pretty=%h", "-n", "1"],
            cwd=os.path.expanduser(app.repo_path), capture_output=True, text=True, timeout=30)
        sha = (r.stdout or "").strip().splitlines()[0] if (r.returncode == 0 and r.stdout.strip()) else None
    except Exception:  # noqa: BLE001
        sha = None
    if sha:
        return f"already landed — commit {sha} on origin/{app.base_branch} + Technical Writer changelog entry"
    return None


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

# Report notes for the two BASE-LEVEL verdicts. Both are emitted ONLY from the constants below
# (never inline literals) because _run_inner and autopilot key their base-level halts on these
# prefixes — an inline rewording would silently disarm both halts and reopen the 2026-07-15
# needs_human massacre class. _BASE_INFRA_NOTES must additionally contain "timed out" so
# infra_classify.classify tags it infra (EU-228: no error strike).
_RED_BASE_NOTES = "red base — gate fails on the clean base tree"
_BASE_INFRA_NOTES = "base gate timed out on the clean base tree — environment/load infra, not a code red"
_BASE_LEVEL_PREFIXES = ("red base", "base gate timed out")


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
                     f"{kk} and closed the parent. The squad takes the fragments next.")
        print(f"  🧩 {ticket.id}: too big → Scrum Master split into {kk}; parent closed.", flush=True)
        return TicketReport(ticket.id, Outcome.REQUEUED, iterations, cost, app.name, branch,
                            notes=f"too big — Scrum Master split into {kk}")
    # 2026-07-19 audit: the refusal reason (depth cap, splitter decline, an exception) used to
    # vanish — the Commander got a bare "turn-limit" park telling him to split by hand, with no
    # mention that auto-split had already run this lineage to its ceiling. Record WHY it refused.
    audit.record("scrum_split_failed", ticket_id=ticket.id, reason=split_reason,
                 error=str(sp.get("error") or "splitter declined")[:400])
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
        # 2026-07-19 Commander order (senior team, not juniors): a depth-capped/declined split does
        # NOT go straight to the Commander — first re-queue ONCE with a raised turn budget.
        # builder.effort_plan sees the persisted retry marker and bumps the effort one level, which
        # scales turns_for (e.g. high 96 → xhigh 144), so the retry genuinely gets more room rather
        # than repeating the same blow-out. mark_turn_retry persisting the counter is the loop
        # guard: if it can't be persisted the retry is NOT taken (fail-closed — park instead of
        # risking an endless requeue); a second blow-out falls through to the park below, and the
        # still-set marker keeps the boost for any Commander-ordered re-run.
        if (not ticket.ephemeral and not getattr(cfg, "dry_run", False)
                and builder_mod.turn_retry_count(cfg, ticket.id) == 0
                and builder_mod.mark_turn_retry(cfg, ticket.id)):
            audit.record("turn_limit_retry", ticket_id=ticket.id)
            _notify(cfg, f"⏳ {ticket.id} ran out of turns and can't be split smaller — retrying once "
                         "with a raised turn budget before bothering you.")
            print(f"  ⏳ {ticket.id}: turn-limit at the split depth cap — requeued once with a "
                  "bigger turn budget.", flush=True)
            return TicketReport(ticket.id, Outcome.REQUEUED, 0, 0.0, app.name,
                                notes="turn-limit — requeued once with a raised turn budget")
        # No split possible and the retry is spent → escalate to the Commander, telling him the
        # TRUTH: auto-split already ran this lineage to its ceiling and a boosted retry also blew
        # out — "split it yourself" would be asking him to do what the system just declined.
        note = (f"{ticket.id} ran out of turns even after a boosted retry, and the Scrum Master "
                "declined to split it further (depth cap — this lineage was already auto-split as "
                "far as it goes). Re-scope/simplify the ticket, or raise the turn budget "
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


def _worktree_path(app: AppConfig, cfg: Config, slot: int = 0) -> str:
    """Where the CTO keeps this app's private worktree (a sibling of the repo).

    EU-380: slot 0 is the historic per-app path (unchanged for the serial drain and every existing
    caller); a concurrent drain gives each extra builder its own `<app>-s<N>` worktree so two
    tickets of one app never share a tree or a git index. git_ops.reap_stale_worktrees derives its
    protected set from this function across range(max_concurrent_builders), so slot paths are
    canonical — the reaper must never eat an idle slot (the EU-334 self-reap class)."""
    name = app.name if slot <= 0 else f"{app.name}-s{slot}"
    if getattr(cfg, "worktree_dir", None):
        return str(Path(cfg.worktree_dir).expanduser() / name)
    repo = Path(app.repo_path).expanduser().resolve()
    return str(repo.parent / ".general-worktrees" / name)


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


def _make_git(cfg: Config, app: AppConfig, slot: int = 0) -> Git:
    """Build the git custodian for an app. With use_worktree on, the CTO gets a
    dedicated linked worktree (based on origin/<base>) so it never fights the user's
    manual checkout. Falls back to in-tree if isolation can't engage (e.g. no origin).
    EU-380: ``slot`` selects the builder slot's worktree; 0 = the historic path."""
    if getattr(cfg, "use_worktree", False):
        wt = _worktree_path(app, cfg, slot)
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
            # EU-367: never SILENTLY downgrade to editing the main checkout. Two cases:
            #  · the orchestrator's OWN repo — building the live unit in-tree is the EU-174
            #    isolation-leak (a self-dev ticket editing the running code). REFUSE: raise so the
            #    ticket parks for a human rather than clobbering the main checkout.
            #  · a product repo — keep the in-tree fallback (some setups legitimately have no origin),
            #    but make it LOUD (it used to be a single silent log line) so an unexpected downgrade
            #    is visible on Telegram, not a surprise discovered later.
            try:
                is_self_repo = Path(app.repo_path).resolve() == Path(__file__).resolve().parent.parent
            except Exception:  # noqa: BLE001 — a bad path just means "treat as product repo"
                is_self_repo = False
            if is_self_repo:
                _notify(cfg, f"⛔ {app.name}: worktree isolation unavailable ({first}) — refusing to "
                             "build the unit's OWN code in the main checkout (EU-174 self-edit guard). "
                             "Ticket parked; ensure origin/<base> resolves, then re-run.")
                raise GitError(
                    f"worktree isolation required for the orchestrator's own repo but unavailable "
                    f"({first}); refusing to build in-tree — EU-174 self-edit guard") from exc
            print(f"  · ⚠️ worktree isolation OFF for {app.name} ({first}) — working IN-TREE "
                  "(the build edits the main checkout).", flush=True)
            _notify(cfg, f"⚠️ {app.name}: worktree isolation unavailable ({first}) — building in-tree "
                         "(edits the main checkout). Check that origin/<base> resolves.")
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
    # EU-366: bound both subprocesses — a wedged package registry on `bun install` (or a git that
    # decides to prompt) must not freeze the drain mid-ticket. A timeout is treated exactly like a
    # non-zero exit here: a logged dep-isolation warning, then the build proceeds (may fail later on
    # its own gate) — never a crash. Override with GENERAL_SETUP_TIMEOUT (seconds).
    _setup_timeout = int(os.environ.get("GENERAL_SETUP_TIMEOUT", "600") or 600)
    # 1) restore DEV's lockfile into the worktree (undoes any prior-ticket drift).
    try:
        restored = _loop_module.subprocess.run(
            ["git", "checkout", base_ref, "--", "bun.lock"],
            cwd=str(workdir), capture_output=True, text=True, timeout=_setup_timeout,
        )
        if restored.returncode != 0:
            why = (restored.stderr.strip().splitlines() or ["no bun.lock at base"])[0]
            print(f"  · dep isolation: bun.lock not restored from {base_ref} ({why})", flush=True)
    except _loop_module.subprocess.SubprocessError as exc:
        print(f"  · dep isolation: bun.lock restore did not complete ({exc}) — continuing", flush=True)
    # 2) reinstall frozen so the install can't drift off the pinned lock.
    print("  · dep isolation — bun install --frozen-lockfile", flush=True)
    try:
        proc = _loop_module.subprocess.run(
            ["bun", "install", "--frozen-lockfile"],
            cwd=str(workdir), capture_output=True, text=True, timeout=_setup_timeout,
        )
        if proc.returncode != 0:
            tail = (proc.stderr.strip().splitlines() or ["unknown"])[-1]
            print(f"  · dep isolation: frozen install reported a problem ({tail})", flush=True)
    except _loop_module.subprocess.SubprocessError as exc:
        print(f"  · dep isolation: frozen install timed out/failed ({exc}) — continuing", flush=True)


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
              audit: AuditLog, stop_event=None, stop_between_tickets=None) -> list[TicketReport]:
    # EU-356: ``stop_between_tickets`` is the TICKET-BOUNDARY-ONLY stop channel, distinct from
    # ``stop_event`` (which also arms the pre-build and pre-merge aborts inside _attempt). The
    # autopilot drain passes its stop Event here and NOT as stop_event, because the drain's contract
    # is "let the in-flight ticket land on DEV, then stand down" — arming the mid-ticket checkpoints
    # would kill a build in flight or abandon a finished build before its merge. This channel is what
    # was MISSING on 2026-07-15: the drain's event armed only autopilot's own per-CYCLE check, and a
    # single cycle ran 3h13m because EU-201 fragment injection kept extending the live worklist
    # in place (EU-321 → EU-350..353 → EU-334 → EU-339), so a 22:50 stop wasn't honoured until 01:53.
    # With this armed, that stop lands at the next ticket boundary (23:12) instead.
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
    # 2026-07-19 hybrid mode: when armed AND a secondary exists (and differs from the main),
    # pin it run-scoped — the SDK seam then routes build-tag calls to the secondary while the
    # planner/architect/reviewer stay on the main model. Best-effort: never blocks a run.
    _hy_token = None
    try:
        from . import backend_pref as _bp
        _hy_sec = _bp.get_secondary(cfg) if _bp.get_hybrid(cfg) else None
        if _hy_sec and _hy_sec != getattr(cfg, "model_backend", None):
            # 2026-07-21 (Commander): SYMMETRIC limit fallback. The main→secondary direction has
            # existed since EU-118 (plan limit → secondary absorbs); this is the reverse — when the
            # SECONDARY (GLM) is out of quota, hybrid stands down for the run and builds go back to
            # the Main model, loudly, instead of dispatching against an exhausted endpoint.
            _sec_over = False
            if backends.normalize(_hy_sec) == backends.GLM:
                try:
                    _gst = usage.dual_provider_budget_status(cfg).get("glm", {})
                    # OVER only (2026-07-21): "bad" is a >=95% threshold on a LOCAL guess —
                    # it false-fired the fallback at Z.ai-real 21%. Re-route on hard
                    # evidence; a bad-threshold estimate is a Telegram warning at most.
                    _sec_over = bool(_gst.get("over"))
                except Exception:  # noqa: BLE001 - an unreadable ledger must not change routing
                    _sec_over = False
            if _sec_over:
                audit.record("hybrid_fallback_main", reason="secondary GLM quota over/near cap")
                _notify(cfg, "⚠️ Secondary (GLM) is out of quota — hybrid stands down; builds run "
                             "on the Main model for this run (pricier, but nothing stalls).")
            else:
                _hy_token = backends.set_hybrid(_hy_sec)
                audit.record("hybrid_mode", builder_backend=_hy_sec,
                             main_backend=getattr(cfg, "model_backend", backends.NATIVE))
    except Exception:  # noqa: BLE001
        _hy_token = None
    try:
        return await _run_inner(cfg, worklist, audit, stop_event, stop_between_tickets)
    finally:
        if _hy_token is not None:
            backends.reset_hybrid(_hy_token)
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


def _glm_budget_preflight_block(cfg: Config, audit: AuditLog) -> bool:
    """EU-222: fail the run closed, before any worklist processing, if GLM is the ACTIVE backend
    and its quota is exhausted/near-exhausted. max_cost_usd already counts GLM spend (the SDK's
    real, non-zero total_cost_usd for glm-4.6 feeds Budget.add same as Claude) — it was only ever
    inert because the default max_cost_usd=0.0 disables the cap. This is a separate GLM
    quota-availability gate, not a parallel price table. Returns True (and records + notifies) if
    the run should stop; False if the gate is a no-op (GLM healthy, or the configured backend isn't
    GLM at all).

    The gate keys off cfg.model_backend — the backend dispatch ACTUALLY uses — NOT
    status['active_provider']. active_provider is a global-pref source that can diverge from
    cfg.model_backend; deferring to it (the old `active_provider != 'glm'` early-return) opened a
    fail-closed bypass where GLM would dispatch against an exhausted quota while the pref still read
    'claude'. So: block iff the configured backend is GLM and GLM is over/near cap."""
    # P0 (2026-07-21 production audit, refined same day): in HYBRID mode every BUILD dispatches on
    # the GLM secondary, which used to be ungoverned here. Governance is now split by whether a
    # fallback exists: hybrid-GLM-over is handled at the ARM SITE (hybrid stands down, builds run
    # on the Main — see the symmetric-fallback block in run_worklist), so the run proceeds; this
    # gate BLOCKS only when the MAIN backend itself is GLM — there is nothing left to fall back to.
    if backends.normalize(getattr(cfg, "model_backend", "opus")) != backends.GLM:
        return False
    status = usage.dual_provider_budget_status(cfg)
    glm = status.get("glm", {})
    if not glm.get("over"):   # OVER only (2026-07-21) — "bad" is a local-guess threshold,
        return False          # alert-worthy, never a dispatch blocker

    msg = (f"GLM budget pre-flight: run blocked before dispatch — GLM quota is "
           f"{'over cap' if glm.get('over') else 'near cap (>=95%)'} ({glm})")
    audit.record("glm_budget_preflight_block", glm_status=glm)
    _notify(cfg, msg)
    return True


async def _run_inner(cfg: Config, worklist: list[tuple[AppConfig, Ticket]],
                     audit: AuditLog, stop_event=None, stop_between_tickets=None) -> list[TicketReport]:
    """Dispatch: the historic serial drain (default), or the EU-380 concurrent drain when
    max_concurrent_builders > 1. The serial body is untouched — flipping the knob back to 1
    restores today's exact behaviour.

    EU-222: before either drain touches the worklist, a fail-closed GLM budget pre-flight runs —
    if GLM is the active backend and its quota is exhausted, the run stops here with a clear
    audit event + notify instead of silently dispatching against a blind dollar cap."""
    if _glm_budget_preflight_block(cfg, audit):
        return []
    n = max(1, int(getattr(cfg, "max_concurrent_builders", 1) or 1))
    if n <= 1:
        return await _run_inner_serial(cfg, worklist, audit, stop_event, stop_between_tickets)
    return await _run_inner_concurrent(cfg, worklist, audit, stop_event, stop_between_tickets, n)


def _fragment_keys_from_report(report: TicketReport) -> list[str]:
    """Fragment keys from a scrum-split REQUEUE, or []. Extracted from the serial drain's EU-201
    injection block so both drains parse the one notes format ('… split into AUTO-101, AUTO-102')."""
    if not (report.outcome == Outcome.REQUEUED and report.notes
            and "split into" in report.notes.lower()):
        return []
    m = re.search(r"split into ([^.\n]+)", report.notes)
    if not m:
        return []
    return [k.strip() for k in m.group(1).strip().split(",") if k.strip()]


def _split_lineage(ticket: Ticket) -> str | None:
    """The auto-split parent key of a fragment, or None. EU-380 prereq 3: two fragments of ONE
    split are written in dependency order and must never build concurrently — the picker would
    otherwise preferentially co-schedule exactly the tickets that must be serial (they're filed
    together with adjacent Rank)."""
    m = re.search(r"Auto-split from ([A-Z][A-Z0-9]+-\d+)", ticket.description or "")
    return m.group(1) if m else None


# 2026-07-20 (Commander order — "develop 2 different tickets so there will not be conflicts"):
# conflict-aware co-scheduling for the concurrent drain. Two tickets may build in parallel ONLY
# when their predicted code footprints are disjoint; overlapping or unpredictable footprints
# serialize. The footprint comes from the ticket TEXT — this unit's tickets consistently cite
# their paths ("Where: apps/x/src/components/calendar/…"), which is exactly the signal a senior
# lead uses when handing two tasks to two developers.
_PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_.@-]+(?:/[A-Za-z0-9_.@\[\]{}*-]+)+")


def _ticket_footprint(ticket: Ticket) -> frozenset[str]:
    """The set of DIRECTORY paths this ticket's text names (trailing filename stripped, lowered).
    Empty = the ticket names no paths, so its footprint is unpredictable."""
    text = " ".join([ticket.summary or "", ticket.description or "",
                     " ".join(ticket.acceptance_criteria or [])])
    dirs: set[str] = set()
    for m in _PATH_TOKEN_RE.finditer(text):
        parts = [p for p in m.group(0).strip("`'\".,()").split("/") if p and p not in (".", "..")]
        if parts and "." in parts[-1]:      # drop a trailing FILE so dirs compare with dirs
            parts = parts[:-1]
        if len(parts) >= 2:                 # a single bare segment ('src') is too weak a signal
            dirs.add("/".join(parts).lower())
    return frozenset(dirs)


def _tickets_conflict(a: Ticket, b: Ticket) -> bool:
    """True when the two tickets must NOT build concurrently. Tickets of DIFFERENT apps live in
    different repos and physically cannot collide — always parallel. Within one app: either
    footprint unpredictable (no paths cited — conservative), or any cited directory of one
    contains/equals one of the other's (apps/x/src/components/calendar vs …/calendar → conflict;
    …/calendar vs …/leads → parallel; a whole-app-root citation conflicts with everything in it)."""
    if (a.app or "") and (b.app or "") and a.app != b.app:
        return False
    fa, fb = _ticket_footprint(a), _ticket_footprint(b)
    if not fa or not fb:
        return True
    return any(p == q or p.startswith(q + "/") or q.startswith(p + "/")
               for p in fa for q in fb)


# P0 (2026-07-21 production audit): under the CONCURRENT drain, a long SYNC phase (gate run,
# the whole land incl. its bounded CI poll) executed inline on the shared event loop froze the
# SIBLING slot's agent stream — its wall-clock budget kept burning, so multi-million-token passes
# died as spurious "wall-clock timeout" failures attributed to the WRONG ticket. Off-load such
# phases to a worker thread when (and only when) more than one builder slot is armed; the serial
# drain keeps its historic inline path byte-identical. Lands stay strictly serialized via an
# EXPLICIT lock (previously an accident of the single event loop).
_LAND_SERIAL_LOCK = threading.Lock()


async def _off_loop(cfg, fn, *args, **kwargs):
    """Run ``fn`` inline (serial drain) or in a worker thread (concurrent drain)."""
    if int(getattr(cfg, "max_concurrent_builders", 1) or 1) > 1:
        import asyncio as _aio
        return await _aio.to_thread(fn, *args, **kwargs)
    return fn(*args, **kwargs)


def _host_free_gb() -> float:
    """Available host memory in GB (free+inactive+speculative via vm_stat on macOS,
    MemAvailable on Linux). inf when unmeasurable — the guard then never blocks.
    2026-07-17 incident this exists for: two concurrent builder contexts exhausted the 24GB box
    (~72MB free), macOS SIGTERM-killed BOTH slots at once (exit-143 x2) — losing two builds is
    strictly worse than briefly running serial."""
    import subprocess as _sp
    try:
        if os.path.exists("/proc/meminfo"):
            for line in open("/proc/meminfo", encoding="utf-8"):
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1e6      # kB → GB
            return float("inf")
        out = _sp.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
        page = 16384 if "page size of 16384" in out else 4096
        pages = 0
        for line in out.splitlines():
            if line.startswith(("Pages free:", "Pages inactive:", "Pages speculative:")):
                pages += int(line.split()[-1].rstrip("."))
        return pages * page / 1e9
    except Exception:  # noqa: BLE001 — an unmeasurable host must not stall the drain
        return float("inf")


async def _run_inner_concurrent(cfg: Config, worklist: list[tuple[AppConfig, Ticket]],
                                audit: AuditLog, stop_event, stop_between_tickets,
                                n: int) -> list[TicketReport]:
    """EU-380: N builder slots over a shared queue. What makes this safe:

    - Each slot has its OWN worktree (loop._worktree_path slot suffix) and its own AppConfig COPY —
      _make_git mutates app.workdir, so a shared AppConfig would point two builders at one tree.
    - Lands stay effectively serialized: _land is synchronous, so on this event loop it can never
      interleave with another slot's land; the overlap concurrency buys lives in the awaited LLM
      passes (measured 59-65% of ticket wall clock). A cross-PROCESS race is still possible and is
      what EU-379's in-process re-trial absorbs at re-merge+re-gate cost, not rebuild cost.
    - Split siblings never co-run (_split_lineage mutex), and a fragment chain injects at the FRONT
      of the queue to preserve EU-201's dependency order.
    - A base-level verdict halts ITS app's further picks (same contract as the serial drain).
    - Per-ticket run logs: the concurrent path writes the EU-253 note file instead of claiming the
      per-app log handle — two slots registering one key would clobber each other; stdout is still
      attributed per-slot via the EU-272 ContextVar."""
    budget = Budget(cfg.max_cost_usd)
    reports: list[TicketReport] = []
    queue: deque[tuple[AppConfig, Ticket]] = deque(worklist)
    gits: dict[tuple[str, int], Git] = {}
    backlogs: dict[str, BacklogAdapter] = {}
    ensured: set[tuple[str, int]] = set()
    base_halted: set[str] = set()
    in_flight_lineage: set[str] = set()
    in_flight_ids: set[str] = set()
    in_flight_tickets: dict[str, Ticket] = {}   # id → Ticket, for the footprint-conflict guard
    conflict_audited: set[tuple[str, str]] = set()
    locks = ExitStack()
    slot_busy: set[tuple[str, int]] = set()

    def _stopped() -> bool:
        return ((stop_event is not None and stop_event.is_set())
                or (stop_between_tickets is not None and stop_between_tickets.is_set()))

    def _pick() -> tuple[AppConfig, Ticket] | None:
        """Next eligible ticket, honouring the app-halt and sibling-mutex sets. Rotates blocked
        items to the tail; returns None when nothing is currently eligible."""
        for _ in range(len(queue)):
            app, ticket = queue.popleft()
            if app.name in base_halted:
                reports.append(TicketReport(ticket.id, Outcome.SKIPPED, 0, 0.0, app.name,
                                            notes="skipped — base-level verdict for this app"))
                continue
            lineage = _split_lineage(ticket)
            if (lineage and (lineage in in_flight_lineage or lineage in in_flight_ids)) or \
                    (ticket.id in in_flight_lineage):
                queue.append((app, ticket))   # a sibling (or its parent) is mid-build — defer
                continue
            # 2026-07-20: footprint-conflict guard — never co-build two tickets whose cited
            # paths overlap (or whose footprint is unpredictable). Deferred to the tail; the
            # audit event fires once per (ticket, in-flight) pair, not on every poll.
            clash = next((t for t in in_flight_tickets.values()
                          if _tickets_conflict(ticket, t)), None)
            if clash is not None:
                if (ticket.id, clash.id) not in conflict_audited:
                    conflict_audited.add((ticket.id, clash.id))
                    audit.record("conflict_deferred", ticket_id=ticket.id, against=clash.id)
                    print(f"  ⇄ {ticket.id}: overlaps in-flight {clash.id} — deferred so the "
                          "two builders never touch the same code.", flush=True)
                queue.append((app, ticket))
                continue
            return app, ticket
        return None

    _mem_floor = float(getattr(cfg, "concurrent_min_free_gb", 6.0) or 0)
    _mem_deferred_logged: set[int] = set()

    async def _worker(slot: int) -> None:
        while not _stopped() and not budget.exceeded():
            # Memory floor (2026-07-17 OOM revert lesson): only slot 0 works unconditionally.
            # Extra slots stand down while host memory is below the floor — the drain degrades
            # to serial under pressure instead of macOS SIGTERM-killing every build at once.
            if slot > 0 and _mem_floor > 0 and _host_free_gb() < _mem_floor:
                if slot not in _mem_deferred_logged:
                    _mem_deferred_logged.add(slot)
                    audit.record("memory_deferred", slot=slot, floor_gb=_mem_floor)
                    print(f"  ⏸ slot {slot}: host memory below {_mem_floor}GB — running serial "
                          "until pressure clears.", flush=True)
                await asyncio.sleep(30)
                continue
            _mem_deferred_logged.discard(slot)
            picked = _pick()
            if picked is None:
                if not queue or not in_flight_ids:
                    return                      # drained, or only ineligible work and nobody active
                await asyncio.sleep(2)          # siblings in flight — wait for a completion
                continue
            app, ticket = picked
            lineage = _split_lineage(ticket)
            in_flight_ids.add(ticket.id)
            in_flight_tickets[ticket.id] = ticket
            if lineage:
                in_flight_lineage.add(lineage)
            try:
                key = (app.name, slot)
                if key not in gits and getattr(cfg, "use_worktree", False):
                    try:
                        locks.enter_context(_worktree_lock(_worktree_path(app, cfg, slot)))
                    except WorktreeBusy:
                        slot_busy.add(key)
                if key in slot_busy:
                    audit.record("worktree_busy_deferred", ticket_id=ticket.id, app=app.name)
                    reports.append(TicketReport(ticket.id, Outcome.SKIPPED, 0, 0.0, app.name,
                                                notes="deferred — worktree busy (another run active)"))
                    continue
                backlog = None
                try:
                    run_logger.write_note_log(
                        cfg, app.name, ticket.id,
                        f"[EU-380] concurrent drain (slot {slot}, {n} builders) — per-ticket stdout "
                        f"is attributed in the shared stream by app; consult the drain log.")
                except Exception:  # noqa: BLE001 — log setup must never block a run
                    pass
                try:
                    try:
                        if key not in gits:
                            app_slot = copy.copy(app)   # _make_git mutates app.workdir — never share
                            gits[key] = (_make_git(cfg, app_slot, slot), app_slot)
                        git, app_slot = gits[key]
                        if ticket.ephemeral:
                            backlog = NoneBacklog()
                        else:
                            if app.name not in backlogs:
                                backlogs[app.name] = make_backlog(app)
                            backlog = backlogs[app.name]
                        if key not in ensured:
                            git.ensure_clean()
                            ensured.add(key)
                        report = await process_ticket(ticket, app_slot, cfg, git, backlog, audit,
                                                      budget, stop_event)
                    except Exception as exc:  # noqa: BLE001 — one bad ticket must not kill the run
                        report = await _exception_report(cfg, ticket, app, exc, audit, backlog=backlog)
                finally:
                    pass
                reports.append(report)
                if (report.notes or "").startswith(_BASE_LEVEL_PREFIXES):
                    base_halted.add(app.name)
                    audit.record("base_halt_run", ticket_id=ticket.id, app=app.name,
                                 reason=(report.notes or "")[:200])
                fragment_keys = _fragment_keys_from_report(report)
                if fragment_keys:
                    fragment_items = _fetch_fragments_to_worklist(cfg, app, fragment_keys)
                    if fragment_items:
                        queue.extendleft(reversed(fragment_items))   # front, in dependency order
                        audit.record("fragment_injection", ticket_id=ticket.id,
                                     fragment_count=len(fragment_items), fragment_keys=fragment_keys)
                try:
                    from . import forensics
                    forensics.maybe_postmortem(cfg, report, audit)
                except Exception:  # noqa: BLE001 — diagnostics must never break the run
                    pass
            finally:
                in_flight_ids.discard(ticket.id)
                in_flight_tickets.pop(ticket.id, None)
                if lineage:
                    in_flight_lineage.discard(lineage)

    try:
        await asyncio.gather(*(_worker(s) for s in range(n)))
        if _stopped():
            audit.record("run_stopped", reason="commander stop (concurrent drain)")
        if budget.exceeded():
            audit.record("budget_stop", spent=budget.spent)
    finally:
        locks.close()
    return reports


async def _run_inner_serial(cfg: Config, worklist: list[tuple[AppConfig, Ticket]],
                            audit: AuditLog, stop_event=None, stop_between_tickets=None) -> list[TicketReport]:
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
    base_halted: set[str] = set()   # apps whose BASE failed this run (red/timed-out) — see below
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
            # EU-356: the ticket boundary honours BOTH stop channels — the full stop_event (manual
            # runs, also armed mid-ticket) and the boundary-only drain channel (autopilot; the
            # in-flight ticket just landed, so standing down here is exactly the drain's promise).
            if (stop_event is not None and stop_event.is_set()) or \
                    (stop_between_tickets is not None and stop_between_tickets.is_set()):
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
            if app.name in base_halted:
                continue   # base-level verdict already hit THIS app — leave its tickets queued
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
            # 2026-07-15: a base-level verdict (genuine red base, or a base-gate timeout under
            # load) applies to EVERY ticket of THAT app — processing its remaining tickets just
            # repeats it N times (the needs_human massacre: ~60 parks in two waves that day).
            # Halt per app, not per run: other apps' tickets in a unit-wide drain keep building
            # (the EU-87/EU-252 never-starve contracts); the halted app's tickets stay queued.
            if (report.notes or "").startswith(_BASE_LEVEL_PREFIXES):
                base_halted.add(app.name)
                _skipped = [t.id for a2, t in worklist[i:] if a2.name == app.name]
                if _skipped:
                    audit.record("base_halt_run", ticket_id=ticket.id, app=app.name,
                                 reason=(report.notes or "")[:200], skipped=_skipped)
                    print(f"  ⛔ {app.name}: base not buildable — leaving {len(_skipped)} queued "
                          "ticket(s) untouched.", flush=True)

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
    # EU-272: bind this context's stdout attribution to the ticket's app — under a concurrent
    # drain the old single-active-run heuristic collapses to None on every line; the ContextVar
    # flows through this coroutine's awaits so each slot's prints tag as its own app.
    _tee_token = cockpit_state.set_run_app(app.name)
    try:
        return await _process_ticket_inner(ticket, app, cfg, git, backlog, audit, budget, stop_event)
    finally:
        cockpit_state.reset_run_app(_tee_token)


async def _process_ticket_inner(ticket, app, cfg, git, backlog, audit, budget,
                                stop_event=None) -> TicketReport:
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
    # EU-395: cross-run memory — a re-picked ticket carries a digest of its own past failures
    # (errored/parked/review-FAIL) into the Planner/Builder context instead of rebuilding blind.
    ticket = _inject_prior_attempts(cfg, ticket, audit)

    if not cfg.dry_run and not ticket.ephemeral:
        # For a resuming ticket this transitions it out of 'Blocked' and back to 'In Progress' (EU-61).
        # 2026-07-19 audit: this was the ONLY unguarded set_status of 10 call sites — a transient
        # Atlassian error here ERRORED the whole run before a single build turn (and burned one of
        # the autopilot's 3 retry strikes). The board claim is cosmetic; the build is the work.
        try:
            backlog.set_status(ticket, "In Progress")
        except Exception as _texc:  # noqa: BLE001 - a status hiccup must never kill the build
            audit.record("claim_transition_failed", ticket_id=ticket.id, error=str(_texc)[:300])
            print(f"  · {ticket.id}: could not mark In Progress ({str(_texc)[:120]}) — building anyway.",
                  flush=True)
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
    real next time instead of looping triage -> re-queue forever.

    Fail direction (2026-07-19 stabilization, documented on purpose): an UNREADABLE audit log
    returns False — i.e. fails toward "triage again", trading a possible duplicate triage for
    never silently skipping the escalation a stuck ticket needs. The failed scan is printed so
    the direction is visible in the run log instead of indistinguishable from "never triaged"."""
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
    except Exception as exc:  # noqa: BLE001
        print(f"  · pm-triage audit scan failed ({exc}) — assuming never triaged (may repeat "
              "one triage)", flush=True)
    return False


# EU-358: how long a no_changes outcome keeps a ticket out of the drain. The guard exists to stop
# an immediate rebuild loop while the ticket sits in 'Needs Human' — it must NOT be a life sentence.
_NO_CHANGES_WINDOW_H = 48.0


def _recent_no_changes_ticket_ids(cfg: Config) -> set[str]:
    """Return the set of ticket IDs that RECENTLY had a no_changes outcome (EU-116).

    The drain uses this to skip tickets that already produced no changes — they're
    in 'Needs Human' awaiting verification/close, and re-running them would waste
    another full build cycle producing the same result.

    EU-358: 'recently' is a 48h window on the event timestamp. The audit log never rotates, so
    the unbounded version excluded a ticket FOREVER after one historical no_changes — a ticket
    the Commander edited and re-queued was silently skipped on every later drain."""
    try:
        import json
        from datetime import datetime, timedelta, timezone
        from . import dashboard as _D
        cutoff = datetime.now(timezone.utc) - timedelta(hours=_NO_CHANGES_WINDOW_H)
        no_changes_ids = set()
        for line in _D.audit_lines(cfg.audit_path):
            try:
                e = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if e.get("event") == "no_changes":
                ticket_id = e.get("ticket_id")
                if not ticket_id:
                    continue
                try:
                    ts = datetime.fromisoformat(str(e.get("ts", "")))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue     # no parseable timestamp → too old to trust as "recent"
                if ts >= cutoff:
                    no_changes_ids.add(ticket_id)
        return no_changes_ids
    except Exception:  # noqa: BLE001
        pass
    return set()


def _format_no_changes_findings(findings: list[dict]) -> str:
    """Render the EU-396 verify-pass per-AC findings as one line per criterion, satisfied/gap
    marked, evidence cited — so a human (or the escalation comment) reads it as one look, not an
    investigation. Empty input -> empty string (the caller then omits the section entirely)."""
    lines = []
    for f in findings or []:
        mark = "✅" if f.get("satisfied") else "❌"
        crit = (f.get("criterion") or "").strip() or "(unlabeled criterion)"
        ev = (f.get("evidence") or "").strip()
        lines.append(f"{mark} {crit}" + (f" — {ev}" if ev else ""))
    return "\n".join(lines)


# EU-395: cross-run memory. A ticket that errors, parks (needs_human) or fails review keeps NO
# memory of that once the process/run ends — the next pick re-plans and re-builds blind, often
# burning its 3 error strikes / 2 review passes rediscovering the same dead approach the audit log
# already recorded. These events, in this order of relevance, feed the digest:
_PRIOR_ATTEMPT_EVENTS = {"ticket_exception", "needs_human"}   # Outcome.ERRORED / Outcome.ESCALATED
# Deliberately tighter than builder._FEEDBACK_MAX_ITEMS/_FEEDBACK_MAX_CHARS (EU-38): this digest is
# a short "don't repeat this" pointer for the Planner/Builder, not the full retry feedback stream —
# same discipline (cap items, then chars, keep newest, mark elisions), smaller ceiling.
_PRIOR_ATTEMPTS_MAX_ITEMS = 4
_PRIOR_ATTEMPTS_MAX_CHARS = 1600


def _prior_attempts_digest(cfg, ticket_id: str) -> str | None:
    """A bounded 'what was tried, why it failed' digest of ticket_id's PAST attempts, built from the
    audit log: prior errors (ticket_exception), parks (needs_human), and review FAIL verdicts.

    Returns None on a first attempt (no such events found yet) or on any read failure — fails
    toward 'no digest' exactly like `_already_pm_triaged`: an unreadable audit log must never block
    or corrupt a build, it just costs the re-pick the memory this ticket exists to add. Capped in
    both items and chars via `builder._cap_feedback`'s own trimming (EU-38 discipline) so a ticket
    with a long, thrashy history can never blow the Planner/Builder input budget."""
    try:
        import json
        from . import dashboard as _D
        rows: list[tuple[str, str]] = []   # (ts, rendered line) — sorted oldest->newest below
        for line in _D.audit_lines(getattr(cfg, "audit_path", "./state/audit.jsonl")):
            try:
                e = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if e.get("ticket_id") != ticket_id:
                continue
            ev = e.get("event")
            ts = str(e.get("ts", ""))
            if ev in _PRIOR_ATTEMPT_EVENTS:
                reason = e.get("reason") or e.get("question") or e.get("error") or e.get("notes") or ""
                reason = str(reason).strip()
                if reason:
                    rows.append((ts, f"- [{ev}] {reason}"))
            elif ev == "review" and str(e.get("verdict", "")).upper() == "FAIL":
                detail = str(e.get("summary") or "").strip()
                changes = e.get("required_changes") or []
                if changes:
                    req = "; ".join(str(c) for c in changes[:3])
                    detail = f"{detail} — required: {req}" if detail else f"required: {req}"
                if detail:
                    rows.append((ts, f"- [review FAIL] {detail}"))
        if not rows:
            return None
        rows.sort(key=lambda r: r[0])   # chronological — _cap_feedback below keeps the NEWEST
        capped = builder_mod._cap_feedback(
            [line for _, line in rows],
            max_items=_PRIOR_ATTEMPTS_MAX_ITEMS, max_chars=_PRIOR_ATTEMPTS_MAX_CHARS)
        return "\n".join(capped) if capped else None
    except Exception as exc:  # noqa: BLE001 — audit-log trouble must never block a build
        print(f"  · prior-attempts digest skipped for {ticket_id}: {exc}", flush=True)
        return None


def _inject_prior_attempts(cfg, ticket, audit):
    """Fold `_prior_attempts_digest` into the ticket description BEFORE the Planner/Builder run
    (EU-395), the same channel `_resume_from_jira_answer` uses to inject a resolved decision — both
    the Planner (planner.plan) and the Builder (builder.build) already read ticket.description
    verbatim, so this is the one seam that reaches both without touching either contract. A no-op
    (returns ticket unchanged) on a first attempt or any digest-read failure."""
    digest = _prior_attempts_digest(cfg, ticket.id)
    if not digest:
        return ticket
    from dataclasses import replace
    desc = (ticket.description or "") + (
        "\n\n---\nPrior attempts on this ticket (from the audit log — what was tried and why it "
        "failed; do not repeat a dead approach without addressing why it failed):\n" + digest)
    audit.record("prior_attempts_injected", ticket_id=ticket.id, chars=len(digest))
    print(f"  ↺ {ticket.id}: prior-attempts digest injected ({len(digest)} chars)", flush=True)
    return replace(ticket, description=desc)


def _planner_verdict_parked_before(cfg: Config, ticket_id: str) -> bool:
    """True if this ticket was ALREADY parked once on a Planner non-BUILD verdict.

    The park (below) would otherwise cycle forever: the Commander /unblocks, the Planner re-runs,
    re-verdicts CLOSE, and parks again. A prior park means he has seen the Planner's reason and
    re-queued anyway — an explicit "build it". Deliberately UNBOUNDED (no time window): unlike
    EU-358's no_changes life-sentence, this guard fails toward BUILDING (today's behaviour), so a
    stale hit can only cost a build, never strand a real ticket.
    """
    try:
        import json
        from . import dashboard as _D
        for line in _D.audit_lines(cfg.audit_path):
            try:
                e = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if e.get("event") == "planner_verdict_park" and e.get("ticket_id") == ticket_id:
                return True
    except Exception:  # noqa: BLE001 — an unreadable audit must fail open (build), never strand
        pass
    return False


def _planner_verdict_question(ticket_id: str, verdict: str, reason: str) -> str:
    """The Commander-facing ask for a parked Planner verdict — plain language, concrete options.

    The reason is the Planner's own words, so it can carry leaked monologue ("## ANALYSIS",
    "Looking at the…"). decisions.add REJECTS such a question (returns None), which would drop the
    park and fall through to the build this exists to prevent — so strip those shapes here rather
    than lose the park.
    """
    # The reason is rendered as ONE line prefixed by "The Planner's reason:", so the validator's
    # line-START openers ("Looking at the…", "I need to…") are already defused by the collapse and
    # must NOT be excised — dropping those lines would throw away the substance (the commit sha that
    # justifies the verdict). Only the two markers it rejects ANYWHERE need removing.
    text = " ".join((reason or "").split())     # collapse to one line: no interior line-starts left
    text = text.replace("#", "")                # no markdown headers; also defuses the '## ANALYSIS' marker
    text = re.sub(r"(?i)reality check on\b", "", text).strip()
    why = text[:500] or "(the Planner gave no reason)"
    return (
        f"{ticket_id} was not built: the Planner reviewed it and judged it {verdict} — not work to "
        f"build. Nothing was closed or changed; it is parked for your call.\n\n"
        f"The Planner's reason: {why}\n\n"
        f"Your options:\n"
        f"• Agree — close {ticket_id} on the board (recommended if the reason above checks out).\n"
        f"• Disagree — reply /unblock {ticket_id} and the unit builds it next cycle, no questions "
        f"asked (this ask fires once per ticket, so it will not park again).\n"
        f"• Re-scope — edit the ticket description, then /unblock {ticket_id}."
    )


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
    # EU-353: last review binding this attempt saw — may stay None if every pass broke before
    # reaching the review step. Read (guarded) at the max-passes escalation site below to surface
    # any escalated review.unverifiable_gaps.
    review = None
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
    # EU-352: fingerprints of unverifiable-surface findings (UI/visual, pixel/layout,
    # browser-render — see reviewer._classify_unverifiable_finding) seen on ANY prior review pass
    # of this ticket attempt. Threaded into every review() call as `already_bounced` so a READ-ONLY
    # reviewer that keeps re-raising the same unrenderable claim gets it demoted (EU-351's
    # escalate-once gate) on the pass AFTER it first appears, instead of blocking forever. Same
    # single-attempt, in-memory lifetime as recent_reject_sigs/last_in_scope_sig above — no
    # cross-process persistence needed.
    bounced_unverifiable: set[str] = set()

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
        if not base_ok and base_gate_timed_out(base_report):
            # 2026-07-15 (twice: 04:20 and 17:41 waves): a box under load timed the base suite
            # out, the red got cached, and the drain force-parked every To Do ticket to
            # needs_human. A timeout is an environment verdict (EU-228 class), never a code-red:
            # charge no ticket, ask the Commander nothing. The notes carry the timeout marker so
            # autopilot's infra_classify path (EU-228) holds the drain with no error strike, and
            # _run_inner stops this run so the rest of the worklist stays queued untouched.
            audit.record("base_gate_infra", ticket_id=ticket.id,
                         report=(base_report or "")[:1500])
            print(f"  🌐 {ticket.id}: base gate timed out — environment/load, not a red base; "
                  "no ticket charged, run holding.", flush=True)
            try:
                # The pick already moved this ticket to In Progress with a "started" ping; without
                # a comment the board shows silent stalled work (the loop.py:240 invisibility
                # problem). Best-effort — a tracker hiccup must never break the infra path.
                backlog.add_comment(ticket, "⏳ Base gate timed out (environment/load, not a code "
                                            "failure) — no work was done on this ticket; the drain "
                                            "holds and retries automatically.")
            except Exception:  # noqa: BLE001
                pass
            return _resolve(TicketReport(ticket.id, Outcome.ERRORED, 0, cost, app.name, branch,
                                         notes=_BASE_INFRA_NOTES))
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
                                         notes=_RED_BASE_NOTES))

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
                # EU-225: if the Planner independently says CLOSE/ANSWER AND the ticket is provably
                # already shipped (changelog + origin/<base> commit), close it to QA with evidence
                # rather than rebuilding landed work (EU-191 was rebuilt 2 days after it merged).
                # Double-keyed (verdict + code evidence) and routed to QA — reversible, never Done.
                _ev = None
                if getattr(cfg, "autoclose_already_landed", True) and _pres.verdict in ("CLOSE", "ANSWER") \
                        and not ticket.ephemeral and not cfg.dry_run:
                    _ev = _already_landed(app, ticket.id)
                if _ev:
                    try:
                        backlog.set_status(ticket, "QA")
                        backlog.add_comment(
                            ticket, f"✅ Auto-closed to QA — {_ev}. The Planner verdicted "
                                    f"{_pres.verdict} and the work is already on origin/{app.base_branch}; "
                                    "not rebuilding (EU-225). Reopen to To Do to force a rebuild.")
                    except Exception as _exc:  # noqa: BLE001 — a board hiccup must not rebuild landed work
                        print(f"  · already-landed close: board update failed ({_exc}); reported anyway.",
                              flush=True)
                    audit.record("already_landed_autoclose", ticket_id=ticket.id,
                                 verdict=_pres.verdict, evidence=_ev)
                    print(f"  ✅ {ticket.id}: {_ev} + Planner {_pres.verdict} → closed to QA, not rebuilt.",
                          flush=True)
                    return _resolve(TicketReport(ticket.id, Outcome.SKIPPED, iteration, cost, app.name,
                                                 branch, notes=f"already landed — closed to QA: {_ev}"))
                # No provable code evidence — but the Planner still says this isn't work to build.
                # Building anyway is what burned AUTO-57 (2026-07-16: correct CLOSE verdict at
                # 16:57, built regardless, 1.5M tokens into the 60-min wall-clock cap → errored).
                # So PARK it for the Commander instead: one cheap Planner call, not a whole build.
                # Deliberately NOT an auto-close — closing on verdict alone with no code evidence is
                # the senior_pm mistake §2 undid; EU-225's evidence-keyed close above still owns
                # that. Fires ONCE per ticket (see _planner_verdict_parked_before) so a /unblock is
                # an unambiguous "build it" and can't ping-pong. The Planner is prompted to be
                # conservative here ("when in doubt, BUILD") so a non-BUILD verdict is high-signal.
                _park_ok = (getattr(cfg, "planner_verdict_park", True)
                            and _pres.verdict in ("CLOSE", "ANSWER", "REFILE")
                            and not ticket.ephemeral and not cfg.dry_run
                            and not _planner_verdict_parked_before(cfg, ticket.id))
                if _park_ok:
                    _q = _planner_verdict_question(ticket.id, _pres.verdict, _pres.answer or _pres.approach)
                    _entry = None
                    try:
                        _entry = decisions.add(cfg, ticket, app.name, _q)
                    except Exception as _exc:  # noqa: BLE001 — a park failure falls through to build
                        print(f"  · planner-verdict park failed ({_exc}) — building instead.", flush=True)
                    if _entry:
                        audit.record("planner_verdict_park", ticket_id=ticket.id,
                                     verdict=_pres.verdict, answer=(_pres.answer or "")[:600])
                        _notify(cfg, f"⏸️ {ticket.id} — the Planner says {_pres.verdict}, not work to "
                                     f"build. Parked for your call (nothing closed).\n\n"
                                     + decisions.reply_hint(ticket.id))
                        print(f"  ⏸️ {ticket.id}: Planner {_pres.verdict} + no landed-code evidence "
                              "→ parked for the Commander, not built.", flush=True)
                        return _resolve(TicketReport(
                            ticket.id, Outcome.ESCALATED, iteration, cost, app.name, branch,
                            notes=f"Planner verdict {_pres.verdict} — parked for the Commander"))
                    # decisions.add rejected the ask (or raised) → fall through and build, which is
                    # the pre-EU-375 behaviour: never silently drop the ticket.
                print(f"  planner · verdict {_pres.verdict} — building anyway (conservative; "
                      "not parked)", flush=True)
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
        # EU-341: on a retry, prepend the deterministic forensics classification of the prior failure
        # (category → recommended action) + how many times this ticket has failed before, so the
        # Builder acts on the KNOWN fix instead of rediscovering it (the reflexion pattern, wired
        # through EXISTING forensics.classify/attempts — no new memory store). Best-effort.
        _feedback = last_changes
        if iteration > 1 and getattr(cfg, "retry_forensics_enabled", True) and last_changes:
            try:
                from . import forensics as _forensics
                _cls = _forensics.classify("", last_changes[0])
                if _cls.get("category") != "unknown":
                    _hist = _forensics.attempts(cfg, ticket.id)
                    _hint = f"⚠️ KNOWN FAILURE PATTERN ({_cls['label']}): {_cls['action']}"
                    if len(_hist) > 1:
                        _hint += (f"\nThis ticket has already failed {len(_hist)}× — do NOT repeat the "
                                  "earlier dead ends; apply the fix above before anything else.")
                    _feedback = [f"{_hint}\n\n{last_changes[0]}", *last_changes[1:]]
            except Exception:  # noqa: BLE001 — forensics enrichment is best-effort, never blocks a build
                _feedback = last_changes
        req = BuildRequest(ticket=ticket, branch=branch, prior_issues=_feedback, iteration=iteration, adr=adr)
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
            # never the turn-limit phrase; num_turns is the only reliable signal. EU-408: a GLM pass
            # cut off by the per-pass token ceiling sets build.is_turn_limit directly — it can burn
            # few-but-huge turns (a giant tool-result read) so num_turns alone would miss it and drop
            # a clean cutoff into the ERRORED fallthrough. is_turn_limit is the one reliable signal
            # shared by BOTH blow-out classes (max-turns and the GLM token ceiling).
            if build.is_turn_limit or build.num_turns >= builder_mod.turns_for(cfg, eff):
                # 2026-07-19: feed the Scrum Master the attempt's REAL progress, not just "it ran
                # out" — fragments sliced blind repeated the parent's burn and re-blew the ceiling
                # (the AUTO-15x lineage). The builder's own summary tells the splitter what is
                # already done and where it got stuck, so fragments can start from the remainder.
                _progress = (build.summary or build.raw or "").strip()[:1200]
                split = await _try_scrum_split(
                    cfg, app, ticket, audit,
                    recap=(f"{ticket.id} ran out of turns before finishing — too big for a single pass."
                           + (f"\nWhere the attempt got to (builder's own summary):\n{_progress}"
                              if _progress else "")),
                    reason="Builder hit the turn limit — split into smaller, independently-shippable tickets.",
                    split_reason="turn-limit", iterations=iteration, cost=cost, branch=branch)
                if split is not None:
                    return _resolve(split)
                # Same senior ladder as _exception_report (the two turn-limit paths used to
                # DIVERGE: this one silently became ERRORED — ticking the EU-219 error counter
                # toward Blocked for a ticket that was neither broken nor wrong, just big — while
                # the exception path parked a decision). Unsplittable → re-queue ONCE with a
                # boosted budget; retry spent → park as a decision with the honest note.
                if (not ticket.ephemeral and not getattr(cfg, "dry_run", False)
                        and builder_mod.turn_retry_count(cfg, ticket.id) == 0
                        and builder_mod.mark_turn_retry(cfg, ticket.id)):
                    audit.record("turn_limit_retry", ticket_id=ticket.id, iteration=iteration)
                    _notify(cfg, f"⏳ {ticket.id} ran out of turns and can't be split smaller — "
                                 "retrying once with a raised turn budget before bothering you.")
                    print(f"  ⏳ {ticket.id}: turn-limit at the split depth cap — requeued once "
                          "with a bigger turn budget.", flush=True)
                    return _resolve(TicketReport(ticket.id, Outcome.REQUEUED, iteration, cost,
                                                 app.name, branch,
                                                 notes="turn-limit — requeued once with a raised turn budget"))
                _note = (f"{ticket.id} ran out of turns even after a boosted retry, and the Scrum "
                         "Master declined to split it further (depth cap — this lineage was already "
                         "auto-split as far as it goes). Re-scope/simplify the ticket, or raise the "
                         "turn budget (builder_max_turns). Nothing was merged.")
                try:
                    decisions.add(cfg, ticket, app.name, _note)
                except Exception:  # noqa: BLE001 - the escalation path must never crash the run
                    pass
                audit.record("needs_human", ticket_id=ticket.id, reason="turn-limit", question=_note)
                _notify(cfg, f"🛑 {ticket.id} — ran out of turns twice and couldn't be split. "
                             "Re-scope it or raise builder_max_turns.\n\n"
                             + decisions.reply_hint(ticket.id))
                print(f"  🛑 {ticket.id}: turn-limit — unsplittable, retry spent; escalated to you.",
                      flush=True)
                return _resolve(TicketReport(ticket.id, Outcome.ESCALATED, iteration, cost,
                                             app.name, branch,
                                             notes="ran out of turns — too big, unsplittable, retry spent"))
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
            # EU-396: before parking an UNVERIFIED "already satisfied" claim on the Commander, run
            # the Reviewer read-only against the UNCHANGED tree (no diff — nothing to review) with
            # the ticket's ACs: "are these ACs already satisfied — cite file:line per AC". A senior
            # teammate checks the claim first, instead of "verify and close it yourself" (the exact
            # senior_pm-style mistake §2 was retired for). Confident PASS with per-AC evidence closes
            # to QA (reversible, never Done) and is separately audited (no_changes_autoclose);
            # anything less than confident falls straight through to today's escalation below, with
            # the Reviewer's per-AC findings attached to it.
            verify_findings_text = ""
            if getattr(cfg, "verify_no_changes_enabled", True) and not ticket.ephemeral and not cfg.dry_run:
                try:
                    with _officer_transcript_context(app, ticket, "reviewer", cfg):
                        vr = await reviewer_mod.verify_no_changes(ticket, app, cfg, report)
                except Exception as exc:  # noqa: BLE001 — a verify crash must fall through to escalation, never break the run
                    vr = {"confident": False, "findings": [], "summary": f"verify call crashed: {exc}",
                          "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0}
                cost += vr.get("cost_usd", 0.0) or 0.0
                budget.add(vr.get("cost_usd", 0.0) or 0.0)
                _burn("reviewer", vr.get("input_tokens", 0) or 0, vr.get("output_tokens", 0) or 0)
                verify_findings_text = _format_no_changes_findings(vr.get("findings"))
                audit.record("no_changes_verify", ticket_id=ticket.id, iteration=iteration,
                             confident=bool(vr.get("confident")), findings=vr.get("findings", []),
                             summary=(vr.get("summary") or "")[:1000], cost_usd=vr.get("cost_usd", 0.0))
                if vr.get("confident"):
                    evidence = verify_findings_text or (vr.get("summary") or "").strip()
                    if not cfg.dry_run and not ticket.ephemeral:
                        try:
                            backlog.set_status(ticket, "QA")
                            backlog.add_comment(
                                ticket, "✅ Auto-verified to QA (EU-396) — the Builder made no changes "
                                        "and the Reviewer independently audited the UNCHANGED tree and "
                                        "confirmed every acceptance criterion is already met:\n\n"
                                        + evidence[:1500]
                                        + "\n\nReversible — reopen to To Do to force a rebuild if this "
                                          "is wrong.")
                        except Exception as _exc:  # noqa: BLE001 — a board hiccup must not block reporting
                            print(f"  · no-changes auto-close: board update failed ({_exc}); reported anyway.",
                                  flush=True)
                    audit.record("no_changes_autoclose", ticket_id=ticket.id, iteration=iteration,
                                 evidence=evidence[:1500])
                    print(f"  ✅ {ticket.id}: no-changes claim Reviewer-verified (per-AC evidence) — "
                          "closed to QA, not parked.", flush=True)
                    return _resolve(TicketReport(ticket.id, Outcome.SKIPPED, iteration, cost, app.name,
                                                 branch, notes="no changes — Reviewer-verified already satisfied, closed to QA"))
                print(f"  · {ticket.id}: no-changes claim not confidently verified — escalating with "
                      "the Reviewer's per-AC findings attached.", flush=True)
            # EU-116: a no-changes build leaves the ticket stuck In Progress and the drain re-runs it.
            # Move the ticket off In Progress to Needs Human so the Commander can verify/close it.
            # The drain guard in intake.from_drain will skip tickets with recent no_changes outcomes.
            # EU-153: Post no-changes comment
            note = "Builder produced no changes — the acceptance criteria are already satisfied or this work was already completed by another ticket."
            if verify_findings_text:
                # EU-396: attach the Reviewer's per-AC findings so the Commander's check is one
                # look, not an investigation — even though it wasn't confident enough to auto-close.
                note += ("\n\nThe Reviewer independently checked the unchanged tree against each AC "
                         "(not confident enough to auto-close — verify before agreeing):\n"
                         + verify_findings_text[:1200])
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
        # (EU-85 / EU-326: domain-gap classification was removed entirely in the Phase-2
        # collapse; the gate never re-classifies here.)
        gate = await _off_loop(cfg, run_gate, app, git.changed_paths())
        if not gate.passed:
            # Flake honesty (2026-07-09): a red gate must REPRODUCE before it burns a builder pass.
            # 5 of the last 14 "max passes — PM escalated" strandings were full-suite flakes
            # (EU-129/139/201/204/206 — 4 later merged with ZERO extra fix passes); worse, the same
            # flake twice tripped the fingerprint-stuck breaker. Mirrors the base-gate confirmation
            # re-run in gate.py. Only a reproduced red proceeds to fingerprint/stuck handling.
            confirm = await _off_loop(cfg, run_gate, app, git.changed_paths())
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
            last_changes = [_gate_retry_feedback(cfg, app, ticket, "Verification", gate)]
            continue
        last_gate_fp = ""   # a passing gate breaks the "consecutive" chain (flakes ≠ stuck)
        if app.gate_commands:
            print("  gate · passed", flush=True)

        # 2.7) DETERMINISTIC CHECKS (§3.3–5): lint → secret scan → lockfile sanity. Scripts, not
        # LLMs — cheap, unhallucinatable, and no reviewer runs until they are green; a failure
        # feeds the Builder as plain text (a free review round).
        det = await _off_loop(cfg, run_deterministic_checks, app, git.changed_paths(), git.diff_against_base())
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
            last_changes = [_gate_retry_feedback(cfg, app, ticket, "Deterministic checks", det)]
            continue
        last_gate_fp = ""   # all gates green this pass — reset the consecutive-failure chain

        # 3) REVIEW (spec + quality) on the diff
        _bar(REVIEW, active=REVIEW)
        print("  review · reviewer reading the diff…", flush=True)
        diff = git.diff_against_base()
        # EU-265: the gate above just ran this pass's REAL test suite green — hand that proof to
        # the Reviewer's EU-268 execution-AC gate as machine evidence, so an "all tests pass" AC
        # is satisfied by the gate that literally ran the tests instead of force-FAILing every
        # pass on missing Builder prose. '' for a no-op gate (see _gate_execution_evidence).
        gate_evidence = _gate_execution_evidence(app, gate, git.changed_paths())
        # EU-72: hand the Reviewer the Builder's BuildArtifact (primary context) + the pool it
        # publishes its ReviewVerdict into. EU-52: escalate the reviewer on re-review.
        # EU-197: Wrap reviewer with transcript context to capture full tool inputs + reasoning
        with _officer_transcript_context(app, ticket, "reviewer", cfg):
            review = await reviewer_mod.review(diff, ticket, app, cfg, iteration,
                                               store=store, build_artifact=store.build,
                                               already_bounced=bounced_unverifiable,
                                               gate_evidence=gate_evidence)
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
                                                   store=store, build_artifact=store.build,
                                                   already_bounced=bounced_unverifiable,
                                                   gate_evidence=gate_evidence)
            cost += review.cost_usd
            budget.add(review.cost_usd)
            _burn("reviewer", review.input_tokens, review.output_tokens)   # EU-96 retry
        # EU-352: fold this pass's unverifiable-surface findings (freshly raised OR already demoted
        # by EU-351's _enforce_bounce_once) into the accumulator BEFORE the audit record below, so
        # the NEXT pass's review() calls above see them in already_bounced.
        bounced_unverifiable |= reviewer_mod.collect_unverifiable_fingerprints(review)
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

        # 2026-07-19 Commander order ("acceptance criteria were done — so why not put that ticket
        # to QA?"): reconcile an INCONSISTENT FAIL instead of churning on it. Two rules, both
        # requiring the reviewer's OWN findings to contradict its verdict:
        #   (a) any pass — FAIL + spec_met + zero blocker/major findings violates the reviewer's
        #       written contract ("PASS only if…; otherwise FAIL") — the AUTO-198 case: "criteria
        #       fully met ✅, two minor concerns (not blocking)" yet verdict FAIL, which burned the
        #       rebuild pass into the turn limit and parked a DONE deliverable on the Commander.
        #   (b) the FINAL pass — FAIL + spec_met + majors but no blocker: majors past the last
        #       rebuild can't drive convergence anymore (EU-174/QW3 corpus: 100% of round-≥2
        #       objections were NEW), so a criteria-met build ships as PASS-with-findings — the
        #       advisory_ship path below files every leftover as a backlog ticket and lands → QA —
        #       instead of escalating a finished change. A blocker or unmet criteria NEVER
        #       reconciles: those FAILs are the reviewer doing its job.
        # P1 (2026-07-21 production audit): reconciliation demands the reviewer's WHOLE output be
        # inconsistent with FAIL — not just issue severities. A FAIL carrying spec_gaps or concrete
        # required_changes is the reviewer asking for real work and must never be overridden.
        if (review.verdict.value == "FAIL" and review.spec_met and not blockers
                and not review.needs_human and not review.spec_gaps
                and not review.required_changes):
            _final_pass = iteration >= max_passes
            if not review.blocking_issues or _final_pass:
                _rec_reason = ("fail-with-minors-only" if not review.blocking_issues
                               else "final-pass-criteria-met-majors")
                review.verdict = Verdict.PASS
                audit.record("review_verdict_reconciled", ticket_id=ticket.id,
                             iteration=iteration, reason=_rec_reason,
                             majors=len(review.blocking_issues))
                print(f"  ⚖ review verdict reconciled FAIL→PASS ({_rec_reason}) — criteria met, "
                      f"nothing blocking; leftovers ship as advisories", flush=True)

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
                        # EU-337: page a PLAIN-LANGUAGE decision (problem + options + recommendation)
                        # via _decision_brief — the same distillation the other escalation sites use —
                        # instead of pasting the raw reviewer bullets/code identifiers the Commander
                        # then has to reverse-engineer (the EU-330 "BAD" format). The stored decision
                        # keeps the full `question`; only the phone ping is distilled.
                        _notify(cfg, f"❓ {ticket.id} — needs YOUR decision:\n"
                                     f"{await _decision_brief(cfg, ticket.id, question)}"
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

            def _land_serialized():
                # explicit land serialization (concurrent drain) — one land at a time, off-loop
                with _LAND_SERIAL_LOCK:
                    if "commenter" in _land_sig.parameters:
                        return _land(ticket, app, cfg, git, backlog, audit, branch, iteration,
                                     cost, build, review, commenter=commenter)
                    return _land(ticket, app, cfg, git, backlog, audit, branch, iteration,
                                 cost, build, review)

            result = await _off_loop(cfg, _land_serialized)
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
                         "closed the parent. The squad takes the fragments next.")
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
    # EU-353: surface any escalated unverifiable_gaps on the last review this attempt saw —
    # exactly once (post_unverifiable_gaps self-guards per ticket_id) — and fold them into the
    # report's notes for cockpit/status-board visibility. Best-effort: a tracker hiccup here must
    # never turn an already-decided escalation into an unhandled exception.
    gap_note = ""
    gaps = getattr(review, "unverifiable_gaps", None) if review is not None else None
    if gaps and backlog and not ticket.ephemeral:
        try:
            commenter.post_unverifiable_gaps(backlog, ticket.key, gaps)
        except Exception as exc:  # noqa: BLE001 — best-effort, never blocks the escalation
            print(f"  · unverifiable-gaps comment skipped ({exc})", flush=True)
        gap_note = " · unverifiable ACs: " + " | ".join(gaps)
    return _resolve(TicketReport(ticket.id, Outcome.ESCALATED, max_passes, cost, app.name, branch,
                                 notes="max_iterations reached without a passing review" + gap_note))


# EU-261: the land commit is PERMANENT history, and until now it was formatted straight from two
# untrusted strings — `ticket.summary` (a Jira title, which for a filed finding is the whole finding
# text: 88ec96a and dfa0cab both have 185-char subjects) and `build.summary`, which builder.py sets to
# `run.final` — the RAW final assistant message. The Builder's prompt asks for "≤5 tight bullets" but
# that is advisory only, so 11 product DEV commits since 07-01 open their body with chat preamble
# (af6df5a: "Perfect! All tests are passing. Let me now create a summary of the changes made:").
# These two helpers are pure so the shape of history is a tested contract (eu261_commit_message_test).
_SUBJECT_MAX = 72  # git convention: a subject that stays readable in `git log --oneline`

# A heading line that marks where the real summary starts ("## Summary", "**Summary**", "Summary:").
# Anchored at line-start and barred from sentence punctuation so ordinary prose that merely mentions
# the word ("Let me now create a summary of the changes made:") can never match.
_SUMMARY_HEADING_RE = re.compile(r"^\s{0,3}(?:#{1,6}\s*)?\**\s*Summary\b[^.!?]*$", re.IGNORECASE)
# Same bullet/numbered shapes builder._section_bullets recognises — the fallback anchor.
_BULLET_RE = re.compile(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)\S")


def _strip_preamble(text: str) -> str:
    """Drop the Builder's chat preamble: return ``text`` from its first ## Summary heading or first
    bullet, whichever comes FIRST. Earliest-anchor (rather than heading-then-bullet) is deliberate —
    it can only ever keep MORE than the alternative, so a summary that leads with bullets and heads a
    later section 'Summary' doesn't lose those leading bullets. When neither anchor matches, the text
    passes through untouched: this trims noise, it must never be a lossy filter."""
    lines = (text or "").splitlines()
    for i, ln in enumerate(lines):
        if _SUMMARY_HEADING_RE.match(ln) or _BULLET_RE.match(ln):
            return "\n".join(lines[i:]).strip()
    return (text or "").strip()


def _commit_message(ticket, build) -> str:
    """The land commit's message: a ≤72-char `{id}: {summary…}` subject, the preamble-stripped build
    summary as the body, and the Reviewed-by trailer last. The FULL untrimmed summary stays on the
    BuildResult and in the audit trail, so nothing is lost — only the permanent history is tidied."""
    prefix = f"{ticket.id}: "
    subject = prefix + builder_mod._digest(ticket.summary, limit=max(12, _SUBJECT_MAX - len(prefix)))
    parts = [subject]
    body = _strip_preamble(getattr(build, "summary", "") or "")
    if body:
        parts.append(body)
    parts.append("Reviewed-by: autodev-reviewer")
    return "\n\n".join(parts)


def _manual_test_block(build, review) -> str | None:
    """The manual-test hand-off text when this land needs the Commander's hands, else None
    (2026-07-20 Commander order: 'if need manual testing they need to write in the comments the
    exact test steps and put in Blocked').

    Two deterministic signals, either suffices:
      · the Builder's own 'MANUAL TEST:' section (the prompt contract obliges it whenever an AC
        could not be verified — missing env, device/browser matrix, visual checks). Used VERBATIM:
        the Builder knows the exact steps.
      · review.unverifiable_gaps — the escalate-once demotions (exec-gate / EU-351): claims nobody
        mechanical could verify. Rendered as a numbered checklist when the Builder wrote no block.
    """
    summary = (getattr(build, "summary", "") or getattr(build, "raw", "") or "")
    m = re.search(r"MANUAL TEST:\s*\n?(.*?)(?:\n\s*\n[A-Z]{2,}|\nTEST:|\Z)", summary, re.S)
    block = (m.group(1).strip() if m else "")
    gaps = list(getattr(review, "unverifiable_gaps", None) or [])
    if block:
        return block
    if gaps:
        steps = "\n".join(f"{i + 1}. Verify by hand: {g}" for i, g in enumerate(gaps))
        return "The unit could not verify these itself:\n" + steps
    return None


def _postmerge_verify_flag(cfg, app, ticket, git, merge_sha, audit, backlog, iteration, cost,
                           branch) -> Optional[TicketReport]:   # noqa: F821 — Optional is lazy via __future__
    """EU-453 — the post-merge dev-HEAD re-verification WIRING called from ``_land``.

    The post-merge counterpart to the pre-land ``base_gate_check`` / ``run_gate``: AFTER a land has
    actually moved dev to the merged tip, re-run the app's own ``gate_commands`` against that ACTUAL
    tip via ``postmerge_verify.verify()`` and FLAG a red verdict. This is the hard-lesson mirror of
    smoke: nothing post-merge may auto-park the queue, so a red here NEVER reverts (the revert is the
    Commander's call / the next pick's base gate); it only records an audit event, notifies, comments,
    and sets the ticket to ``Needs Human`` (skipped for ``ticket.ephemeral``), then returns the ticket
    as ``Outcome.ESCALATED``. The merge already happened and stands.

    Returns an ``ESCALATED`` ``TicketReport`` on a TRUE red (``verify -> (False, evidence)``);
    ``None`` on green, an engine green-skip (sha-mismatch / runner-error -> ``(True, ...)``), or when
    the feature isn't armed (``postmerge_verify.should_run`` is False) — in every ``None`` case
    ``_land`` proceeds to smoke → ci → MERGED.

    Never raises into the loop: the whole body is guarded so a verify/tracker/notify hiccup cannot
    corrupt a land that already succeeded (the merge already happened and stands). The wiring's
    ``postmerge_verify_fail`` / ``postmerge_verify_pass`` events are SEPARATE from and ADDITIONAL to
    the engine's internal ``postmerge_verify_red`` / ``_green`` / ``_skip`` (the wiring carries
    ``ticket_id`` + ``merge_sha``; the engine's carry ``app`` + ``reason``) — intentional, mirroring
    how the sentinel loop records events atop ``sentinel.guard``'s. Do NOT dedupe them.
    """
    from . import postmerge_verify
    if not postmerge_verify.should_run(cfg, app):
        return None
    try:
        pok, pnote = postmerge_verify.verify(cfg, app, git, merge_sha, audit)
    except Exception as exc:  # noqa: BLE001 — a verify hiccup must NEVER corrupt a landed merge
        audit.record("postmerge_verify_skip", ticket_id=ticket.id, app=app.name,
                     error=str(exc).splitlines()[0][:200])
        return None
    if pok:
        # GREEN (or an engine green-skip): quiet — record the wiring-level pass and proceed to MERGED.
        audit.record("postmerge_verify_pass", ticket_id=ticket.id, app=app.name, merge_sha=merge_sha)
        return None

    # RED — FLAG ONLY. The merge already happened and stands; we NEVER revert (the Commander's call /
    # the next pick's base gate). Record the wiring-level fail event atop the engine's postmerge_verify_red.
    try:
        audit.record("postmerge_verify_fail", ticket_id=ticket.id, app=app.name, merge_sha=merge_sha)
        # EU-454: publish_base_green already wrote a green entry for merge_sha BEFORE this verify
        # ran (EU-376's dedup). A RED here means that entry is now FALSE — the next pick's
        # base_gate_check would HIT it and skip re-running against the real (red) dev (the EU-447
        # false-green hazard). Evict it so the next pick MISSES and re-verifies. Best-effort: a
        # failure just leaves the stale entry (the merge already stands; green is left untouched).
        evict_base_green(app, cfg, merge_sha)
        msg = (f"🚨 Post-merge dev-HEAD verify FAILED for {ticket.id} — DEV may be silently red; "
               f"needs revert/escalation\n• {pnote[:900]}")
        _notify(cfg, msg)
        if not ticket.ephemeral:
            backlog.set_status(ticket, "Needs Human")
            backlog.add_comment(ticket, msg)
        print(f"  🔎 {ticket.id}: post-merge dev-HEAD verify RED — flagged (merge stands, no "
              f"revert). {pnote[:200]}", flush=True)
    except Exception as exc:  # noqa: BLE001 — the merge already stands; a tracker/notify outage must not raise
        audit.record("tracker_reconcile_needed", ticket_id=ticket.id, phase="postmerge_verify",
                     base=app.base_branch, error=str(exc).splitlines()[0][:200])
        print(f"  🔎 {ticket.id}: post-merge dev-HEAD verify RED, but the tracker/notify update "
              f"failed ({exc}) — reconcile by hand (merge stands).", flush=True)
    return TicketReport(ticket.id, Outcome.ESCALATED, iteration, cost, app.name, branch,
                        notes=f"post-merge dev-HEAD verify red: {pnote[:160]}")


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
    git.commit_all(_commit_message(ticket, build))
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
        # EU-379: on a land race, re-trial IN-PROCESS instead of requeuing to a fresh build. The
        # feature branch is intact (land_trial only deletes the throwaway temp before raising), so
        # losing the race costs one re-merge + one gate run (~3 min), not a full planner+builder
        # rebuild (mean ~6M tokens; the requeue path re-runs everything from scratch). Bounded:
        # 3 total attempts, then fall through to today's REQUEUE — the never-worse fallback. At
        # N=1 concurrency races never happen in practice (land_race_requeue: 0 events all-time),
        # so this path stays dormant until a concurrent drain (EU-380) arms it.
        landed = False
        race_detail = ""
        for _attempt in range(3):
            try:
                git.land_trial(temp)
                landed = True
                # P0 (2026-07-21 production audit): the push above is the IRREVERSIBLE moment, but
                # everything after it (branch cleanup, base sync, an LLM comment, Jira transition,
                # changelog) is seconds of network+LLM in which a process death left NO durable
                # merged-marker — boot resume then re-picked the still-In-Progress ticket and
                # rebuilt on top of its own merged code (the EU-307 class; AUTO-80 re-billed ~$7
                # after the 2026-07-20 machine sleep). Record the durable audit event NOW; the
                # richer post-land event below keeps its fields.
                audit.record("land_pushed", ticket_id=ticket.id, base=app.base_branch)
                break
            except LandRaceError as exc:
                race_detail = str(exc).splitlines()[0][:200]
                if _attempt == 2:
                    break              # attempts exhausted — fall through to the requeue below
                audit.record("land_race_retrial", ticket_id=ticket.id, base=app.base_branch,
                             attempt=_attempt + 1, detail=race_detail)
                print(f"  land · {ticket.id}: {app.base_branch} advanced mid-land — re-trialing "
                      f"in-process (attempt {_attempt + 2}/3; branch intact, gate re-runs).",
                      flush=True)
                # Re-merge onto the NEW tip and re-prove it — the gate must run against what will
                # actually land; skipping it here would land an ungated combined tree (the exact
                # thing EU-259 rejected rebase-and-push for).
                if not git.trial_merge(branch, temp, merge_msg):
                    break              # no longer merges cleanly against the new tip → requeue
                regate = run_gate(app, git.changed_paths())
                audit.record("dev_gate", ticket_id=ticket.id, passed=regate.passed,
                             failing=[] if regate.passed else _gate_failures(regate.report),
                             report_tail="" if regate.passed else (regate.report or "").strip()[-2000:])
                if not regate.passed:
                    break              # red against the new tip → the requeue path is honest
                merge_sha = git.current_sha()
        if not landed:
            # EU-259: the base advanced under us (a concurrent land) — nothing was merged. This is a
            # benign race, NOT a build/infra error: requeue so the next drain re-trials this ticket
            # against the new base AND re-runs the gate. No Jira status changed yet (that happens
            # only after a successful land below), so the ticket stays put for the resume. REQUEUED
            # does not tick the EU-219 error counter; a distinct audit event keeps forensics honest.
            # land_trial already detached to a clean base and deleted the trial branch before raising.
            audit.record("land_race_requeue", ticket_id=ticket.id, base=app.base_branch,
                         detail=race_detail)
            print(f"  land · {ticket.id}: {app.base_branch} advanced mid-land — re-trialing next "
                  "drain (nothing merged, gate will re-run).", flush=True)
            return TicketReport(ticket.id, Outcome.REQUEUED, iteration, cost, app.name, branch,
                                notes="dev advanced during land — re-trial next drain (gate re-runs)")
        # EU-376: the dev_gate above proved THIS exact commit green, and land_trial just
        # fast-forwarded it to <base> — publish the verdict so the NEXT ticket's base_gate_check
        # hits the cache instead of re-running the identical full suite on the identical commit
        # object (measured 2026-07-16: 14/14 misses, 178s of duplication per ticket). Green-only
        # and post-push by construction; publish_base_green itself refuses lint-armed apps.
        publish_base_green(app, cfg, merge_sha)
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
            # 2026-07-20 Commander order: a land whose verification needs HUMAN hands (an AC the
            # unit could not run itself) goes to the Blocked column with the EXACT manual test
            # steps as the hand-off comment — not to QA as if it were fully machine-verified.
            # ('Needs Human' is the logical status the boards map to their Blocked column.)
            manual = None if cfg.mark_done_on_merge else _manual_test_block(build, review)
            head = ("marked Done" if cfg.mark_done_on_merge
                    else "moved to Blocked — manual test needed" if manual else "moved to QA")
            # Best-effort: the code IS merged at this point — a Jira hiccup here must degrade to a
            # log line, not propagate to _exception_report and mislabel a successful land as a
            # ticket_exception (which would strand the already-merged ticket In Progress).
            try:
                if manual:
                    backlog.set_status(ticket, "Needs Human")
                    backlog.add_comment(
                        ticket,
                        f"🧪 Merged to {app.base_branch} — needs YOUR manual test before sign-off.\n\n"
                        f"MANUAL TEST STEPS:\n{manual}{test_line}\n\n"
                        f"Pass → move to Done. Fail → comment what broke and move it back to To Do.")
                    audit.record("manual_test_required", ticket_id=ticket.id,
                                 gaps=list(getattr(review, "unverifiable_gaps", None) or [])[:8])
                else:
                    backlog.set_status(ticket, "Done" if cfg.mark_done_on_merge else "QA")
                    backlog.add_comment(
                        ticket,
                        f"✅ Merged to {app.base_branch} → {head}.\nWhat was done:\n{whatdone}{test_line}")
            except Exception as exc:  # noqa: BLE001 — tracker trouble never un-lands a merge
                # EU-310: a lost post-merge transition strands the ticket In Progress, so the drain
                # re-picks and rebuilds already-merged code (EU-307, 2026-07-14). Record it as an
                # audit event, not just a print, so the miss is diagnosable — the in-memory
                # recently-merged guard in autopilot is what actually prevents the re-pick.
                print(f"  land · ticket status update skipped ({exc})", flush=True)
                audit.record("merge_transition_failed", ticket_id=ticket.id, error=str(exc)[:200])
            # EU-374 (EU-301 step 5): if this was the Epic's final VERIFY child and every sibling
            # is already Done/QA, close the Epic itself — otherwise auto-split Epics accumulate
            # open forever. Best-effort inside the helper: never un-lands the merge.
            _maybe_close_epic(backlog, ticket, audit)
        _manual_tail = (not ticket.ephemeral and not cfg.mark_done_on_merge
                        and _manual_test_block(build, review))
        done = "" if ticket.ephemeral else (
            " · marked Done" if cfg.mark_done_on_merge
            else " · moved to Blocked — exact manual test steps are on the ticket" if _manual_tail
            else " · moved to QA")
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
                # EU-387: flag the pending restart for the drain's cycle boundary — with
                # self_update_auto_restart on (default), the process exits cleanly once idle and
                # the keepalive respawns it on this landed code; EU-385 re-arms the drains on boot.
                from . import autopilot as _ap
                if _ap.flag_self_update(cfg, ticket.id, sha=merge_sha, audit=audit):
                    # EU-405: the promise of an automatic restart is now TRUE even for a manual run —
                    # the serve-level self-restart watcher (autopilot.ensure_self_restart_watcher,
                    # started from server.serve) consumes the flag at an idle boundary whether or not a
                    # drain is armed. But it is still conditional on the knob: with
                    # self_update_auto_restart OFF the watcher clears the flag and never exits, so
                    # announcing "restarting automatically" would be the announce-lie that stranded
                    # the unit on old code for 11h. Tell the truth either way.
                    if getattr(cfg, "self_update_auto_restart", True):
                        _notify(cfg, f"⚠️ {ticket.id} changed the squad's own code — restarting automatically "
                                     "once idle (self_update_auto_restart); drains re-arm on the new sha.")
                    else:
                        _notify(cfg, f"⚠️ {ticket.id} changed the squad's own code. "
                                     "self_update_auto_restart is OFF, so the cockpit keeps running the "
                                     "OLD code until you restart it by hand (./general deploy).")
                else:
                    # The flag did NOT persist — no automatic restart will happen. Never announce one.
                    _notify(cfg, f"⚠️ {ticket.id} changed the squad's own code but the restart flag "
                                 "could not be written — RESTART THE COCKPIT BY HAND (./general deploy) "
                                 "or it keeps running the old code.")
        except Exception:  # noqa: BLE001 — the signal is best-effort
            pass
        # Technical Writer: log this land to the unit's feature changelog (best-effort, never breaks).
        _record_changelog(cfg, ticket, app, review.summary or build.summary, turl)
        # EU-378: a land is when dev's facts change — re-derive the STATE OF DEV brief so the
        # NEXT officer's preamble reflects the code that just merged, not the pre-land world.
        try:
            from . import devstate
            devstate.refresh(cfg)
        except Exception as _exc:  # noqa: BLE001 — a brief refresh must never break a land
            print(f"  · dev-state refresh skipped ({_exc})", flush=True)

        # SRE: run the heavier post-merge suite on the landed DEV; if it's red, roll the merge
        # back (forward-only) and hand the ticket back rather than leave DEV broken.
        from . import sentinel
        if sentinel.should_run(cfg, app):
            ok, snote = sentinel.guard(cfg, app, ticket, git, merge_sha, audit)
            if not ok:
                # EU-367: the SRE has ALREADY reverted the merge (irreversible git effect — DEV is
                # restored). The tracker writes below are the ONLY thing telling the board this
                # ticket needs a human; a tracker outage here used to raise straight out of _land,
                # mislabelling the ticket a ticket_exception AND losing the Needs-Human signal — DEV
                # reverted but the board still shows In Progress. Guard them: on failure, record a
                # loud, reconcilable audit event + Telegram with the exact manual step, and still
                # return ESCALATED (the intended outcome).
                if not ticket.ephemeral:
                    try:
                        backlog.set_status(ticket, "Needs Human")
                        backlog.add_comment(ticket,
                            f"⚠️ SRE rolled back from {app.base_branch}.\n"
                            f"• {snote[:900]}")
                    except Exception as exc:  # noqa: BLE001 — DEV is already reverted; don't crash
                        audit.record("tracker_reconcile_needed", ticket_id=ticket.id,
                                     phase="sentinel_revert", base=app.base_branch,
                                     error=str(exc).splitlines()[0][:200])
                        _notify(cfg, f"⚠️ {ticket.id}: SRE reverted the merge on {app.base_branch} "
                                     "(DEV is restored) but the tracker update FAILED — the ticket "
                                     "still shows In Progress. Set it to Needs Human manually.")
                        print(f"  🛡️ {ticket.id}: SRE reverted, but tracker update failed ({exc}) — "
                              "reconcile the ticket status by hand.", flush=True)
                print(f"  🛡️ {ticket.id}: SRE reverted the merge — needs you.", flush=True)
                return TicketReport(ticket.id, Outcome.ESCALATED, iteration, cost, app.name, branch,
                                    notes=f"sentinel reverted: {snote[:160]}")

        # EU-453: re-run the app's own gate_commands against the ACTUAL merged dev HEAD
        # (postmerge_verify). Flag-only — a red merge is FLAGGED (audit + Telegram + Needs Human) and
        # the ticket returned as ESCALATED, but the merge STANDS (never auto-reverts; that's the
        # Commander's call / the next pick's base gate). Sits between the SRE (which CAN revert) and
        # the lighter smoke canary. No-op unless the app opts in BOTH the master + per-app
        # postmerge_verify flag (postmerge_verify.should_run).
        pm = _postmerge_verify_flag(cfg, app, ticket, git, merge_sha, audit, backlog,
                                    iteration, cost, branch)
        if pm:
            return pm

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

        # EU-353: surface any escalated review.unverifiable_gaps exactly once at this terminal
        # (MERGED) outcome — post_unverifiable_gaps self-guards per ticket_id — and fold them
        # into TicketReport.notes for cockpit/status-board visibility. Best-effort: a tracker
        # hiccup here must never un-land an already-successful merge.
        gap_note = ""
        gaps = getattr(review, "unverifiable_gaps", None) if review else None
        if gaps and backlog and not ticket.ephemeral:
            try:
                commenter.post_unverifiable_gaps(backlog, ticket.key, gaps)
            except Exception as exc:  # noqa: BLE001 — best-effort, never un-lands a merge
                print(f"  land · unverifiable-gaps comment skipped ({exc})", flush=True)
            gap_note = " · unverifiable ACs: " + " | ".join(gaps)

        return TicketReport(ticket.id, Outcome.MERGED, iteration, cost, app.name, branch,
                            notes=f"merged to {app.base_branch}"
                            + (", Done" if cfg.mark_done_on_merge else ", awaiting QA")
                            + smoke_note + ci_note + gap_note)

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
    # EU-367: the branch is pushed and (if configured) the PR is already created — remote side
    # effects that are done. A tracker outage on the comment/attach below must NOT raise out of
    # _land (which would mislabel the ticket a ticket_exception and, worse, LOSE the PR link so the
    # already-open PR is orphaned from the board). Guard it: on failure the PR url is preserved in
    # the audit event + Telegram, and the ticket still reports PR_OPENED.
    if not ticket.ephemeral:
        try:
            backlog.add_comment(ticket,
                f"⚠️ Passed review — not auto-merged.\n"
                f"• Reason: {reason}\n"
                + (f"• PR: {pr_url}" if pr_url else "• Open a PR manually."))
            if pr_url:
                backlog.attach_pr(ticket, pr_url)
        except Exception as exc:  # noqa: BLE001 — the PR already exists; never lose its link
            audit.record("tracker_reconcile_needed", ticket_id=ticket.id, phase="pr_opened",
                         pr_url=pr_url, error=str(exc).splitlines()[0][:200])
            print(f"  land · PR opened but the tracker update failed ({exc}) — PR: {pr_url}", flush=True)
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
