"""Backlog adapter interface — the loop talks only to this, never a vendor SDK.

Swap Jira <-> Notion <-> none per app from config without touching the loop.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..contracts import Ticket


class BacklogAdapter(ABC):
    @abstractmethod
    def get_ready_tasks(self, limit: int) -> list[Ticket]:
        """Return up to `limit` ready tickets, highest priority first."""

    @abstractmethod
    def get_task(self, key: str) -> Ticket:
        """Fetch one ticket by its key/id."""

    @abstractmethod
    def set_status(self, ticket: Ticket, status: str) -> None:
        """Transition a ticket. `status` is a logical name ('In Progress',
        'In Review', 'Needs Human') the adapter maps to the backend workflow."""

    @abstractmethod
    def add_comment(self, ticket: Ticket, body: str) -> None:
        ...

    def attach_pr(self, ticket: Ticket, pr_url: str) -> None:
        """Optional: link a PR to the ticket. Default no-op."""
        return None


class NoneBacklog(BacklogAdapter):
    """For apps with no tracker (ad-hoc / free-text only)."""

    def get_ready_tasks(self, limit: int) -> list[Ticket]:
        return []

    def get_task(self, key: str) -> Ticket:
        raise RuntimeError("this app has backlog_backend: none — pass work via `task`, not `ticket`/`drain`")

    def set_status(self, ticket: Ticket, status: str) -> None:
        return None

    def add_comment(self, ticket: Ticket, body: str) -> None:
        return None


def make_backlog(app) -> BacklogAdapter:
    if app.backlog_backend == "jira":
        from .jira import JiraAdapter
        return JiraAdapter(app)
    if app.backlog_backend == "notion":
        from .notion import NotionAdapter
        return NotionAdapter(app)
    if app.backlog_backend == "none":
        return NoneBacklog()
    raise ValueError(f"unknown backlog_backend: {app.backlog_backend}")
