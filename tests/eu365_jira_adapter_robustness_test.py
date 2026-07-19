"""EU-365: Jira-adapter robustness batch — the four read/de-dup defects the 2026-07-16 total audit
found in `orchestrator/backlog/jira.py`, each pinned here against a fake session (no network).

  1. status_map bypass on the READ path — `_jql_for_status` built `status = "In Progress"` from the
     raw LOGICAL name while `set_status` mapped through `status_map`. A board whose workflow calls
     that column "In Development" drained NOTHING, silently.
  2. error-swallow -> duplicate filings — `find_open_by_summary` returned None on ANY API failure,
     and filing.py reads None as "no duplicate exists", so a search hiccup minted a duplicate of
     every finding (the de-dup EU-42 relies on). Now fail-closed: raises BacklogSearchError.
  3. single-page comments — `comments()` read one page (Jira's default 50, oldest-first), so
     `latest_answer` missed the Commander's newest reply on a long decision thread.
  4. truncated-title de-dup — `create_task` stored `summary[:240]` but `find_open_by_summary`
     compared the FULL title, so a >240-char finding could never match its own stored twin.

Item 5 of the ticket (429/Retry-After handling in orchestrator/filing.py) is NOT covered here — it
lives in a different file and is tracked separately.
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
    """Stands in for requests.RequestException (which HTTPError subclasses, so raise_for_status on a
    5xx lands here). A DISTINCT class, not bare Exception: an over-broad `except` must not pass."""


req = types.ModuleType("requests")
req.Session = lambda *a, **k: None
req.RequestException = _ReqErr
sys.modules["requests"] = req

from orchestrator import filing                       # noqa: E402
from orchestrator.backlog import jira                 # noqa: E402
from orchestrator.config import AppConfig             # noqa: E402
from orchestrator.contracts import Ticket             # noqa: E402

# Feed plain strings as ADF comment bodies.
jira._adf_to_text = lambda x: x if isinstance(x, str) else ""

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), str(d) if not c else ""))


class _Resp:
    def __init__(self, payload=None, boom=None):
        self._p = payload or {}
        self._boom = boom
        self.headers = {}
    def raise_for_status(self):
        if self._boom:
            raise self._boom
    def json(self):
        return self._p


# ========================================================================= #
# 1. status_map must apply to the READ path, exactly as it does to the write
# ========================================================================= #
class _SearchSess:
    """Serves issues per JQL fragment; records every JQL the adapter asked for."""
    def __init__(self, by_fragment):
        self.by_fragment = by_fragment
        self.jqls = []
    def post(self, url, json=None):
        self.jqls.append(json["jql"])
        for frag, issues in self.by_fragment.items():
            if frag in json["jql"]:
                return _Resp({"issues": issues})
        return _Resp({"issues": []})


def _adapter(session, **kw):
    d = dict(app_name="Elite-Unit", base_url="https://x.atlassian.net", project="EU",
             assignee=None, only_mine=False, require_label=False, label="autodev",
             status_map={}, queue_statuses=["In Progress", "To Do"], jql_override=None,
             session=session)
    d.update(kw)
    s = types.SimpleNamespace(**d)
    s._url = lambda p: f"https://x.atlassian.net/rest/api/3/{p.lstrip('/')}"
    s._fields = lambda: ["summary"]
    s._to_ticket = lambda issue: Ticket(id=issue["key"], key=issue["key"], summary="s",
                                        description="", acceptance_criteria=[],
                                        status=issue.get("status"))
    s._jql_for_status = lambda st: jira.JiraAdapter._jql_for_status(s, st)
    s._raise_if_unauthenticated = lambda r: jira.JiraAdapter._raise_if_unauthenticated(s, r)
    return s


# A board whose workflow renames both queue columns.
BOARD_MAP = {"In Progress": "In Development", "To Do": "Ready for Dev"}
sess = _SearchSess({'status = "In Development"': [{"key": "EU-1", "status": "In Development"}],
                    'status = "Ready for Dev"': [{"key": "EU-2", "status": "Ready for Dev"}]})
ad = _adapter(sess, status_map=BOARD_MAP)
drawn = [t.key for t in jira.JiraAdapter.get_ready_tasks(ad, 5)]
chk("_jql_for_status maps the logical name through status_map",
    'status = "In Development"' in jira.JiraAdapter._jql_for_status(ad, "In Progress"),
    jira.JiraAdapter._jql_for_status(ad, "In Progress"))
chk("a renamed-workflow board actually drains its columns (was: silently empty)",
    drawn == ["EU-1", "EU-2"], f"{drawn} from jqls={sess.jqls}")
chk("no query is issued against the unmapped logical name",
    not any('status = "In Progress"' in q or 'status = "To Do"' in q for q in sess.jqls),
    str(sess.jqls))

# An unmapped name passes through untouched — the default board is unaffected.
plain = _adapter(_SearchSess({}), status_map={"Needs Human": "Needs Triage"})
chk("a status absent from status_map is queried verbatim",
    'status = "To Do"' in jira.JiraAdapter._jql_for_status(plain, "To Do"),
    jira.JiraAdapter._jql_for_status(plain, "To Do"))

# Mapping must be read defensively: several harnesses inject adapters with no status_map attribute
# at all (tests/eu252_drain_starves_in_progress_test.py:92), and so does any older fake.
no_attr = types.SimpleNamespace(project="EU", assignee=None, only_mine=False,
                                require_label=False, label="autodev")
try:
    jql = jira.JiraAdapter._jql_for_status(no_attr, "To Do")
    ok = 'status = "To Do"' in jql
except AttributeError as exc:
    ok, jql = False, f"AttributeError: {exc}"
chk("_jql_for_status tolerates an adapter with no status_map attribute", ok, jql)


# ========================================================================= #
# 2. de-dup search failure is UNKNOWN, never "no duplicate"
# ========================================================================= #
class _BoomSess:
    """Every search dies the way a 5xx does: raise_for_status -> RequestException."""
    def __init__(self, exc=None):
        self.exc = exc
        self.posts = 0
    def post(self, url, json=None):
        self.posts += 1
        if self.exc:
            raise self.exc
        return _Resp(boom=_ReqErr("503 Server Error: Service Unavailable"))


chk("BacklogSearchError exists and is not swallowed by callers of RequestException",
    issubclass(jira.BacklogSearchError, Exception)
    and not issubclass(jira.BacklogSearchError, _ReqErr))

for label, sess_boom in (("a 5xx (raise_for_status)", _BoomSess()),
                         ("a connection error", _BoomSess(exc=_ReqErr("connection reset")))):
    ad_boom = _adapter(sess_boom)
    try:
        got = jira.JiraAdapter.find_open_by_summary(ad_boom, "Fix the unguarded null deref")
        ok, det = False, f"returned {got!r} instead of raising"
    except jira.BacklogSearchError:
        ok, det = True, ""
    except Exception as exc:  # noqa: BLE001
        ok, det = False, f"raised {type(exc).__name__}: {exc}"
    chk(f"find_open_by_summary raises BacklogSearchError on {label}", ok, det)

# A SUCCESSFUL search with no hit still means a definitive "no duplicate" — None, not an error.
ad_clean = _adapter(_SearchSess({}))
chk("a successful search with no match still returns None (definitive 'no duplicate')",
    jira.JiraAdapter.find_open_by_summary(ad_clean, "Nothing like this is open") is None)
chk("an empty summary short-circuits to None without a search call",
    jira.JiraAdapter.find_open_by_summary(_adapter(_BoomSess()), "   ") is None)


# --- the consequence filing.py must see: fail-CLOSED, no duplicate minted ---
class _RaisingBacklog:
    def __init__(self):
        self.created = []
    def find_open_by_summary(self, summary):
        raise jira.BacklogSearchError("de-dup search failed for EU: 503 Service Unavailable")
    def create_task(self, summary, description, labels=None, issue_type="Task", priority=None):
        self.created.append(summary)
        return "EU-999"


REPORT = ('Build summary: done.\n===TICKETS===\n'
          '[{"title": "Fix unguarded null deref in parser", "type": "Bug", "severity": "HIGH",'
          ' "body": "orchestrator/x.py crashes on empty input"}]\n===END===\n')
app = AppConfig(name="Elite-Unit", repo_path="/tmp/x", base_branch="dev",
                protected_branch="main", backlog_backend="none")
fake_backlog = _RaisingBacklog()
filing.make_backlog = lambda a: fake_backlog
res = filing.file_findings(app, "out-of-scope", REPORT)
chk("a de-dup search failure does NOT mint a ticket (fail-closed)",
    fake_backlog.created == [], str(fake_backlog.created))
chk("the un-filed finding is reported as failed, never buried",
    res.filed == [] and res.failed_n == 1, f"filed={res.filed} failed={res.failed}")


# ========================================================================= #
# 3. comments() pages the whole thread
# ========================================================================= #
class _PagedSess:
    """Serves a comment thread the way Jira does: oldest-first, capped page size, `total` present."""
    def __init__(self, comments, page=50):
        self.all = comments
        self.page = page
        self.calls = []
    def get(self, url, params=None):
        p = params or {}
        start = int(p.get("startAt", 0) or 0)
        n = min(int(p.get("maxResults", self.page) or self.page), self.page)
        self.calls.append((start, n))
        return _Resp({"startAt": start, "maxResults": n, "total": len(self.all),
                      "comments": self.all[start:start + n]})


THREAD = [{"body": f"[General] question {i}"} for i in range(119)] + [{"body": "Ship option B."}]
paged = _PagedSess(THREAD)
ad_paged = _adapter(_SearchSess({}))
ad_paged.session = paged
ad_paged.comments = lambda key: jira.JiraAdapter.comments(ad_paged, key)
got = jira.JiraAdapter.comments(ad_paged, "EU-365")
chk("comments() returns the WHOLE 120-comment thread, not the first page",
    len(got) == 120, f"{len(got)} comments from calls={paged.calls}")
chk("comments() pages with startAt", len(paged.calls) > 1, str(paged.calls))
chk("latest_answer sees the Commander's newest reply on a long thread",
    jira.JiraAdapter.latest_answer(ad_paged, types.SimpleNamespace(key="EU-365")) == "Ship option B.")

# A short thread must still cost exactly ONE request (the common case).
short = _PagedSess([{"body": "only reply"}])
ad_short = _adapter(_SearchSess({}))
ad_short.session = short
chk("a single-page thread still costs one request",
    len(jira.JiraAdapter.comments(ad_short, "EU-1")) == 1 and len(short.calls) == 1,
    str(short.calls))


class _NoTotalSess:
    """A response with no `total` (and which ignores startAt) — the shape the older harnesses fake.
    The paging loop must TERMINATE on it rather than re-read the same page forever."""
    def __init__(self, comments):
        self.all = comments
        self.calls = 0
    def get(self, url, params=None):
        self.calls += 1
        return _Resp({"comments": self.all})


legacy = _NoTotalSess([{"body": "a"}, {"body": "b"}, {"body": "c"}])
ad_legacy = _adapter(_SearchSess({}))
ad_legacy.session = legacy
chk("a payload with no `total` terminates the paging loop (no repeat reads)",
    len(jira.JiraAdapter.comments(ad_legacy, "EU-1")) == 3 and legacy.calls == 1,
    f"calls={legacy.calls}")


# ========================================================================= #
# 4. a >240-char title de-dups against its own truncated twin
# ========================================================================= #
LONG_TITLE = ("Fix the unbounded comment pagination in the Jira backlog adapter so a long "
              "decision thread cannot hide the Commander's newest reply from latest_answer, "
              "which currently reads only the first page and therefore answers with a stale "
              "instruction the loop then executes against the wrong branch")
STORED = LONG_TITLE.strip()[:240]      # what create_task can actually persist
chk("the fixture title genuinely exceeds Jira's 240-char summary cap", len(LONG_TITLE) > 240,
    str(len(LONG_TITLE)))


class _CreateSess:
    def __init__(self):
        self.posts = []
    def post(self, url, json=None):
        self.posts.append((url, json))
        return _Resp({"key": "EU-900"})


csess = _CreateSess()
ad_create = _adapter(csess)
jira.JiraAdapter.create_task(ad_create, LONG_TITLE, "body")
stored_summary = csess.posts[-1][1]["fields"]["summary"]
chk("create_task stores the title truncated to the cap", stored_summary == STORED,
    f"{len(stored_summary)} chars")

dedup_sess = _SearchSess({"summary ~": [{"key": "EU-900", "fields": {"summary": STORED}}]})
ad_dedup = _adapter(dedup_sess)
found = jira.JiraAdapter.find_open_by_summary(ad_dedup, LONG_TITLE)
chk("a >240-char finding de-dups against the truncated ticket it already filed",
    found == "EU-900", repr(found))

# The compare must stay exact: a DIFFERENT long title that merely shares its first 240 chars is a
# distinct finding only if the stored twin differs — a same-prefix title IS the same stored ticket,
# so what must not regress is that an unrelated summary still does not match.
other = _SearchSess({"summary ~": [{"key": "EU-901", "fields": {"summary": "Something else open"}}]})
chk("an unrelated open ticket is not mistaken for a duplicate",
    jira.JiraAdapter.find_open_by_summary(_adapter(other), "Fix unguarded null deref") is None)
chk("short titles still de-dup case-insensitively (unchanged contract)",
    jira.JiraAdapter.find_open_by_summary(
        _adapter(_SearchSess({"summary ~": [{"key": "EU-902",
                                             "fields": {"summary": "Fix The Null Deref"}}]})),
        "fix the null deref  ") == "EU-902")


print("\n================ EU-365 JIRA-ADAPTER ROBUSTNESS QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
