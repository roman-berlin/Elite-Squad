"""Jira Cloud backlog adapter (REST API v3).

Auth is HTTP basic with an Atlassian email + API token. By default these come from
JIRA_EMAIL / JIRA_API_TOKEN, but each app names its own env vars (email_env / token_env)
so SEVERAL Jira sites/accounts can run side by side — one per app/project. Per-app settings
come from the app's `backlog:` block:

  base_url:        https://your-domain.atlassian.net
  project_key:     AUTO
  email_env:       JIRA_EMAIL          # which env var holds this connection's email (default)
  token_env:       JIRA_API_TOKEN      # which env var holds this connection's API token (default)
  ready_status:    "To Do"            # status marking a ticket ready for autodev
  label:           autodev            # only pick tickets carrying this label
  jql:             "<override>"        # optional full JQL, overrides the above
  acceptance_criteria_field: customfield_10xxx   # optional custom field id
  status_map:                          # logical -> workflow status names
    In Progress: "In Progress"
    In Review:   "In Review"
    Needs Human: "Needs Triage"
"""
from __future__ import annotations

import os
from typing import Any

import requests

from ..contracts import Ticket
from .base import BacklogAdapter

# Roman Berlin's Atlassian accountId. The unit only ever works and files Roman's tickets (Standing
# Order), so every ticket the unit creates is pinned to him — even when the board/connection configures
# no explicit assignee. Without this, Jira leaves a created issue assigned to the API-token owner (the
# implicit currentUser()), so it never lands in Roman's queue.
ROMAN_ACCOUNT_ID = "70121:051c9744-3c4d-4dfb-b2e5-d7a0e87c2443"


class JiraAdapter(BacklogAdapter):
    def __init__(self, app):
        b = app.backlog
        self.app_name = app.name
        self.base_url = (b.get("base_url") or "").rstrip("/")
        self.project = b.get("project_key")
        self.ready_status = b.get("ready_status", "To Do")
        self.label = b.get("label", "autodev")
        self.only_mine = b.get("only_mine", True)        # assignee = currentUser()
        self.assignee = b.get("assignee")                # explicit assignee accountId; overrides currentUser()
        self.require_label = b.get("require_label", False)  # also require the label?
        self.jql_override = b.get("jql")
        self.ac_field = b.get("acceptance_criteria_field")
        self.status_map = b.get("status_map", {})
        self.fetch_images = b.get("fetch_images", True)   # download ticket image attachments for the Builder
        # Queue order: resume In Progress first, then pull the ready column (To Do),
        # each ordered by board Rank (top first). Override with `queue_statuses:` in config.
        self.queue_statuses = b.get("queue_statuses") or ["In Progress", self.ready_status]
        self.session = requests.Session()
        # Credentials, in priority order:
        #  1) a cockpit "connection" assigned to this project (the quick-connect store) — base_url +
        #     project + email/token come straight from it.
        #  2) the per-app env-var path (email_env / token_env) — the original mechanism, untouched.
        conn = None
        try:
            from .. import connections
            conn = connections.for_app(self.app_name)
        except Exception:  # noqa: BLE001 - a bad store must never break a configured app
            conn = None
        if conn:
            self.base_url = (conn.get("base_url") or self.base_url).rstrip("/")
            self.project = conn.get("project_key") or self.project
            self.session.auth = (conn.get("email", ""), conn.get("token", ""))
            # A connection can pin an assignee so future boards inherit it (quick-connect stores
            # Roman by default); app config still wins if it set one explicitly.
            self.assignee = self.assignee or conn.get("assignee")
        else:
            email_env = b.get("email_env", "JIRA_EMAIL")
            token_env = b.get("token_env", "JIRA_API_TOKEN")
            try:
                self.session.auth = (os.environ[email_env], os.environ[token_env])
            except KeyError as exc:
                raise RuntimeError(
                    f"Jira credentials for app '{self.app_name}' are not set — missing env var {exc}. "
                    f"Set {email_env} and {token_env}, or connect a Jira in the cockpit (Jira → "
                    "Quick connect)."
                ) from exc
        if not self.base_url:
            raise RuntimeError(
                f"No Jira base_url for app '{self.app_name}' — set `base_url` in config or connect a "
                "Jira in the cockpit (Jira → Quick connect).")
        self.session.headers.update({"Accept": "application/json",
                                     "Content-Type": "application/json"})

    # -- helpers ---------------------------------------------------------- #
    def _url(self, path: str) -> str:
        # EU-83: '#' in a REST path means an internal decision-entry suffix (e.g. '#out-of-scope')
        # has leaked into a Jira URL — that produces 404/405. Catch it here as a hard error so the
        # regression is visible in tests and doesn't silently drop findings in production.
        if "#" in path:
            raise ValueError(
                f"Jira REST path contains '#': {path!r}. An internal decision-entry suffix "
                "(e.g. '#out-of-scope') has leaked into the REST URL. Strip it before the call "
                "(see decisions.to_worklist / EU-83)."
            )
        return f"{self.base_url}/rest/api/3/{path.lstrip('/')}"

    def _fields(self) -> list[str]:
        fields = ["summary", "description", "status", "comment", "labels", "issuetype", "attachment"]
        if self.ac_field:
            fields.append(self.ac_field)
        return fields

    def _jql_for_status(self, status: str) -> str:
        clauses = []
        if self.project:
            clauses.append(f'project = "{self.project}"')
        clauses.append(f'status = "{status}"')
        if self.assignee:
            clauses.append(f'assignee = "{self.assignee}"')   # pinned to a specific person (accountId)
        elif self.only_mine:
            clauses.append("assignee = currentUser()")        # = the API-token owner's account
        if self.require_label and self.label:
            clauses.append(f'labels = "{self.label}"')
        return " AND ".join(clauses) + " ORDER BY Rank ASC"     # board order, top first

    def _raise_if_unauthenticated(self, resp) -> None:
        """Guard against Jira's auth-blind 200. POST search/jql answers an UNAUTHENTICATED request with
        HTTP 200 and an EMPTY issue list (NOT 401), so an invalid/expired token — or one whose account
        can't reach the site — is indistinguishable from 'no ready tickets': the drain comes back empty,
        raise_for_status() passes, and the cockpit/autopilot silently reports 'queue clear - nothing of
        yours'. Atlassian still flags the real state in the Seraph login-reason header even on that 200,
        so raise on it and let from_drain record the board as UNREACHABLE (visible) instead of clear
        (silent). Real cause this caught: a dead JIRA_API_TOKEN hiding EU's whole To Do column."""
        reason = (resp.headers.get("X-Seraph-LoginReason") or "").upper()
        if "FAILED" in reason or "DENIED" in reason:
            raise RuntimeError(
                f"Jira auth failed for app '{self.app_name}' (X-Seraph-LoginReason={reason}) - the API "
                f"token is invalid/expired or its account can't access {self.base_url}. Rotate "
                f"JIRA_API_TOKEN (id.atlassian.com -> Security -> API tokens) or reconnect this Jira in "
                f"the cockpit. Jira answers an unauthenticated search with HTTP 200 and no issues, so "
                f"this otherwise masquerades as 'queue clear - nothing of yours'.")

    # -- interface -------------------------------------------------------- #
    def get_ready_tasks(self, limit: int) -> list[Ticket]:
        """Resume In Progress first, then pull To Do top-to-bottom (board Rank),
        assignee = you. A full `jql:` override, if set, replaces this entirely.
        (Jira Cloud search endpoint; older instances: POST /rest/api/3/search.)"""
        queries = [self.jql_override] if self.jql_override else \
            [self._jql_for_status(s) for s in self.queue_statuses]
        out: list[Ticket] = []
        seen: set[str] = set()
        for jql in queries:
            if len(out) >= limit:
                break
            resp = self.session.post(self._url("search/jql"), json={
                "jql": jql, "maxResults": max(1, limit - len(out)), "fields": self._fields(),
            })
            resp.raise_for_status()
            self._raise_if_unauthenticated(resp)   # a 200-empty here can mean 'bad token', not 'no work'
            for issue in resp.json().get("issues", []):
                t = self._to_ticket(issue)
                if t.key not in seen:
                    seen.add(t.key)
                    out.append(t)
        return out[:limit]

    def get_task(self, key: str) -> Ticket:
        resp = self.session.get(self._url(f"issue/{key}"),
                                params={"fields": ",".join(self._fields())})
        resp.raise_for_status()
        return self._to_ticket(resp.json())

    def comments(self, key: str) -> list[dict[str, Any]]:
        """All comments on an issue, oldest -> newest (GET issue/{key}/comment). The read half of a
        decision round-trip: the loop posts a question as a comment, the Commander answers in one."""
        resp = self.session.get(self._url(f"issue/{key}/comment"))
        resp.raise_for_status()
        return list((resp.json() or {}).get("comments", []) or [])

    def latest_answer(self, ticket) -> str | None:
        """The most recent HUMAN comment on the ticket — the Commander's answer in a decision
        round-trip — as plain text, or None. Skips the unit's own '[General]'-prefixed comments so a
        question the loop just posted is never mistaken for the reply."""
        key = getattr(ticket, "key", ticket)
        for c in reversed(self.comments(key)):
            txt = _adf_to_text(c.get("body"))
            if txt and not txt.strip().startswith("[General]"):
                return txt.strip()
        return None

    def latest_builder_comment(self, key: str) -> str | None:
        """The most recent [General]-prefixed comment posted by the unit — Builder next-step
        instructions, CI guardrail handoffs, escalation notes. Used by the CTO chat to surface
        the concrete action for the Commander when they ask 'what do I need to do about X?'"""
        for c in reversed(self.comments(key)):
            txt = _adf_to_text(c.get("body"))
            if txt and txt.strip().startswith("[General]"):
                return txt.strip()
        return None

    def _current_status(self, key: str) -> str | None:
        try:
            r = self.session.get(self._url(f"issue/{key}"), params={"fields": "status"})
            r.raise_for_status()
            return ((r.json().get("fields", {}) or {}).get("status", {}) or {}).get("name")
        except requests.RequestException:
            return None

    def set_status(self, ticket: Ticket, status: str) -> None:
        target = self.status_map.get(status, status)
        # Already in the target column (e.g. resuming an In Progress ticket)? No-op, no comment.
        current = self._current_status(ticket.key)
        if current and current.lower() == target.lower():
            return
        tr = self.session.get(self._url(f"issue/{ticket.key}/transitions"))
        tr.raise_for_status()
        match = next((t for t in tr.json().get("transitions", [])
                      if t["to"]["name"].lower() == target.lower()), None)
        if not match:
            self.add_comment(ticket, f"[autodev] No transition to '{target}' available "
                                     f"from '{current or 'current status'}'; please move it manually.")
            return
        self.session.post(self._url(f"issue/{ticket.key}/transitions"),
                          json={"transition": {"id": match["id"]}}).raise_for_status()

    def add_comment(self, ticket: Ticket, body: str) -> None:
        # Prefix so the CTO's own comments can be told apart from the Commander's.
        self.session.post(self._url(f"issue/{ticket.key}/comment"),
                          json={"body": _adf("[General] " + body)}).raise_for_status()

    def attach_pr(self, ticket: Ticket, pr_url: str) -> None:
        try:
            self.session.post(self._url(f"issue/{ticket.key}/remotelink"), json={
                "object": {"url": pr_url, "title": "Pull request"}
            }).raise_for_status()
        except requests.RequestException:
            self.add_comment(ticket, f"PR: {pr_url}")

    # -- filing (officers raise their own tickets) ------------------------ #
    def create_task(self, summary: str, description: str, labels=None,
                    issue_type: str = "Task") -> str | None:
        fields: dict[str, Any] = {
            "project": {"key": self.project},
            "summary": summary[:240],
            "issuetype": {"name": issue_type},
            "description": _adf(description or summary),
        }
        # Always pin an assignee: the configured one, else Roman by default. Leaving it unset makes
        # Jira fall back to the token owner (implicit currentUser()), so tickets the unit files would
        # never reach Roman's queue.
        fields["assignee"] = {"accountId": self.assignee or ROMAN_ACCOUNT_ID}
        if labels:
            fields["labels"] = [str(l).replace(" ", "-") for l in labels]
        r = self.session.post(self._url("issue"), json={"fields": fields})
        r.raise_for_status()
        return r.json().get("key")

    def find_open_by_summary(self, summary: str) -> str | None:
        """Return an OPEN ticket with a matching summary (de-dup), else None."""
        q = summary.replace('"', " ").replace("\\", " ").strip()[:120]
        if not q:
            return None
        try:
            r = self.session.post(self._url("search/jql"), json={
                "jql": f'project = "{self.project}" AND statusCategory != Done AND summary ~ "{q}"',
                "maxResults": 5, "fields": ["summary"]})
            r.raise_for_status()
            for it in r.json().get("issues", []):
                if ((it.get("fields", {}) or {}).get("summary", "")).strip().lower() == summary.strip().lower():
                    return it.get("key")
        except requests.RequestException:
            return None
        return None

    def _download_images(self, key: str, attachments: list[dict[str, Any]]) -> list[str]:
        """Download image attachments to a local cache so the Builder's Read tool can view them.
        Best-effort: any failure (no creds, network, odd mime) is skipped, never raised."""
        if not getattr(self, "fetch_images", True) or not attachments:
            return []
        import re
        import tempfile
        from pathlib import Path
        dest = Path(tempfile.gettempdir()) / "general-ticket-images" / key
        out: list[str] = []
        for a in attachments:
            if not str(a.get("mimeType") or "").lower().startswith("image/"):
                continue
            url = a.get("content")
            if not url:
                continue
            fname = re.sub(r"[^A-Za-z0-9._-]", "_", str(a.get("filename") or a.get("id") or "image"))
            try:
                dest.mkdir(parents=True, exist_ok=True)
                p = dest / fname
                if not p.exists() or p.stat().st_size == 0:
                    r = self.session.get(url, timeout=30)
                    r.raise_for_status()
                    p.write_bytes(r.content)
                out.append(str(p))
            except Exception:  # noqa: BLE001 - image fetch is best-effort
                continue
        return out

    # -- parsing ---------------------------------------------------------- #
    def _to_ticket(self, issue: dict[str, Any]) -> Ticket:
        f = issue.get("fields", {})
        description = _adf_to_text(f.get("description"))
        ac: list[str] = []
        if self.ac_field and f.get(self.ac_field):
            ac = _split_criteria(_adf_to_text(f[self.ac_field]))
        if not ac:
            ac = _criteria_from_description(description)
        # Bring ALL of the Commander's comments into context (skip the CTO's own) — these carry
        # the QA feedback when a ticket bounces back from QA to To Do.
        feedback = []
        for c in (f.get("comment", {}) or {}).get("comments", []) or []:
            txt = _adf_to_text(c.get("body"))
            if txt and not txt.strip().startswith("[General]"):
                feedback.append(txt.strip())
        if feedback:
            description += ("\n\nCommander's comments (oldest -> newest) — read ALL of these; they "
                           "include QA feedback on what to fix:\n" + "\n---\n".join(feedback))[:8000]
        # Download image attachments so the Builder can SEE the mockups/screenshots, not guess.
        imgs = self._download_images(issue["key"], f.get("attachment") or [])
        if imgs:
            description += ("\n\nTicket images — OPEN and VIEW each (they show the desired design / "
                           "the bug); do not guess at visuals:\n" + "\n".join(f"- {p}" for p in imgs))
        return Ticket(
            id=issue["key"],
            key=issue["key"],
            summary=f.get("summary", ""),
            description=description,
            acceptance_criteria=ac,
            url=f"{self.base_url}/browse/{issue['key']}",
            app=self.app_name,
            labels=list(f.get("labels") or []),
            issue_type=((f.get("issuetype") or {}) or {}).get("name"),
        )


# --------------------------------------------------------------------------- #
# Atlassian Document Format helpers
# --------------------------------------------------------------------------- #
def _adf(text: str) -> dict:
    """Wrap plain text in a minimal ADF document for comments."""
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph",
                         "content": [{"type": "text", "text": text}]}]}


def _adf_to_text(node: Any) -> str:
    """Flatten an ADF node tree to plain text (newline per block)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    out: list[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            if n.get("type") == "text":
                out.append(n.get("text", ""))
            for child in n.get("content", []) or []:
                walk(child)
            # listItem excluded: its child paragraph already emits the newline.
            if n.get("type") in ("paragraph", "heading"):
                out.append("\n")
        elif isinstance(n, list):
            for child in n:
                walk(child)

    walk(node)
    return "".join(out).strip()


def _split_criteria(text: str) -> list[str]:
    lines = [l.strip(" -*•\t") for l in text.splitlines()]
    return [l for l in lines if l]


def _criteria_from_description(description: str) -> list[str]:
    """Pull bullets under an 'Acceptance Criteria' heading in the description."""
    out: list[str] = []
    capturing = False
    for line in description.splitlines():
        if "acceptance criteria" in line.strip().lower():
            capturing = True
            continue
        if capturing:
            s = line.strip()
            if not s:
                if out:
                    break
                continue
            out.append(s.strip(" -*•\t"))
    return out
