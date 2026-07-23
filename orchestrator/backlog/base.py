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

    def create_task(self, summary: str, description: str, labels=None,
                    issue_type: str = "Task", priority: Optional[str] = None,
                    parent: Optional[str] = None) -> Optional[str]:
        """Optional: file a new ticket (officers raising findings). `priority` is an optional
        backend-native priority name (e.g. Jira's "Highest"/"High"/"Medium"/"Low"); a None/absent
        value leaves the backend's own default untouched. `parent` (EU-301) links the new ticket to
        an Epic (team-managed `parent` field). Default: not supported."""
        return None

    def find_open_by_summary(self, summary: str) -> Optional[str]:
        """Optional: find an open ticket with this summary, for de-dup. Default: None."""
        return None

    def find_open_by_label(self, label: str) -> Optional[str]:
        """Optional: find an open ticket carrying ``label`` — the SUBJECT-level de-dup key
        (2026-07-21). Exact-title matching alone let six differently-worded reports of one
        problem through (EU-409..414); filing now stamps a subject fingerprint label and looks
        THAT up first. Default: not supported (callers fall back to the title match)."""
        return None

    def set_labels(self, key: str, add: tuple = (), remove: tuple = ()) -> bool:
        """Optional: add and/or remove labels on an EXISTING ticket — non-clobbering (only the
        named labels change; every other label, the officer label, assignee, etc. are untouched).
        EU-439: ``relabel_fingerprint`` uses this to correct a mis-stamped subject fingerprint
        (the EU-422/423/424 collision). Default: not supported (returns False)."""
        return False

    def comments(self, key: str) -> list:
        """Optional: all human-visible comments on a ticket, oldest -> newest. The read half of a
        decision round-trip (the loop posts a question, the Commander answers in a comment).
        Default: not supported."""
        return []

    def latest_answer(self, ticket: Ticket) -> Optional[str]:
        """Optional: the most recent HUMAN comment on the ticket (the Commander's answer), skipping
        the unit's own comments, as plain text. Default: not supported."""
        return None

    def status_category(self, key: str) -> Optional[str]:
        """Optional: the ticket's statusCategory key ('new' | 'indeterminate' | 'done'), or None when
        it can't be determined (unreachable board, unknown issue, no backend). The branch-retirement
        sweep (EU-426) treats None as 'do not prune' — an unknown status must never authorise a delete.
        Default: not supported (None → fail closed)."""
        return None

    def latest_builder_comment(self, key: str) -> Optional[str]:
        """Optional: the most recent [General]-prefixed comment posted by the unit itself (Builder
        next-step instructions, CI guardrail handoffs, escalation notes), as plain text.
        Used by the CTO chat to surface the concrete action for the Commander. Default: not supported."""
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
