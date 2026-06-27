"""Intent classifier for Commander directives on the Needs-you answer form (EU-82).

When the Commander types an answer in the /needs Ship-answer box, it is not always a
clarification that should re-run the original ticket.  Sometimes it is a directive
("open a ticket for this"), a close order, or a simple deferral.  This module classifies
that free text into one of four intents so the /api/answer endpoint can route to the
correct action without forcing every Commander reply into the decisions.handle_reply /
re-run path.

Intents
-------
  'file_ticket'   — The Commander wants a new backlog ticket filed
                    (e.g. "open a ticket", "create a ticket for this").
  'close'         — The Commander wants the parked ticket closed/resolved with no re-run
                    (e.g. "close it", "done", "won't fix").
  'defer'         — The Commander wants to dismiss the row and do nothing further
                    (e.g. "defer", "skip", "later").
  'clarification' — Anything else: treat as a decision that answers the open question
                    and re-run the original ticket with the answer baked in
                    (the existing decisions.handle_reply path).

Classification is purely keyword/pattern-based — no LLM call — so it is deterministic,
fast, and never fails due to an API outage.
"""
from __future__ import annotations

import re
from typing import Literal

Intent = Literal["file_ticket", "close", "defer", "clarification"]

# --- Pattern bank (most-specific first within each intent) ---

_FILE_TICKET = re.compile(
    r"\b(open|file|create|raise|log|add|make|cut)\s+a?\s*(new\s+)?ticket\b"
    r"|\bnew\s+ticket\b"
    r"|\bticket\s+(for\s+this|it)\b"
    r"|\bopen\s+a\s+jira\b"
    r"|\bcreate\s+an?\s+issue\b",
    re.IGNORECASE,
)

# 'close' / 'defer' are ANCHORED, whole-message directives (EU-82 iter-3). Unlike 'file_ticket'
# (which is a standalone instruction that legitimately appears mid-sentence — "please open a ticket
# for X"), a 'close'/'defer' verdict must be *essentially the entire reply*. Otherwise a clarification
# that merely mentions a word like "invalid", "complete", "done", "ignore" or "later" — while actually
# carrying a substantive instruction — would be misread as an order to close/defer the parked ticket.
#
# So each pattern is matched with ``re.fullmatch`` against the whole (normalised) message: an optional
# politeness lead, then one or more directive "cores" joined ONLY by punctuation/whitespace, then
# trailing punctuation. "won't fix — no longer needed" parses as two stacked close-cores; "the date
# parsing is invalid, switch to ISO 8601" does NOT fullmatch (the tail isn't made of cores) and stays
# a 'clarification'. A word-count guard is an extra, explicit backstop on the same "just the directive"
# rule.

_LEAD = r"(?:(?:please|pls)[,:\s]+)*"      # polite prefix that doesn't change the directive
_SEP = r"[\s,;.:—–-]+"                       # joins stacked cores ("won't fix — no longer needed")
_END = r"[\s.!?…]*"                          # trailing punctuation / whitespace

_CLOSE_CORE = (
    r"close(?:\s+(?:it|this|out|the\s+(?:ticket|issue)))?"
    r"|resolve(?:\s+(?:it|this))?|resolved"
    r"|mark(?:\s+it)?(?:\s+as)?\s+(?:done|closed|resolved|complete|completed)"
    r"|won'?t\s+fix|wont\s*fix|won'?t\s+do|will\s+not\s+fix|not\s+fixing"
    r"|not\s+a\s+(?:bug|real\s+bug|valid\s+(?:bug|issue|concern))"
    r"|no\s+longer\s+(?:needed|valid|relevant|an?\s+issue)"
    r"|already\s+(?:fixed|done|resolved)"
    r"|invalid|duplicate|dupe"
    r"|done|finished|complete|completed"
)
_CLOSE = re.compile(
    _LEAD + r"(?:" + _CLOSE_CORE + r")(?:" + _SEP + r"(?:" + _CLOSE_CORE + r"))*" + _END,
    re.IGNORECASE,
)

_DEFER_CORE = (
    r"defer(?:\s+(?:it|this))?|deferred"
    r"|skip(?:\s+(?:it|this))?|skipped"
    r"|postpone(?:\s+(?:it|this))?|postponed"
    r"|hold\s+(?:off|it)"
    r"|park(?:\s+(?:it|this))?|parked"
    r"|snooze(?:\s+(?:it|this))?"
    r"|not\s+(?:now|right\s+now|yet)"
    r"|maybe\s+later|later"
    r"|ignore(?:\s+(?:it|this))?"
    r"|for\s+now"
    r"|backlog(?:\s+it)?"
)
_DEFER = re.compile(
    _LEAD + r"(?:" + _DEFER_CORE + r")(?:" + _SEP + r"(?:" + _DEFER_CORE + r"))*" + _END,
    re.IGNORECASE,
)

# A genuine close/defer reply is terse; a longer message is a clarification that merely mentions a
# keyword. The anchored fullmatch already rejects those — this is a cheap, explicit second gate.
_MAX_DIRECTIVE_WORDS = 7


def _normalise(text: str) -> str:
    """Trim whitespace and one optional layer of matching surrounding quotes."""
    t = (text or "").strip()
    if len(t) >= 2 and t[0] in "\"'`" and t[-1] == t[0]:
        t = t[1:-1].strip()
    return t


def classify_intent(text: str) -> Intent:
    """Classify the Commander's free-text answer into an action intent.

    Returns one of:
      'file_ticket'   — Commander wants a new backlog ticket filed; do not re-run the
                        original ticket — instead create a new task and dismiss the row.
                        Matched with an unanchored search, since "open a ticket for X" is a
                        standalone instruction that may sit inside a longer sentence.
      'close'         — Commander wants the parked ticket closed with no re-run; transition
                        the ticket to Done via the backlog adapter then dismiss. Fires ONLY
                        when the whole reply is essentially the close directive.
      'defer'         — Commander wants to leave the ticket parked (snooze the row); do not
                        re-run, file, or close. Fires ONLY when the whole reply is the directive.
      'clarification' — Default: the answer should re-run the original ticket with the
                        Commander's text baked into its spec (decisions.handle_reply path).
                        If no pending decision is on file, post it as a comment and unblock.
                        Anything carrying a substantive instruction lands here, even if it
                        incidentally contains a close/defer keyword.
    """
    t = _normalise(text)
    if not t:
        return "clarification"  # empty → let existing guard handle it
    if _FILE_TICKET.search(t):
        return "file_ticket"
    # close/defer must be (essentially) the WHOLE message: short AND a full anchored match.
    if len(t.split()) <= _MAX_DIRECTIVE_WORDS:
        if _CLOSE.fullmatch(t):
            return "close"
        if _DEFER.fullmatch(t):
            return "defer"
    return "clarification"
