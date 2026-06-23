"""Intake — turn a request into a worklist of (app, ticket) pairs.

Three ways to feed work in:
  - text   : a free-text bug/feature -> an ad-hoc (ephemeral) ticket, no tracker needed
  - ticket : one or more existing Jira keys
  - drain  : pull ready tickets from an app's backlog (or every backlogged app)
"""
from __future__ import annotations

import re

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


def from_drain(cfg: Config, app_name: str | None, limit: int) -> list[WorkItem]:
    """Pull ready tickets. With ``app_name=None`` this spans EVERY app that has a backlog — i.e. all
    connected Jiras — so Autopilot works across several Jira accounts at once. One connection failing
    (bad creds, network, a renamed project) is skipped, never fatal — the other Jiras still drain, and
    the failure is recorded in ``LAST_DRAIN_ERRORS`` so it's visible instead of silent."""
    apps = [cfg.app(app_name)] if app_name else [a for a in cfg.apps if a.backlog_backend != "none"]
    items: list[WorkItem] = []
    for app in apps:
        try:
            backlog = make_backlog(app)
            for ticket in backlog.get_ready_tasks(limit):
                items.append((app, ticket))
            LAST_DRAIN_ERRORS.pop(app.name, None)   # a clean fetch clears any prior error
        except Exception as exc:  # noqa: BLE001 - one Jira/connection must not abort the others
            LAST_DRAIN_ERRORS[app.name] = str(exc)[:200]
            print(f"  · backlog '{app.name}' UNREACHABLE this cycle: {str(exc)[:160]}", flush=True)
    return items
