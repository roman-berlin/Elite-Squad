"""Intake — turn a request into a worklist of (app, ticket) pairs.

Three ways to feed work in:
  - text   : a free-text bug/feature -> an ad-hoc (ephemeral) ticket, no tracker needed
  - ticket : one or more existing Jira keys
  - drain  : pull ready tickets from an app's backlog (or every backlogged app)
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .backlog.base import make_backlog
from .config import AppConfig, Config
from .contracts import Ticket

WorkItem = tuple[AppConfig, Ticket]


def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return s[:40] or "task"


def adhoc_ticket(app: AppConfig, summary: str, acceptance: list[str],
                 description: str | None = None) -> Ticket:
    tid = "adhoc-" + _slug(summary)
    return Ticket(
        id=tid, key=tid, summary=summary,
        description=description or summary,
        acceptance_criteria=acceptance or [],
        app=app.name, ephemeral=True,
    )


def from_text(cfg: Config, app_name: str, summary: str, acceptance: list[str],
              description: str | None = None) -> list[WorkItem]:
    app = cfg.app(app_name)
    return [(app, adhoc_ticket(app, summary, acceptance, description))]


_KEY = re.compile(r"[A-Z][A-Z0-9]+-\d+")


def extract_key(s: str) -> str:
    """Accept a bare key (AUTO-1) or a full Jira URL and return the key."""
    m = _KEY.search(s)
    return m.group(0) if m else s


def from_tickets(cfg: Config, app_name: str, keys: list[str],
                 spec: str | None = None, title: str | None = None) -> list[WorkItem]:
    """Fetch real Jira tickets (so status moves + comments + Telegram fire). With `spec`,
    the build brief comes from that file instead of the Jira description — for tickets
    whose real spec lives in a linked doc — while keeping the ticket's Jira identity."""
    from dataclasses import replace
    app = cfg.app(app_name)
    backlog = make_backlog(app)
    items: list[WorkItem] = []
    for k in keys:
        t = backlog.get_task(extract_key(k))
        if spec is not None:
            t = replace(t, description=spec, summary=(title or t.summary))
        items.append((app, t))
    return items


# Per-app backlog-fetch failures from the most recent from_drain() call (app name -> message). The
# cockpit panel + autopilot read this so an unreachable/misconfigured board surfaces as an explicit
# warning instead of silently looking like "queue clear · nothing of yours". Cleared per app on a
# clean fetch.
LAST_DRAIN_ERRORS: dict[str, str] = {}

# Per-app git repo validation failures (app name -> message). Set when an app's repo_path is not
# a valid git repository. The autopilot skips such apps during the drain and announces once per
# run, not every cycle. EU-128.
MISSING_REPO_ERRORS: dict[str, str] = {}


def _validate_git_repo(app: AppConfig) -> bool:
    """Check if an app's repo_path is a valid git repository.

    Returns True if the repo is valid, False otherwise. On failure, records the error
    in MISSING_REPO_ERRORS for the autopilot to surface.

    Handles apps without repo_path gracefully (for test compatibility).
    """
    # Check if the app has a repo_path attribute (handles SimpleNamespace test objects)
    if not hasattr(app, "repo_path") or not app.repo_path:
        # Apps without repo_path are considered valid (legacy test compatibility)
        MISSING_REPO_ERRORS.pop(app.name, None)
        return True

    repo_path = Path(app.repo_path).expanduser()
    git_dir = repo_path / ".git"

    # First check if the directory exists
    if not repo_path.exists():
        MISSING_REPO_ERRORS[app.name] = f"repo not found at {repo_path}"
        return False

    # Check if it's a git repository (either .git directory or .git file for worktrees)
    if not git_dir.exists():
        MISSING_REPO_ERRORS[app.name] = f"not a git repository: {repo_path}"
        return False

    # Verify we can run git commands in it
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            MISSING_REPO_ERRORS[app.name] = f"git rev-parse failed: {proc.stderr.strip() or 'unknown error'}"
            return False
    except (subprocess.TimeoutExpired, OSError) as exc:
        MISSING_REPO_ERRORS[app.name] = f"git command failed: {str(exc)[:100]}"
        return False

    # Clear any previous error for this app
    MISSING_REPO_ERRORS.pop(app.name, None)
    return True


def from_drain(cfg: Config, app_name: str | None, limit: int) -> list[WorkItem]:
    """Pull ready tickets. With ``app_name=None`` this spans EVERY app that has a backlog — i.e. all
    connected Jiras — so Autopilot works across several Jira accounts at once. One connection failing
    (bad creds, network, a renamed project) is skipped, never fatal — the other Jiras still drain, and
    the failure is recorded in ``LAST_DRAIN_ERRORS`` so it's visible instead of silent.

    EU-128: Apps with missing/invalid git repos are also skipped and recorded in MISSING_REPO_ERRORS."""
    apps = [cfg.app(app_name)] if app_name else [a for a in cfg.apps if a.backlog_backend != "none"]
    items: list[WorkItem] = []
    for app in apps:
        # EU-128: Skip apps whose git repo is missing/invalid
        if not _validate_git_repo(app):
            # Error already recorded in MISSING_REPO_ERRORS
            continue
        try:
            backlog = make_backlog(app)
            for ticket in backlog.get_ready_tasks(limit):
                items.append((app, ticket))
            LAST_DRAIN_ERRORS.pop(app.name, None)   # a clean fetch clears any prior error
        except Exception as exc:  # noqa: BLE001 - one Jira/connection must not abort the others
            LAST_DRAIN_ERRORS[app.name] = str(exc)[:200]
            print(f"  · backlog '{app.name}' UNREACHABLE this cycle: {str(exc)[:160]}", flush=True)
    # EU-116: drain guard — skip tickets that recently had a no_changes outcome. They're in
    # 'Needs Human' awaiting verification/close, and re-running them would waste another build.
    try:
        from .loop import _recent_no_changes_ticket_ids
        no_changes_ids = _recent_no_changes_ticket_ids(cfg)
        if no_changes_ids:
            # 2026-07-19 stabilization: the old condition `len(items) < len(items) + len(...)` was
            # vacuously true and `skipped` counted the guard SET, not filtered tickets — the log
            # lied. Count the actual delta (and still log when everything was filtered).
            before = len(items)
            items = [(a, t) for (a, t) in items if t.id not in no_changes_ids]
            skipped = before - len(items)
            if skipped:
                print(f"  · EU-116 drain guard: skipped {skipped} ticket(s) with recent no_changes outcome", flush=True)
    except Exception:  # noqa: BLE001 - drain guard must not break the run
        pass
    return items
