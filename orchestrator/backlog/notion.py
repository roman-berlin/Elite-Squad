"""Notion backlog adapter (stub).

Implements the same BacklogAdapter interface as Jira so you can switch backends
per app. Fill in the methods against the Notion API:

  - get_ready_tasks : query a database (filter Status == ready_status, sort by a
                      priority/order property, page_size=limit). Map each page's
                      title -> summary, a rich-text prop -> description, and a
                      multi-select/rich-text -> acceptance_criteria.
  - get_task        : retrieve one page by id and map it the same way.
  - set_status      : PATCH the page's Status property.
  - add_comment     : POST /v1/comments with the page_id as parent.

Auth: NOTION_TOKEN from the environment; database id from app.backlog['database_id'].
Docs: https://developers.notion.com/reference
"""
from __future__ import annotations

from ..contracts import Ticket
from .base import BacklogAdapter


class NotionAdapter(BacklogAdapter):
    def __init__(self, app):
        self.app = app
        self.backlog = app.backlog

    def get_ready_tasks(self, limit: int) -> list[Ticket]:
        raise NotImplementedError("NotionAdapter.get_ready_tasks — see module docstring.")

    def get_task(self, key: str) -> Ticket:
        raise NotImplementedError("NotionAdapter.get_task — retrieve a page by id.")

    def set_status(self, ticket: Ticket, status: str) -> None:
        raise NotImplementedError("NotionAdapter.set_status — PATCH the Status property.")

    def add_comment(self, ticket: Ticket, body: str) -> None:
        raise NotImplementedError("NotionAdapter.add_comment — POST /v1/comments.")
