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


def from_tickets(cfg: Config, app_name: str, keys: list[str]) -> list[WorkItem]:
    app = cfg.app(app_name)
    backlog = make_backlog(app)
    return [(app, backlog.get_task(extract_key(k))) for k in keys]


def from_drain(cfg: Config, app_name: str | None, limit: int) -> list[WorkItem]:
    apps = [cfg.app(app_name)] if app_name else [a for a in cfg.apps if a.backlog_backend != "none"]
    items: list[WorkItem] = []
    for app in apps:
        backlog = make_backlog(app)
        for ticket in backlog.get_ready_tasks(limit):
            items.append((app, ticket))
    return items
