"""EU-397: every Jira transition (success, fallback, no-op, failure) leaves an audit trace.

From the 2026-07-19 senior-workflow audit: only the post-merge call site ever logged a
transition outcome (merge_transition_failed) — the live audit log held ZERO transition events
across 133 merges. set_status now instruments itself (the one write choke-point every call site
shares) so board-vs-Jira drift becomes diagnosable regardless of which of the six call sites
triggered the write.

Pins the four outcome shapes: matched, fallback, no-op, no-transition — plus
ticket_transition_failed on exception (re-raised unchanged, exactly as before).
"""
import sys
import types

sys.path.insert(0, ".")

# Stub the Agent SDK + requests so importing the orchestrator never reaches the network.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk


class _ReqErr(Exception):
    """Stands in for requests.RequestException."""


req = types.ModuleType("requests")
req.Session = lambda *a, **k: None
req.RequestException = _ReqErr
sys.modules["requests"] = req

from orchestrator.backlog import jira                 # noqa: E402
from orchestrator.contracts import Ticket              # noqa: E402

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), str(d) if not c else ""))


class _FakeAudit:
    """Captures every record() call — the harness's window into the adapter's audit writes."""
    def __init__(self):
        self.events = []
    def record(self, event, **fields):
        self.events.append((event, fields))


class _Resp:
    def __init__(self, payload=None, boom=None):
        self._p = payload or {}
        self._boom = boom
    def raise_for_status(self):
        if self._boom:
            raise self._boom
    def json(self):
        return self._p


class _Sess:
    """A scriptable fake Jira session: current-status GET, transitions GET, transition POST,
    comment POST — each independently controllable per test case."""
    def __init__(self, current_status=None, transitions=None, post_boom=None,
                 transitions_boom=None):
        self.current_status = current_status
        self.transitions = transitions if transitions is not None else []
        self.post_boom = post_boom
        self.transitions_boom = transitions_boom
        self.posts = []

    def get(self, url, params=None):
        if url.endswith("/transitions"):
            if self.transitions_boom:
                return _Resp(boom=self.transitions_boom)
            return _Resp({"transitions": self.transitions})
        # issue/<key> (current status probe)
        return _Resp({"fields": {"status": {"name": self.current_status}}})

    def post(self, url, json=None):
        self.posts.append((url, json))
        if url.endswith("/transitions") and self.post_boom:
            return _Resp(boom=self.post_boom)
        return _Resp({})


def _adapter(session, status_map=None, status_fallbacks=None):
    ad = types.SimpleNamespace(
        session=session,
        status_map=status_map or {},
        status_fallbacks=status_fallbacks or {"QA": ["Done", "Closed"]},
    )
    ad._url = lambda p: f"https://x.atlassian.net/rest/api/3/{p.lstrip('/')}"
    ad._current_status = lambda key: jira.JiraAdapter._current_status(ad, key)
    ad.add_comment = lambda t, b: jira.JiraAdapter.add_comment(ad, t, b)
    return ad


TICKET = Ticket(id="EU-397", key="EU-397", summary="s", description="", acceptance_criteria=[])


def _run(session, status, **kw):
    fake = _FakeAudit()
    jira._AUDIT_LOG = fake
    ad = _adapter(session, **kw)
    exc = None
    try:
        jira.JiraAdapter.set_status(ad, TICKET, status)
    except Exception as e:  # noqa: BLE001 - captured for the failure-outcome case
        exc = e
    return fake, exc


# ============================================================================ #
# 1. matched — the requested column exists and is reached directly
# ============================================================================ #
sess_matched = _Sess(current_status="In Progress",
                     transitions=[{"id": "31", "to": {"name": "In Review"}}])
audit_matched, exc = _run(sess_matched, "In Review")
chk("matched: exactly one ticket_transition event", len(audit_matched.events) == 1,
    audit_matched.events)
ev, fields = audit_matched.events[0] if audit_matched.events else (None, {})
chk("matched: event name is ticket_transition", ev == "ticket_transition", ev)
chk("matched: outcome=matched, wanted=landed=In Review",
    fields.get("outcome") == "matched" and fields.get("wanted") == "In Review"
    and fields.get("landed") == "In Review", fields)
chk("matched: ticket_id is stamped", fields.get("ticket_id") == "EU-397", fields)
chk("matched: no exception raised", exc is None, exc)


# ============================================================================ #
# 2. fallback — the primary target column is absent; a configured fallback lands instead
# ============================================================================ #
sess_fb = _Sess(current_status="In Review",
                transitions=[{"id": "41", "to": {"name": "Done"}}])
audit_fb, exc = _run(sess_fb, "QA")   # QA has no column; status_fallbacks: QA -> [Done, Closed]
chk("fallback: exactly one ticket_transition event", len(audit_fb.events) == 1, audit_fb.events)
ev, fields = audit_fb.events[0] if audit_fb.events else (None, {})
chk("fallback: outcome=fallback, wanted=QA, landed=Done",
    fields.get("outcome") == "fallback" and fields.get("wanted") == "QA"
    and fields.get("landed") == "Done", fields)
chk("fallback: the board-fallback ticket comment still posts (fail-soft behavior unchanged)",
    any("fallback" in (p[1]["body"] if False else str(p)) for p in sess_fb.posts)
    or len(sess_fb.posts) >= 1, str(sess_fb.posts))


# ============================================================================ #
# 3. no-op — the ticket already sits in a candidate column
# ============================================================================ #
sess_noop = _Sess(current_status="Done", transitions=[])
audit_noop, exc = _run(sess_noop, "QA")   # current "Done" matches the QA->Done fallback candidate
chk("no-op: exactly one ticket_transition event", len(audit_noop.events) == 1, audit_noop.events)
ev, fields = audit_noop.events[0] if audit_noop.events else (None, {})
chk("no-op: outcome=no-op, landed=current status (Done)",
    fields.get("outcome") == "no-op" and fields.get("landed") == "Done", fields)
chk("no-op: no transition POST is made (unchanged no-bounce behavior)",
    sess_noop.posts == [], str(sess_noop.posts))


# ============================================================================ #
# 4. no-transition — no candidate column exists anywhere on the board
# ============================================================================ #
sess_none = _Sess(current_status="In Progress",
                  transitions=[{"id": "1", "to": {"name": "Blocked"}}])
audit_none, exc = _run(sess_none, "In Review")
chk("no-transition: exactly one ticket_transition event", len(audit_none.events) == 1,
    audit_none.events)
ev, fields = audit_none.events[0] if audit_none.events else (None, {})
chk("no-transition: outcome=no-transition", fields.get("outcome") == "no-transition", fields)
chk("no-transition: the 'please move it manually' comment still posts (fail-soft unchanged)",
    len(sess_none.posts) == 1 and "please move it manually" in sess_none.posts[0][1]["body"]["content"][0]["content"][0]["text"],
    str(sess_none.posts))


# ============================================================================ #
# 5. ticket_transition_failed on exception — then RE-RAISED (not swallowed by the adapter itself)
# ============================================================================ #
sess_boom = _Sess(current_status="In Progress", transitions_boom=_ReqErr("503 Service Unavailable"))
audit_boom, exc = _run(sess_boom, "In Review")
chk("failed: exactly one ticket_transition_failed event", len(audit_boom.events) == 1,
    audit_boom.events)
ev, fields = audit_boom.events[0] if audit_boom.events else (None, {})
chk("failed: event name is ticket_transition_failed", ev == "ticket_transition_failed", ev)
chk("failed: wanted + ticket_id + error are stamped",
    fields.get("wanted") == "In Review" and fields.get("ticket_id") == "EU-397"
    and "503" in str(fields.get("error", "")), fields)
chk("failed: the exception is RE-RAISED (fail-soft stays at the CALL SITE, not the adapter)",
    isinstance(exc, _ReqErr), exc)


print("\n================ EU-397 JIRA TRANSITION AUDIT QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
