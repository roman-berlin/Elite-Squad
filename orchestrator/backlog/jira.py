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

import requests

from ..audit import AuditLog
from ..contracts import Ticket
# EU-456: the appended-comment / appended-image sentinels are owned by filing.py (the stripper) so
# the writer (_to_ticket below) and the stripper (_clean_ticket_description) share ONE source of
# truth and can't drift on the appended wording — the drift that would silently re-break relabel.
from ..filing import _COMMANDER_COMMENTS_PREFIX, _TICKET_IMAGES_PREFIX
from .base import BacklogAdapter

# Roman Berlin's Atlassian accountId. The unit only ever works and files Roman's tickets (Standing
# Order), so every ticket the unit creates is pinned to him — even when the board/connection configures
# no explicit assignee. Without this, Jira leaves a created issue assigned to the API-token owner (the
# implicit currentUser()), so it never lands in Roman's queue.
ROMAN_ACCOUNT_ID = "70121:051c9744-3c4d-4dfb-b2e5-d7a0e87c2443"

# EU-358: requests has NO default timeout, so a single half-open connection to Atlassian used to
# block the drain thread forever (every adapter call runs inline in the autopilot cycle).
_HTTP_TIMEOUT = float(os.environ.get("JIRA_HTTP_TIMEOUT", "30") or 30)

# Jira hard-caps an issue summary at 255 chars; we store 240 to leave room. EU-365: the cap has to be
# applied in exactly ONE place, because create_task's stored title is what find_open_by_summary later
# compares against — see _summary_for_jira.
_SUMMARY_MAX = 240

# Jira's comment endpoint pages (default 50/page, oldest-first). Ask for the biggest page it serves
# and bound the walk: 20 x 100 = 2000 comments is far past any real decision thread, and the cap is
# what stops a paging bug (or a backend that ignores startAt) from hanging the drain thread — every
# adapter call runs inline in the autopilot cycle (same reasoning as _HTTP_TIMEOUT above).
_COMMENT_PAGE = 100
_COMMENT_MAX_PAGES = 20

# EU-397: 133 merges, ZERO audited transitions — only the post-merge call site logged anything
# (merge_transition_failed), so board-vs-Jira drift was undiagnosable everywhere else. Instrumented
# HERE, at the adapter's one write choke-point, instead of at each of set_status's many call sites.
# Tests substitute this module attribute directly with a fake sink (`jira._AUDIT_LOG = fake`) — the
# guard below short-circuits before any real file touches disk.
_AUDIT_LOG: AuditLog | None = None

# EU-425: the audit_path the LIVE process resolved from its loaded Config INSTANCE — handed in once
# at boot by configure_audit_path() (main.py / server.py, right where they build their own
# AuditLog(cfg.audit_path)). JiraAdapter is built from `app` alone (see base.make_backlog), so no cfg
# ever reaches the constructor; without this hook _audit() falls back to Config.audit_path — the
# dataclass CLASS default './state/audit.jsonl' — which only ever matched the live ledger because both
# example configs set audit_path to that same default. An overridden audit_path in a live config would
# otherwise route ticket_transition events to a DIFFERENT file than the rest of the process writes to,
# silently re-breaking the board-vs-Jira traceability EU-397 exists to restore. Tests never boot, so
# this stays None under run_all and GENERAL_AUDIT_PATH governs isolation there.
_RESOLVED_AUDIT_PATH: str | None = None


def configure_audit_path(path: str | None) -> None:
    """Anchor the adapter's audit sink to the same resolved ``audit_path`` the live process uses.

    Call once at process start (main.py / server.py), alongside ``agent.configure_audit``. ``path``
    is ``cfg.audit_path`` — the loaded Config INSTANCE value, NOT the dataclass class default
    ``_audit()`` used to read on its own. Idempotent: a repeat call with the same path is a no-op;
    a CHANGED path drops the cached sink so the next ``_audit()`` rebuilds against the new path.
    ``None`` clears it (a reconfigure, or a test reset, falls back to the class default)."""
    global _RESOLVED_AUDIT_PATH, _AUDIT_LOG
    if path == _RESOLVED_AUDIT_PATH:
        return
    _RESOLVED_AUDIT_PATH = path
    _AUDIT_LOG = None   # cached sink was opened against the old/default path — rebuild on next use


def _audit() -> AuditLog:
    global _AUDIT_LOG
    if _AUDIT_LOG is None:
        # Resolution order:
        #   1. GENERAL_AUDIT_PATH env — the highest override. Same contract as GENERAL_PID_FILE
        #      (EU-355): run_all points it at a per-run temp file so NO harness can write to the live
        #      ledger, whether or not it remembered to stub `_AUDIT_LOG`. state_routing_test.py did
        #      not, and every `python3 tests/run_all.py` appended 5 fabricated AUTO-73 ticket_transition
        #      rows; 15 of the 29 rows on record were test fixtures, incl. a `landed: Fertig` from a
        #      German-column case that existed on no board. The dashboard, forensics and the daily
        #      brief all read that file.
        #   2. _RESOLVED_AUDIT_PATH — the LIVE cfg.audit_path handed in by configure_audit_path (EU-425).
        #   3. Config.audit_path — the dataclass class-level default; the pre-EU-425 behaviour, kept
        #      as the fallback for any entry point that does not call configure_audit_path.
        env_path = os.environ.get("GENERAL_AUDIT_PATH", "").strip()
        if env_path:
            path: str = env_path
        elif _RESOLVED_AUDIT_PATH:
            path = _RESOLVED_AUDIT_PATH
        else:
            try:
                from ..config import Config
                path = Config.audit_path
            except Exception:  # noqa: BLE001 - the ledger must never block a transition
                path = "./state/audit.jsonl"
        _AUDIT_LOG = AuditLog(path)
    return _AUDIT_LOG


class BacklogSearchError(RuntimeError):
    """A de-dup search could not be COMPLETED — the answer is 'unknown', not 'no duplicate'.

    EU-365 (2026-07-16 total audit): find_open_by_summary used to swallow every RequestException and
    return None. filing.py:109 reads None as "nothing open matches" and files, so one 5xx from
    Atlassian minted a duplicate of every finding in the batch — silently defeating the de-dup EU-42
    depends on. Raising instead makes the search failure fail-CLOSED: filing.py's per-finding
    `except Exception` records it in FilingResult.failed (escalated, never buried) and skips the
    create, so a search hiccup under-files visibly rather than duplicating invisibly.
    """


def _summary_for_jira(summary: str) -> str:
    """The EXACT title create_task persists — the one string both the write and the de-dup compare
    must agree on. EU-365: create_task stored `summary[:240]` while find_open_by_summary compared the
    FULL summary, so any finding with a >240-char title could never match the ticket it had itself
    filed and re-filed on every run."""
    return (summary or "").strip()[:_SUMMARY_MAX]


def _with_default_timeout(session):
    """Make every request on ``session`` carry a default timeout (EU-358). Call sites may still
    pass their own ``timeout=``; the point is that FORGETTING one can no longer hang the autopilot.
    Wraps (not subclasses) so the many test harnesses that stub ``requests`` with a bare fake —
    whose Session isn't a real class, or whose fake session has no ``request`` — keep working."""
    try:
        orig = session.request
    except AttributeError:        # a test stub whose .get/.post don't route through .request
        return session
    def request(method: str, url: str, **kwargs: object) -> requests.Response:
        kwargs.setdefault("timeout", _HTTP_TIMEOUT)
        return orig(method, url, **kwargs)
    session.request = request
    return session


# EU-783: Jira priority → numeric rank for tiebreak ordering (ascending = higher priority first).
# Unknown / None maps to 2 (the "medium" slot) so untagged tickets never outrank a known tier —
# they stay mutually ordered by Python's stable sort (i.e. board Rank).
_PRIORITY_RANK: dict[str, int] = {
    "highest": 0, "high": 1, "medium": 2, "low": 3, "lowest": 4,
}


def _tiebreak_autofiled(tickets: list[Ticket]) -> list[Ticket]:
    """Return *tickets* sorted within their Jira priority tier: Commander tickets (no 'autofiled'
    label) drain before autofiled ones at equal priority. Preserves relative order for tickets that
    share both (priority, autofiled) — Python's stable sort guarantees this, which means board
    Rank-order is preserved inside each slice.

    Applied PER QUERY-BLOCK in ``get_ready_tasks`` so the In-Progress-before-To-Do resume-first
    invariant (EU-252) is unaffected — sorting the merged list would let a high-priority To Do
    jump a low-priority In Progress.
    """
    return sorted(
        tickets,
        key=lambda t: (_PRIORITY_RANK.get((getattr(t, 'priority', None) or '').lower(), 2),
                       1 if 'autofiled' in (getattr(t, 'labels', None) or []) else 0),
    )


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
        # Per-board transition fallbacks: when the primary target status doesn't exist on this
        # board, try these in order (logical names; each is re-mapped through status_map). Default
        # covers the QA hand-off column being absent — land → QA falls back to Done/Closed.
        self.status_fallbacks = {str(k): list(v) for k, v in
                                 (b.get("status_fallbacks") or {"QA": ["Done", "Closed"]}).items()}
        self.fetch_images = b.get("fetch_images", True)   # download ticket image attachments for the Builder
        # Queue order: resume In Progress first, then pull the ready column (To Do),
        # each ordered by board Rank (top first). Override with `queue_statuses:` in config.
        self.queue_statuses = b.get("queue_statuses") or ["In Progress", self.ready_status]
        self.session = _with_default_timeout(requests.Session())
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
        fields = ["summary", "description", "status", "comment", "labels", "issuetype", "attachment", "priority"]
        if self.ac_field:
            fields.append(self.ac_field)
        return fields

    def _jql_for_status(self, status: str) -> str:
        # `status` is a LOGICAL name ("In Progress", the ready column) — map it to this board's
        # workflow name exactly as set_status does. EU-365: only the WRITE path mapped, so a board
        # whose status_map renames the column ("In Progress" -> "In Development") was queried for a
        # status it doesn't have: Jira answers 0 issues, raise_for_status passes, and the drain
        # reports "queue clear" while the whole column sits there. Same silent-empty class as the
        # auth-blind 200 below. getattr: several harnesses inject adapters with no status_map.
        status = getattr(self, "status_map", {}).get(status, status)
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
        # EU-344 root cause: this ordered by Rank ASC (manual board drag-order) ONLY, so the
        # priority field was never honoured — Highest tickets sat behind lower-Rank Medium/Low ones,
        # violating the base.py "highest priority first" contract. (EU-344 fixed the *picker* on the
        # false premise that the adapter already emitted priority order; it never did — Rank ASC has
        # been here since the initial commit.) `priority DESC` = Highest→Lowest (Jira's urgent-first
        # idiom); Rank ASC is the tiebreaker so board order still decides among equal-priority
        # tickets. Applied PER status query, so the In-Progress-first / To-Do-second resume split
        # (EU-252) is unaffected — In Progress is still drawn before To Do regardless of priority.
        return " AND ".join(clauses) + " ORDER BY priority DESC, Rank ASC"

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
        assignee = you. A full `jql:` override, if set, replaces the To-Do/ready query — but a
        PRECEDING In-Progress-only query still runs first regardless. Without it, a custom override's
        ORDER BY (e.g. "priority DESC, Rank ASC") can rank an In Progress fragment below `limit`, so
        it never enters the drawn window at all and the tier-1 resume-first split in autopilot.py
        becomes a no-op — this stranded EU-233..237 for 29h behind newer To Do filings (EU-252).
        Results are deduped by key across both queries.
        (Jira Cloud search endpoint; older instances: POST /rest/api/3/search.)"""
        queries = [self._jql_for_status("In Progress"), self.jql_override] if self.jql_override else \
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
            # EU-783: collect deduped tickets into a batch, then sort so Commander tickets (no
            # 'autofiled' label) drain before autofiled ones within the same Jira priority tier.
            batch: list[Ticket] = []
            for issue in resp.json().get("issues", []):
                t = self._to_ticket(issue)
                if t.key not in seen:
                    seen.add(t.key)
                    batch.append(t)
            out.extend(_tiebreak_autofiled(batch))
        return out[:limit]

    def get_task(self, key: str) -> Ticket:
        resp = self.session.get(self._url(f"issue/{key}"),
                                params={"fields": ",".join(self._fields())})
        resp.raise_for_status()
        return self._to_ticket(resp.json())

    def comments(self, key: str) -> list[dict[str, object]]:
        """All comments on an issue, oldest -> newest (GET issue/{key}/comment). The read half of a
        decision round-trip: the loop posts a question as a comment, the Commander answers in one.

        EU-365: this used to read ONE page. Jira returns comments oldest-first, 50 per page, so on a
        long decision thread the Commander's newest reply fell off the end and latest_answer() —
        which reads the LAST comment — handed the loop a stale answer to act on. Page to the end."""
        out: list[dict[str, object]] = []
        start = 0
        for _ in range(_COMMENT_MAX_PAGES):
            resp = self.session.get(self._url(f"issue/{key}/comment"),
                                    params={"startAt": start, "maxResults": _COMMENT_PAGE})
            resp.raise_for_status()
            body = resp.json() or {}
            page = list(body.get("comments") or [])
            out.extend(page)
            start += len(page)
            total = body.get("total")
            # Stop on: a short/empty page, the reported total reached, or no total at all (a backend
            # or fake that omits it — re-reading page 0 forever is the failure to avoid).
            if not page or not isinstance(total, int) or start >= total:
                break
        return out

    def latest_answer(self, ticket) -> str | None:
        """The most recent HUMAN comment on the ticket — the Commander's answer in a decision
        round-trip — as plain text, or None. Skips the squad's own '[Squad]'/'[General]'-prefixed comments so a
        question the loop just posted is never mistaken for the reply."""
        key = getattr(ticket, "key", ticket)
        for c in reversed(self.comments(key)):
            txt = _adf_to_text(c.get("body"))
            if txt and not txt.strip().startswith(("[Squad]", "[General]")):
                return txt.strip()
        return None

    def latest_builder_comment(self, key: str) -> str | None:
        """The most recent squad-prefixed comment ([Squad], legacy [General]) — Builder next-step
        instructions, CI guardrail handoffs, escalation notes. Used by the CTO chat to surface
        the concrete action for the Commander when they ask 'what do I need to do about X?'"""
        for c in reversed(self.comments(key)):
            txt = _adf_to_text(c.get("body"))
            if txt and txt.strip().startswith(("[Squad]", "[General]")):
                return txt.strip()
        return None

    def _current_status(self, key: str) -> str | None:
        try:
            r = self.session.get(self._url(f"issue/{key}"), params={"fields": "status"})
            r.raise_for_status()
            return ((r.json().get("fields", {}) or {}).get("status", {}) or {}).get("name")
        except requests.RequestException:
            return None

    def status_category(self, key: str) -> str | None:
        """The issue's statusCategory key ('new' | 'indeterminate' | 'done'), or None when it can't be
        determined (unreachable board, unknown issue, auth-blind 200, bad JSON). Used by the
        branch-retirement sweep (EU-426), which prunes an unmerged autodev/<KEY>-* branch ONLY when
        this is 'done' — so a None here means 'do not prune' (fail closed: an unknown status must
        never authorise a delete). statusCategory is read straight off the status object Jira returns
        (same shape epic_children already decodes), so it is robust to board-specific status NAMES
        (Done / Closed / Resolved / Shipped all carry category 'done')."""
        try:
            r = self.session.get(self._url(f"issue/{key}"), params={"fields": "status"})
            r.raise_for_status()
            self._raise_if_unauthenticated(r)   # a 200-empty here can mean 'bad token', not 'no issue'
            status = (r.json().get("fields", {}) or {}).get("status", {}) or {}
            return ((status.get("statusCategory") or {}).get("key")) or None
        except (requests.RequestException, RuntimeError, ValueError):
            return None

    def set_status(self, ticket: Ticket, status: str) -> None:
        # Candidates: the mapped target, then its per-board fallbacks (e.g. QA → Done/Closed for a
        # board that has no QA column). Without the chain, a missing target silently stranded the
        # ticket In Progress with only a "please move it manually" comment — the 2026-07-09 stuck-
        # column incident: every land had been TRYING "QA" and no-oping before the column existed.
        targets = [self.status_map.get(status, status)]
        targets += [self.status_map.get(f, f) for f in self.status_fallbacks.get(status, [])]
        # EU-397: the Jira status name we were actually trying to land on — paired with `landed` in
        # every audit event below, whichever of the four outcomes it takes.
        wanted = targets[0]
        try:
            # Already in ANY candidate column (e.g. resuming an In Progress ticket, or a ticket the
            # Commander already moved to Done)? No-op, no comment — never bounce a ticket backwards.
            current = self._current_status(ticket.key)
            if current and any(current.lower() == t.lower() for t in targets):
                _audit().record("ticket_transition", ticket_id=ticket.id, wanted=wanted,
                                 landed=current, outcome="no-op")
                return
            tr = self.session.get(self._url(f"issue/{ticket.key}/transitions"))
            tr.raise_for_status()
            available = tr.json().get("transitions", [])
            match = next((t for tgt in targets for t in available
                          if t["to"]["name"].lower() == tgt.lower()), None)
            if not match:
                self.add_comment(ticket, f"[autodev] No transition to '{targets[0]}' available "
                                         f"from '{current or 'current status'}'; please move it manually.")
                _audit().record("ticket_transition", ticket_id=ticket.id, wanted=wanted,
                                 landed=current, outcome="no-transition")
                return
            self.session.post(self._url(f"issue/{ticket.key}/transitions"),
                              json={"transition": {"id": match["id"]}}).raise_for_status()
            # 2026-07-19 audit: a FALLBACK landing used to be indistinguishable from the real target —
            # on a board with no QA column, land → QA fell back to Done and the human-QA step vanished
            # with zero trace. The move still happens (better than stranding the ticket), but now it
            # says so on the ticket, so a skipped QA hand-off is visible instead of silent.
            landed = match["to"]["name"]
            if landed.lower() != str(targets[0]).lower():
                self.add_comment(ticket, f"[autodev] Board has no '{targets[0]}' column — moved to "
                                         f"'{landed}' instead (fallback). If '{targets[0]}' matters, "
                                         "add that column to the board.")
                _audit().record("ticket_transition", ticket_id=ticket.id, wanted=wanted,
                                 landed=landed, outcome="fallback")
            else:
                _audit().record("ticket_transition", ticket_id=ticket.id, wanted=wanted,
                                 landed=landed, outcome="matched")
        except Exception as exc:  # noqa: BLE001 - audited, then re-raised unchanged (six call sites
            # still fail-soft around this call exactly as before; this is what makes each miss visible)
            # EU-425: guard the audit write so a ledger failure (disk full, bad perms) can never mask
            # the original transition exception — the re-raise below must always hand the caller the
            # REAL error, not an audit-write error standing in for it. (record() opens/locks a file,
            # so it is genuinely fallible; the success-path record() calls above are NOT guarded
            # because there a ledger error is itself the signal worth surfacing, not a mask.)
            try:
                _audit().record("ticket_transition_failed", ticket_id=ticket.id, wanted=wanted,
                                 error=str(exc)[:300])
            except Exception:  # noqa: BLE001 - the audit sink is best-effort in the failure path
                pass
            raise

    def add_comment(self, ticket: Ticket, body: str) -> None:
        # Prefix so the CTO's own comments can be told apart from the Commander's.
        # 2026-07-23 (Commander: "relevant to ALL comments … effective, brief, human language"):
        # every LONG comment folds through notify.jira_brief at this one choke point — tables,
        # headings and code fences dropped, prose folded to bullets, trimmed at a line boundary
        # (never mid-sentence). Short comments (the majority — merge notes, hand-offs) pass through
        # byte-identical, same passthrough contract as brief_for_phone on the Telegram side. The
        # structured hand-off lines (WHY/BLOCKER/DECISION/OPTIONS/MANUAL TEST) survive verbatim.
        if len(body or "") > 700:
            try:
                from .. import notify
                body = notify.jira_brief(body) or body
            except Exception:  # noqa: BLE001 — formatting must never lose a comment
                pass
        self.session.post(self._url(f"issue/{ticket.key}/comment"),
                          json={"body": _adf("[Squad] " + body)}).raise_for_status()

    def attach_pr(self, ticket: Ticket, pr_url: str) -> None:
        try:
            self.session.post(self._url(f"issue/{ticket.key}/remotelink"), json={
                "object": {"url": pr_url, "title": "Pull request"}
            }).raise_for_status()
        except requests.RequestException:
            self.add_comment(ticket, f"PR: {pr_url}")

    # -- filing (officers raise their own tickets) ------------------------ #
    def create_task(self, summary: str, description: str, labels=None,
                    issue_type: str = "Task", priority: str | None = None,
                    parent: str | None = None) -> str | None:
        fields: dict[str, object] = {
            "project": {"key": self.project},
            "summary": _summary_for_jira(summary),
            "issuetype": {"name": issue_type},
            "description": _adf(description or summary),
        }
        # EU-301: link a child Task to its Epic via the team-managed `parent` field (these boards are
        # simplified/next-gen — children group under an Epic by `parent`, not the classic epic-link).
        if parent:
            fields["parent"] = {"key": parent}
        # Always pin an assignee: the configured one, else Roman by default. Leaving it unset makes
        # Jira fall back to the token owner (implicit currentUser()), so tickets the unit files would
        # never reach Roman's queue.
        fields["assignee"] = {"accountId": self.assignee or ROMAN_ACCOUNT_ID}
        if labels:
            fields["labels"] = [str(l).replace(" ", "-") for l in labels]
        # EU-284: an explicit priority (mapped from the finding's severity) overrides the project
        # default; leaving it unset (None/empty) keeps Jira's own default (Medium) untouched.
        if priority:
            fields["priority"] = {"name": priority}
        r = self.session.post(self._url("issue"), json={"fields": fields})
        r.raise_for_status()
        return r.json().get("key")

    # -- Epic completion (EU-374, closes EU-301's step 5) ------------------ #
    def parent_epic_key(self, key: str) -> str | None:
        """The key of the EPIC this issue is a child of (the team-managed ``parent`` field EU-301
        links children with), or None. Deliberately Epic-only: on these next-gen boards ``parent``
        also carries sub-task→Task links, and the Epic auto-close must never fire on one of those.
        Any API/shape hiccup returns None — the caller (loop._maybe_close_epic) is best-effort and
        an unresolved parent simply leaves the Epic open."""
        try:
            r = self.session.get(self._url(f"issue/{key}"), params={"fields": "parent"})
            r.raise_for_status()
            parent = ((r.json().get("fields") or {}).get("parent") or {})
            ptype = ((((parent.get("fields") or {}).get("issuetype") or {}).get("name")) or "")
            if parent.get("key") and ptype.strip().lower() == "epic":
                return parent["key"]
            return None
        except requests.RequestException:
            return None

    def epic_children(self, epic_key: str) -> list[dict[str, str]]:
        """All child issues of an Epic (``parent = <epic>`` JQL, the EU-301 linkage) as
        ``[{key, summary, status, status_category}]`` — exactly what the Epic auto-close needs to
        decide whether every sibling is Done/QA. Raises on an HTTP failure (and on Jira's
        auth-blind 200, see _raise_if_unauthenticated) instead of returning [] — an empty list
        must mean "the Epic truly has no children", never "the search broke", or a hiccup could
        vacuously prove the siblings done (the caller also re-checks the landed child is present)."""
        r = self.session.post(self._url("search/jql"), json={
            "jql": f'parent = "{epic_key}" ORDER BY created ASC',
            "maxResults": 100, "fields": ["summary", "status"]})
        r.raise_for_status()
        self._raise_if_unauthenticated(r)
        out: list[dict[str, str]] = []
        for it in r.json().get("issues", []) or []:
            f = it.get("fields", {}) or {}
            status = (f.get("status") or {}) or {}
            out.append({
                "key": it.get("key", "") or "",
                "summary": (f.get("summary") or "").strip(),
                "status": (status.get("name") or "").strip(),
                "status_category": (((status.get("statusCategory") or {}).get("key")) or "").strip(),
            })
        return out

    def find_open_by_summary(self, summary: str) -> str | None:
        """Return an OPEN ticket with a matching summary (de-dup), else None — None means the search
        RAN and found no duplicate. A search that could not run raises BacklogSearchError instead, so
        a caller can never read an outage as "nothing open matches" and file a duplicate (EU-365)."""
        q = summary.replace('"', " ").replace("\\", " ").strip()[:120]
        if not q:
            return None
        try:
            r = self.session.post(self._url("search/jql"), json={
                "jql": f'project = "{self.project}" AND statusCategory != Done AND summary ~ "{q}"',
                "maxResults": 5, "fields": ["summary"]})
            r.raise_for_status()
            issues = r.json().get("issues", [])
        except requests.RequestException as exc:
            raise BacklogSearchError(
                f"de-dup search failed for project '{self.project}': {exc}") from exc
        # Compare truncated-vs-truncated: the candidate's stored summary is what create_task could
        # persist, so normalise this summary the same way before matching (EU-365).
        want = _summary_for_jira(summary).lower()
        for it in issues:
            stored = ((it.get("fields", {}) or {}).get("summary", "") or "").strip().lower()
            if stored == want:
                return it.get("key")
        return None

    def find_open_by_label(self, label: str) -> str | None:
        """Return an OPEN ticket carrying ``label``, else None. The subject-level de-dup key
        (2026-07-21): filing stamps a fingerprint label derived from the finding's code anchors,
        so six differently-worded reports of ONE problem (EU-409..414) collapse to one ticket.
        Raises BacklogSearchError when the search can't run — same fail-closed contract as
        find_open_by_summary (EU-365): an outage must never read as "no duplicate"."""
        lab = "".join(ch for ch in (label or "") if ch.isalnum() or ch in "-_")[:60]
        if not lab:
            return None
        try:
            r = self.session.post(self._url("search/jql"), json={
                "jql": f'project = "{self.project}" AND statusCategory != Done AND labels = "{lab}"',
                "maxResults": 1, "fields": ["summary"]})
            r.raise_for_status()
            issues = r.json().get("issues", [])
        except requests.RequestException as exc:
            raise BacklogSearchError(
                f"label de-dup search failed for project '{self.project}': {exc}") from exc
        return issues[0].get("key") if issues else None

    def set_labels(self, key: str, add: tuple = (), remove: tuple = ()) -> bool:
        """Add and/or remove labels on an existing issue via Jira's edit-operations form. EU-439:
        ``relabel_fingerprint`` uses this to swap a mis-stamped subject fingerprint label for the
        correct one. Non-clobbering — only the named labels change; the officer label, autofiled,
        assignee and every other label are untouched (the ``update`` edit-operations form mutates
        the label SET, it never replaces it). A duplicate add / unknown remove is a harmless
        no-op on Jira's side. Returns True on success."""
        ops = ([{"add": str(l)} for l in (add or [])]
               + [{"remove": str(l)} for l in (remove or [])])
        if not ops:
            return True
        r = self.session.put(self._url(f"issue/{key}"), json={"update": {"labels": ops}})
        r.raise_for_status()
        return True

    # -- Senior PM operations: close & transition -------------------------------- #
    def open_epics(self) -> list[str]:
        """Keys of every Epic in this project that is NOT done — the input to the EU-734 sweep.

        The Epic roll-up normally fires when a child completes, but that only helps Epics whose
        last child completes AFTER the roll-up exists. An Epic finished earlier (or one whose
        hand-off failed on a board hiccup) would sit open forever with nothing left to trigger it —
        16 were found in exactly that state on 2026-07-27. This lets an idle-boundary sweep
        re-evaluate them, so "no zombies" holds without depending on a single event.

        Raises BacklogSearchError on a failed/auth-blind search, like the other read paths: an
        outage must read as "unknown", never as "no open Epics"."""
        try:
            r = self.session.post(self._url("search/jql"), json={
                "jql": f'project = "{self.project}" AND issuetype = Epic AND statusCategory != Done '
                       f'ORDER BY created ASC',
                "maxResults": 100, "fields": ["summary"]})
            r.raise_for_status()
            self._raise_if_unauthenticated(r)
            return [str(i.get("key")) for i in (r.json().get("issues") or []) if i.get("key")]
        except requests.RequestException as exc:
            raise BacklogSearchError(
                f"open-Epic search failed for project '{self.project}': {exc}") from exc

    def close_ticket(self, ticket_id: str, comment: str = "", audit=None) -> bool:
        """Close a ticket with an optional comment. Returns True on success.

        The Senior PM uses this after a CLOSE verdict to resolve invalid/duplicate tickets.
        All Jira writes are recorded to the audit log when an audit_log is passed.
        """
        try:
            # Add comment if provided
            if comment and comment.strip():
                self.session.post(self._url(f"issue/{ticket_id}/comment"),
                                  json={"body": _adf("[Squad] " + comment)}).raise_for_status()

            # Find transition to Done/Closed
            tr = self.session.get(self._url(f"issue/{ticket_id}/transitions"))
            tr.raise_for_status()
            # Prefer "Done" but fall back to any terminal status
            target = next((t for t in tr.json().get("transitions", [])
                          if t["to"]["name"].lower() in ("done", "closed")), None)
            if not target:
                # Try any status with statusCategory = Done
                for t in tr.json().get("transitions", []):
                    if t.get("to", {}).get("statusCategory", {}).get("key") == "done":
                        target = t
                        break

            if not target:
                raise RuntimeError(f"No close transition available for {ticket_id}")

            self.session.post(self._url(f"issue/{ticket_id}/transitions"),
                              json={"transition": {"id": target["id"]}}).raise_for_status()

            # Audit the close
            if audit:
                audit.record("jira_close", ticket_id=ticket_id, comment=comment)

            return True
        except requests.RequestException as exc:
            if audit:
                audit.record("jira_close_failed", ticket_id=ticket_id, error=str(exc))
            return False

    def transition_ticket(self, ticket_id: str, target_project_key: str,
                          corrected_acceptance: str, audit=None) -> bool:
        """Move a ticket to another project (bulk-edit the project field) and update acceptance.

        The Senior PM uses this after a REFILE verdict to route tickets to the correct project.
        This is a bulk edit because Jira has no native 'move' via REST without admin rights.
        All Jira writes are recorded to the audit log when an audit_log is passed.

        Args:
            ticket_id: The ticket key (e.g., AUTO-123).
            target_project_key: Destination project key (e.g., 'AUTO').
            corrected_acceptance: New acceptance criteria to set on the ticket.
            audit: Optional AuditLog instance for recording the action.

        Returns:
            True on success, False on failure.
        """
        try:
            # Fetch the target project to get its ID
            proj_resp = self.session.get(self._url("project"), params={"key": target_project_key})
            proj_resp.raise_for_status()
            projects = proj_resp.json().get("values", [])
            if not projects:
                raise RuntimeError(f"Target project {target_project_key} not found")
            target_proj_id = projects[0]["id"]

            # Build update payload: change project + set acceptance criteria if field is configured
            payload: dict[str, object] = {"fields": {"project": {"id": target_proj_id}}}
            if self.ac_field and corrected_acceptance:
                payload["fields"][self.ac_field] = _adf(corrected_acceptance)

            # Apply the update
            self.session.put(self._url(f"issue/{ticket_id}"), json=payload).raise_for_status()

            # Audit the transition
            if audit:
                audit.record(
                    "jira_transition",
                    ticket_id=ticket_id,
                    target_project=target_project_key,
                    has_acceptance=bool(corrected_acceptance)
                )

            return True
        except requests.RequestException as exc:
            if audit:
                audit.record("jira_transition_failed", ticket_id=ticket_id,
                            target_project=target_project_key, error=str(exc))
            return False

    def _download_images(self, key: str, attachments: list[dict[str, object]]) -> list[str]:
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
    def _to_ticket(self, issue: dict[str, object]) -> Ticket:
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
            if txt and not txt.strip().startswith(("[Squad]", "[General]")):
                feedback.append(txt.strip())
        if feedback:
            description += (_COMMANDER_COMMENTS_PREFIX
                           + " read ALL of these; they include QA feedback on what to fix:\n"
                           + "\n---\n".join(feedback))[:8000]
        # Download image attachments so the Builder can SEE the mockups/screenshots, not guess.
        imgs = self._download_images(issue["key"], f.get("attachment") or [])
        if imgs:
            description += (_TICKET_IMAGES_PREFIX
                           + " (they show the desired design / the bug); do not guess at visuals:\n"
                           + "\n".join(f"- {p}" for p in imgs))
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
            # Carry the Jira status through so the autopilot can split In Progress vs To Do
            # for the three-tier drain order (EU-87) without a second API call.
            status=((f.get("status") or {}) or {}).get("name"),
            # EU-783: carry Jira priority so _tiebreak_autofiled can sort autofiled last within equal
            # priority tier, while respecting board Rank via Python's stable sort.
            priority=((f.get("priority") or {}) or {}).get("name"),
        )


# --------------------------------------------------------------------------- #
# Atlassian Document Format helpers
# --------------------------------------------------------------------------- #
def _adf(text: str) -> dict:
    """Wrap plain text in a minimal ADF document for comments."""
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph",
                         "content": [{"type": "text", "text": text}]}]}


def _adf_to_text(node: object) -> str:
    """Flatten an ADF node tree to plain text (newline per block)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    out: list[str] = []

    def walk(n: object) -> None:
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


# --------------------------------------------------------------------------- #
# Doctrine lookup helpers — read CLAUDE.md and config.yaml for context
# --------------------------------------------------------------------------- #
def read_claude_md(repo_path: str) -> str:
    """Read the CLAUDE.md file from a repository for doctrine lookup.

    Returns the file content as a string, or empty string if not found.
    Used by the Senior PM to ground decisions in repo conventions.
    """
    from pathlib import Path
    claude_path = Path(repo_path).expanduser().resolve() / "CLAUDE.md"
    try:
        return claude_path.read_text(encoding="utf-8")
    except Exception:
        return ""


def read_config_yaml(config_path: str | None = None) -> str:
    """Read the config.yaml file for unit-wide doctrine lookup.

    Args:
        config_path: Path to config.yaml (default: ./config.yaml relative to cwd).

    Returns the file content as a string, or empty string if not found.
    Used by the Senior PM to understand project settings and conventions.
    """
    from pathlib import Path
    if config_path is None:
        config_path = "./config.yaml"
    try:
        return Path(config_path).expanduser().resolve().read_text(encoding="utf-8")
    except Exception:
        return ""


def get_doctrine_context(app_name: str, repo_path: str, cfg) -> str:
    """Build a doctrine context string from CLAUDE.md + config for the Senior PM.

    Combines repo conventions (CLAUDE.md) with unit-wide configuration (config.yaml)
    so the Senior PM can ground its triage decisions in how the unit actually works.

    Args:
        app_name: The app name (for selecting relevant config sections).
        repo_path: Path to the app's repo (to read CLAUDE.md).
        cfg: The Config object (provides config path resolution).

    Returns:
        A string with relevant doctrine excerpts, or empty if unavailable.
    """
    parts = []
    claude_content = read_claude_md(repo_path)
    if claude_content:
        parts.append(f"CLAUDE.md (conventions for this repo):\n{claude_content[:4000]}")

    # Try to resolve config path from cfg if available
    config_path = None
    try:
        if hasattr(cfg, 'audit_path'):
            # Derive config path relative to audit.jsonl (same convention as run_logger)
            config_path = Path(cfg.audit_path).parent.resolve() / "config.yaml"
        if not config_path or not Path(config_path).exists():
            config_path = "./config.yaml"
    except Exception:
        config_path = "./config.yaml"

    config_content = read_config_yaml(str(config_path))
    if config_content:
        parts.append(f"\nconfig.yaml (unit configuration):\n{config_content[:4000]}")

    return "\n\n".join(parts) if parts else ""
