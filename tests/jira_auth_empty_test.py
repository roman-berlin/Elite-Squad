"""Regression: a dead Jira token must NOT masquerade as 'queue clear'.

Root cause this guards (the EU-20..EU-36 invisible-backlog bug): Jira's POST
/rest/api/3/search/jql answers an UNAUTHENTICATED request with HTTP 200 and an EMPTY
issue list (not 401). So an invalid/expired JIRA_API_TOKEN looked exactly like 'no ready
tickets' — get_ready_tasks returned [], raise_for_status() passed, and the cockpit/
autopilot reported 'queue clear - nothing of yours' for EU's whole To Do column.

Atlassian flags the real state in the X-Seraph-LoginReason header even on that 200
(AUTHENTICATED_FAILED). The fix raises on it so from_drain records the board as
UNREACHABLE (visible) instead of clear (silent). These checks pin that behaviour AND
prove it doesn't over-fire on a genuinely empty queue or the happy path.
"""
import sys, types

# stub the heavy/optional deps so the orchestrator imports with no network / no model SDK
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda *a, **k: None
req.RequestException = Exception
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator.backlog import jira
from orchestrator import intake

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


class FakeResp:
    """A search/jql response with a controllable status, issue list, and Seraph header."""
    def __init__(self, status=200, issues=None, login_reason=None):
        self.status_code = status
        self._issues = issues or []
        self.headers = {} if login_reason is None else {"X-Seraph-LoginReason": login_reason}
        self.text = ""
    def raise_for_status(self):
        if self.status_code >= 400:
            raise req.RequestException(f"HTTP {self.status_code}")
    def json(self):
        return {"issues": self._issues, "isLast": True}


class FakeSession:
    def __init__(self, search_resp):
        self.search_resp = search_resp
        self.posts = []
    def post(self, url, json=None):
        self.posts.append((url, json))
        return self.search_resp


def make_self(search_resp):
    """A SimpleNamespace standing in for a JiraAdapter, wired to the REAL guard + get_ready_tasks."""
    s = types.SimpleNamespace(
        app_name="Elite-Unit", base_url="https://toibis.atlassian.net",
        jql_override=None, queue_statuses=["To Do"],
        _jql_for_status=lambda st: f'project = "EU" AND status = "{st}"',
        _url=lambda path: f"https://toibis.atlassian.net/rest/api/3/{path}",
        _fields=lambda: ["summary"],
        _to_ticket=lambda issue: types.SimpleNamespace(id=issue["key"], key=issue["key"]),
        session=FakeSession(search_resp),
    )
    # bind the REAL guard so the test exercises shipped code, not a copy
    s._raise_if_unauthenticated = lambda resp: jira.JiraAdapter._raise_if_unauthenticated(s, resp)
    return s


# --- A) the bug: 200 + empty + AUTHENTICATED_FAILED must RAISE, not return [] -------------
self_dead = make_self(FakeResp(200, issues=[], login_reason="AUTHENTICATED_FAILED"))
raised = None
try:
    jira.JiraAdapter.get_ready_tasks(self_dead, 25)
except RuntimeError as e:
    raised = str(e)
chk("dead token (200-empty + AUTHENTICATED_FAILED) RAISES instead of silent []", raised is not None)
chk("the error is actionable (names the token + 'auth failed')",
    bool(raised) and "auth failed" in raised.lower() and "JIRA_API_TOKEN" in raised, raised or "")

# --- A2) AUTHENTICATION_DENIED is treated the same way -----------------------------------
self_denied = make_self(FakeResp(200, issues=[], login_reason="AUTHENTICATION_DENIED"))
denied_raised = False
try:
    jira.JiraAdapter.get_ready_tasks(self_denied, 25)
except RuntimeError:
    denied_raised = True
chk("a DENIED login-reason is also surfaced", denied_raised)

# --- B) genuinely empty queue (authenticated) must return [], NOT cry auth failure -------
self_empty_ok = make_self(FakeResp(200, issues=[], login_reason="OK"))
empty_ok = None
try:
    empty_ok = jira.JiraAdapter.get_ready_tasks(self_empty_ok, 25)
except RuntimeError as e:
    empty_ok = e
chk("authenticated-but-empty (OK header) returns [] without raising", empty_ok == [])

# --- B2) no header at all -> behave as before (empty), never a false auth alarm ----------
self_no_header = make_self(FakeResp(200, issues=[], login_reason=None))
no_header_ok = None
try:
    no_header_ok = jira.JiraAdapter.get_ready_tasks(self_no_header, 25)
except RuntimeError as e:
    no_header_ok = e
chk("missing Seraph header returns [] (no false positive)", no_header_ok == [])

# --- C) happy path: real issues come back, guard does not interfere ----------------------
self_ok = make_self(FakeResp(200, issues=[{"key": "EU-20"}, {"key": "EU-21"}], login_reason="OK"))
got = jira.JiraAdapter.get_ready_tasks(self_ok, 25)
chk("authenticated with work returns the tickets", [t.key for t in got] == ["EU-20", "EU-21"], str(got))

# --- D) full chain: from_drain SURFACES the auth failure (UNREACHABLE), not 'queue clear' -
class DeadBacklog:
    """get_ready_tasks delegates to the REAL adapter method against a dead-token session."""
    def get_ready_tasks(self, limit):
        return jira.JiraAdapter.get_ready_tasks(
            make_self(FakeResp(200, issues=[], login_reason="AUTHENTICATED_FAILED")), limit)

appEU = types.SimpleNamespace(name="Elite-Unit", backlog_backend="jira")
cfg = types.SimpleNamespace(apps=[appEU])
intake.make_backlog = lambda app: DeadBacklog()
intake.LAST_DRAIN_ERRORS.clear()
drained = intake.from_drain(cfg, None, 25)
chk("from_drain returns no items for the dead board", drained == [])
chk("from_drain RECORDS the auth failure in LAST_DRAIN_ERRORS (visible, not silent)",
    "Elite-Unit" in intake.LAST_DRAIN_ERRORS and "auth failed" in intake.LAST_DRAIN_ERRORS["Elite-Unit"].lower(),
    str(dict(intake.LAST_DRAIN_ERRORS)))

print("\n============ JIRA AUTH-BLIND 200 QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
