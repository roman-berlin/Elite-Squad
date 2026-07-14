"""EU-284: auto-filed out-of-scope findings must not drop severity.

Two things regressed before this fix: (1) filing.file_findings threw the finding's declared
severity away — every auto-filed ticket landed at the project's default priority (Medium)
regardless of how critical the officer said it was; (2) the out-of-scope path never notified,
so a CRITICAL finding could be filed and then rot unnoticed (EU-252 starvation compounds this).

Exercised with STUB backlog + STUB _notify (no Jira, no network):
  1) severity -> Jira priority mapping (CRITICAL/HIGH/MEDIUM/LOW/absent) via filing.file_findings.
  2) JiraAdapter.create_task builds `fields["priority"]` only when a priority is passed.
  3) loop._route_out_of_scope fires exactly one CRITICAL alert naming the filed key(s)+title(s);
     MEDIUM/LOW-only findings stay silent (current behavior).
  4) de-dup (find_open_by_summary) is unchanged: no create_task call, no CRITICAL alert, and the
     [officer_label, "autofiled"] labels are still passed on newly-filed tickets.
"""
import sys
import types

sys.path.insert(0, ".")

# Stub the Agent SDK so importing the orchestrator package never reaches the network.
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

from orchestrator import filing
from orchestrator.backlog.jira import JiraAdapter
from orchestrator.config import AppConfig

results = []


def chk(name, cond, detail=""):
    results.append((name, bool(cond), detail))


# --------------------------------------------------------------------------------------------- #
# Stub backlog — records the `priority` kwarg create_task was called with (the thing filing.py
# must now pass) alongside the labels/assignee contract eu42_filing_test.py already covers.
# --------------------------------------------------------------------------------------------- #
class StubBacklog:
    def __init__(self, open_summaries=None, assignee="you"):
        self.assignee = assignee
        self._open = {s.strip().lower(): k for s, k in (open_summaries or {}).items()}
        self.created = []          # (summary, labels, issue_type, priority)
        self.dedup_lookups = []

    def find_open_by_summary(self, summary):
        self.dedup_lookups.append(summary)
        return self._open.get(str(summary).strip().lower())

    def create_task(self, summary, description, labels=None, issue_type="Task", priority=None):
        key = f"EU-{900 + len(self.created)}"
        self.created.append((summary, list(labels or []), issue_type, priority))
        return key


app = AppConfig(name="Elite-Unit", repo_path="/tmp/x", base_branch="dev",
                protected_branch="main", backlog_backend="none")

REPORT = (
    "Build summary: implemented the in-scope change.\n"
    "Noticed off-spec issues while in there.\n"
    "===TICKETS===\n"
    '[{"title": "Auth bypass on admin route", "type": "Bug", "severity": "CRITICAL",'
    ' "body": "orchestrator/x.py skips the auth guard"},'
    ' {"title": "Fix unguarded null deref in parser", "type": "Bug", "severity": "HIGH",'
    ' "body": "orchestrator/x.py crashes on empty input"},'
    ' {"title": "Stale config key still referenced", "type": "Task", "severity": "MEDIUM",'
    ' "body": "remove dead flag"},'
    ' {"title": "Typo in log message", "type": "Task", "severity": "LOW",'
    ' "body": "cosmetic only"},'
    ' {"title": "No severity given for this one", "type": "Task",'
    ' "body": "severity key omitted entirely"}]\n'
    "===END===\n"
)

# --- 1) severity -> Jira priority mapping, per finding ----------------------------------------- #
stub = StubBacklog(open_summaries={}, assignee="commander-acct")
filing.make_backlog = lambda a: stub
res = filing.file_findings(app, "out-of-scope", REPORT)

chk("all five findings filed", res.filed_n == 5, str(res.filed))
by_title = {s: p for s, _labels, _it, p in stub.created}
chk("CRITICAL -> priority Highest", by_title["Auth bypass on admin route"] == "Highest", str(by_title))
chk("HIGH -> priority High", by_title["Fix unguarded null deref in parser"] == "High", str(by_title))
chk("MEDIUM -> priority Medium", by_title["Stale config key still referenced"] == "Medium", str(by_title))
chk("LOW -> priority Low", by_title["Typo in log message"] == "Low", str(by_title))
chk("no severity -> priority None (Jira keeps project default Medium)",
    by_title["No severity given for this one"] is None, str(by_title))
chk("out-of-scope label still applied to every filed ticket",
    all("out-of-scope" in labels for _, labels, _, _ in stub.created), str(stub.created))
chk("filed_critical records exactly the CRITICAL finding's (key, title)",
    len(res.filed_critical) == 1 and res.filed_critical[0][1] == "Auth bypass on admin route",
    str(res.filed_critical))

# --- 2) de-dup path is unchanged: no create_task call for the matched finding, no CRITICAL alert - #
stub2 = StubBacklog(open_summaries={"Auth bypass on admin route": "EU-500"}, assignee="commander-acct")
filing.make_backlog = lambda a: stub2
res2 = filing.file_findings(app, "out-of-scope", REPORT)
chk("de-duped CRITICAL finding recorded in result.deduped", res2.deduped == ["EU-500"], str(res2.deduped))
chk("de-duped finding did NOT call create_task",
    all("Auth bypass" not in s for s, *_ in stub2.created), str(stub2.created))
chk("de-duped finding does not appear in filed_critical",
    all(k != "EU-500" for k, _t in res2.filed_critical), str(res2.filed_critical))
chk("the other four findings still filed with correct labels",
    res2.filed_n == 4 and all("out-of-scope" in labels for _, labels, _, _ in stub2.created),
    str(stub2.created))

# --- 3) MEDIUM/LOW-only report: nothing critical, filed_critical stays empty -------------------- #
MEDIUM_LOW_REPORT = (
    "===TICKETS===\n"
    '[{"title": "Stale config key still referenced", "type": "Task", "severity": "MEDIUM",'
    ' "body": "remove dead flag"},'
    ' {"title": "Typo in log message", "type": "Task", "severity": "LOW", "body": "cosmetic only"}]\n'
    "===END===\n"
)
stub3 = StubBacklog()
filing.make_backlog = lambda a: stub3
res3 = filing.file_findings(app, "out-of-scope", MEDIUM_LOW_REPORT)
chk("MEDIUM/LOW-only report files no CRITICAL", res3.filed_critical == [], str(res3.filed_critical))

# --------------------------------------------------------------------------------------------- #
# 4) JiraAdapter.create_task: priority field only appears when a priority is passed.
# --------------------------------------------------------------------------------------------- #
class _StubResp:
    def __init__(self, key): self._key = key
    def raise_for_status(self): return None
    def json(self): return {"key": self._key}


class _StubSession:
    def __init__(self):
        self.last_json = None

    def post(self, url, json=None):
        self.last_json = json
        return _StubResp("EU-777")


adapter = JiraAdapter.__new__(JiraAdapter)   # bypass __init__ (no network / config needed)
adapter.base_url = "https://example.atlassian.net"
adapter.project = "EU"
adapter.assignee = "commander-acct"
adapter.session = _StubSession()

adapter.create_task("No priority given", "body", priority=None)
chk("priority=None builds fields WITHOUT a 'priority' key",
    "priority" not in adapter.session.last_json["fields"], str(adapter.session.last_json))

adapter.create_task("Critical thing", "body", priority="Highest")
chk("priority='Highest' builds fields WITH {'priority': {'name': 'Highest'}}",
    adapter.session.last_json["fields"].get("priority") == {"name": "Highest"},
    str(adapter.session.last_json))

# --------------------------------------------------------------------------------------------- #
# 5) loop._route_out_of_scope: exactly one CRITICAL alert naming the filed key(s)+title(s);
#    MEDIUM/LOW-only findings emit zero alerts. The in-scope blocker alert (loop.py:1284) is a
#    separate code path entirely and is untouched by this ticket.
# --------------------------------------------------------------------------------------------- #
import orchestrator.loop as loop  # noqa: E402  (import after sdk stub, matches repo convention)


class _StubTicket:
    id = "AUTO-1"


class _StubApp:
    name = "automatixy"


class _StubCfg:
    dry_run = False
    out_of_scope_autofile = True


class _StubAudit:
    def __init__(self): self.events = []
    def record(self, event, **kw): self.events.append({"event": event, **kw})


notify_calls = []
loop._notify = lambda cfg, text: notify_calls.append(text)

# 5a) one CRITICAL among the findings -> exactly one alert naming the filed key + title.
notify_calls.clear()
stub_backlog_5a = StubBacklog()
filing.make_backlog = lambda a: stub_backlog_5a
loop._route_out_of_scope(_StubCfg(), _StubTicket(), _StubApp(), _StubAudit(), REPORT, source="reviewer")
chk("exactly one CRITICAL alert fired", len(notify_calls) == 1, str(notify_calls))
chk("the alert names the filed key and title",
    len(notify_calls) == 1 and "EU-900" in notify_calls[0] and "Auth bypass on admin route" in notify_calls[0],
    str(notify_calls))

# 5b) MEDIUM/LOW-only report -> zero alerts.
notify_calls.clear()
stub_backlog_5b = StubBacklog()
filing.make_backlog = lambda a: stub_backlog_5b
loop._route_out_of_scope(_StubCfg(), _StubTicket(), _StubApp(), _StubAudit(), MEDIUM_LOW_REPORT, source="reviewer")
chk("MEDIUM/LOW-only findings emit zero alerts", notify_calls == [], str(notify_calls))

passed = sum(1 for _, c, _ in results if c)
for n, c, d in results:
    print(f"  {'✓' if c else '✗'} {n}" + (f"  [{d}]" if (not c and d) else ""))
print(f"{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
