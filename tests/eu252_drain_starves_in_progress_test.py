"""EU-252: drain starves In Progress tickets — a jql override + cap-before-tier-split break
resume-first.

Root cause (two independent breaks, both fixed here):

  1. backlog/jira.py get_ready_tasks: when `jql:` (jql_override) is set, it REPLACES the
     In-Progress-first two-query contract entirely with a single flat query. If that override's
     ORDER BY (e.g. "priority DESC, Rank ASC") ranks an In Progress ticket below `limit`, it never
     enters the drawn window at all — no downstream tiering can rescue it.

  2. autopilot.py worklist assembly: `raw = [...][:cap]` truncated the FULL drawn window to `cap`
     BEFORE the tier-1 (In Progress) / tier-3 (To Do) split — so with cap=1 and an override, the
     resume-first split became a no-op: whatever the flat query's single top row was (often a fresh
     To Do ticket) won, regardless of an older In Progress ticket sitting lower in that same window.

This harness pins both fixes plus the "cap bounds only fresh To Do" contract, and confirms the
eu61 answered-resume behaviour is untouched.
"""
import sys, types, asyncio, tempfile
from pathlib import Path

# --- stub heavy deps so modules import without them ---------------------
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
from orchestrator import autopilot, intake
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


def _ticket(tid, status):
    return Ticket(id=tid, key=tid, summary=f"ticket {tid}",
                  description="", acceptance_criteria=[], status=status)


def _app():
    return AppConfig(name="eu", repo_path="/tmp", base_branch="dev",
                     protected_branch="main", backlog_backend="jira",
                     backlog={"base_url": "https://x.atlassian.net", "project_key": "EU"})


# ========================================================================= #
# Part 1 — backlog/jira.py: get_ready_tasks issues a preceding In-Progress
# query under a jql_override, so a fragment the override alone ranks below
# `limit` is still drawn, and appears BEFORE the To Do rows. Deduped by key.
# ========================================================================= #

class FakeResp:
    def __init__(self, issues):
        self.status_code = 200
        self._issues = issues
        self.headers = {}
    def raise_for_status(self):
        pass
    def json(self):
        return {"issues": self._issues}


class FakeSession:
    """Routes by JQL: an 'In Progress' status clause -> the in-progress fixture,
    anything else (the override) -> the flat, priority-ranked fixture."""
    def __init__(self, in_progress_issues, override_issues):
        self.in_progress_issues = in_progress_issues
        self.override_issues = override_issues
        self.posts = []

    def post(self, url, json=None):
        self.posts.append((url, json))
        jql = json["jql"]
        if 'status = "In Progress"' in jql:
            return FakeResp(self.in_progress_issues)
        return FakeResp(self.override_issues)


def make_adapter(jql_override, in_progress_issues, override_issues):
    s = types.SimpleNamespace(
        app_name="Elite-Unit", base_url="https://toibis.atlassian.net",
        jql_override=jql_override, queue_statuses=["In Progress", "To Do"],
        project="EU", assignee=None, only_mine=True, require_label=False, label="autodev",
        _url=lambda path: f"https://toibis.atlassian.net/rest/api/3/{path}",
        _fields=lambda: ["summary"],
        _to_ticket=lambda issue: Ticket(id=issue["key"], key=issue["key"], summary="s",
                                        description="", acceptance_criteria=[],
                                        status=issue.get("status")),
        session=FakeSession(in_progress_issues, override_issues),
    )
    s._jql_for_status = lambda st: jira.JiraAdapter._jql_for_status(s, st)
    s._raise_if_unauthenticated = lambda resp: jira.JiraAdapter._raise_if_unauthenticated(s, resp)
    return s


# Flat override query (priority DESC, Rank ASC): 5 To Do rows outrank the lone In Progress
# fragment, which sits at position 6 — beyond a limit of 3, so it would never surface without
# a preceding In-Progress-only query.
OVERRIDE_ROWS = (
    [{"key": f"EU-{200+i}", "status": "To Do"} for i in range(5)] +
    [{"key": "EU-233", "status": "In Progress"}]
)
IN_PROGRESS_ROWS = [{"key": "EU-233", "status": "In Progress"}]

adapter = make_adapter('project = "EU" ORDER BY priority DESC, Rank ASC', IN_PROGRESS_ROWS, OVERRIDE_ROWS)
got = jira.JiraAdapter.get_ready_tasks(adapter, 3)
got_keys = [t.key for t in got]

chk("EU-233 (In Progress, ranked below `limit` by the override) is still drawn",
    "EU-233" in got_keys, str(got_keys))
chk("EU-233 appears BEFORE the To Do rows from the override query",
    got_keys and got_keys[0] == "EU-233", str(got_keys))
chk("no ticket appears twice (deduped by key)",
    len(got_keys) == len(set(got_keys)), str(got_keys))

# A ticket that legitimately has no In Progress fragment at all: override-only behaviour intact.
adapter_empty_ip = make_adapter('project = "EU" ORDER BY priority DESC, Rank ASC', [], OVERRIDE_ROWS)
got2 = jira.JiraAdapter.get_ready_tasks(adapter_empty_ip, 3)
chk("with no In Progress fragment, override still returns To Do rows normally",
    [t.key for t in got2] == ["EU-200", "EU-201", "EU-202"], str([t.key for t in got2]))

# Non-override path is untouched: only the normal two-query (queue_statuses) contract runs.
adapter_no_override = make_adapter(None, IN_PROGRESS_ROWS, OVERRIDE_ROWS)
got3 = jira.JiraAdapter.get_ready_tasks(adapter_no_override, 5)
chk("no jql_override -> unchanged two-query (queue_statuses) contract still works",
    "EU-233" in [t.key for t in got3], str([t.key for t in got3]))


# ========================================================================= #
# Part 2 — autopilot.py worklist assembly: tier ordering survives BOTH the
# override and the cap. cap bounds only fresh To Do, never In Progress.
# ========================================================================= #

def _run_cycle(drain_tickets, resumed_map, blocked_set, cap):
    """Mirrors the EU-252 worklist-assembly code in autopilot.py: filter blocked over the FULL
    drawn window, split tier-1/tier-3 over that full window, cap ONLY the To Do slice."""
    app = _app()
    raw = [(app, t) for t in drain_tickets if t.id not in blocked_set]

    in_progress = [(a, t) for (a, t) in raw
                   if t.status and "progress" in t.status.lower()]
    to_do = [(a, t) for (a, t) in raw
             if not (t.status and "progress" in t.status.lower())][:cap]

    in_drain = {t.id for _, t in raw}
    answered_items = [v for k, v in resumed_map.items() if k not in in_drain]

    worklist = in_progress + answered_items + to_do
    return [t.id for _, t in worklist]


# EU-233 (In Progress) sits BELOW several higher-priority To Do tickets in the drawn window
# (mirrors what a jql_override with priority DESC hands back) — with cap=1, the pre-fix code
# sliced `raw` to the single top (To Do) row before ever splitting tiers.
ip = _ticket("EU-233", "In Progress")
higher_todo = [_ticket(f"EU-24{i}", "To Do") for i in range(5)]
drawn_window = higher_todo + [ip]   # In Progress ranked LAST in the flat/override order

order = _run_cycle(drawn_window, resumed_map={}, blocked_set=set(), cap=1)
chk("tier ordering survives both the override and the cap: In Progress ticket runs first",
    order[:1] == ["EU-233"], str(order))

# cap bounds only fresh To Do: N In Progress + M To Do, cap=1 -> all N In Progress ride through,
# exactly 1 To Do makes it in.
ip2 = _ticket("EU-234", "In Progress")
many_todo = [_ticket(f"EU-25{i}", "To Do") for i in range(4)]
order2 = _run_cycle([ip, ip2] + many_todo, resumed_map={}, blocked_set=set(), cap=1)
chk("cap bounds only fresh To Do: all In Progress tickets ride through uncapped",
    "EU-233" in order2 and "EU-234" in order2, str(order2))
chk("cap bounds only fresh To Do: exactly 1 To Do ticket admitted (cap=1)",
    sum(1 for k in order2 if k.startswith("EU-25")) == 1, str(order2))
chk("cap bounds only fresh To Do: total worklist size is N(In Progress) + cap",
    len(order2) == 3, str(order2))


# ========================================================================= #
# Part 3 — full integration: one autopilot cycle with a jql override, cap=1,
# proves the fix end-to-end (drain -> worklist).
# ========================================================================= #

tmp = Path(tempfile.mkdtemp())
autopilot._PID_FILE = tmp / "general-autopilot.pid"
cfg = Config(
    apps=[AppConfig(name="eu", repo_path=str(tmp), base_branch="dev",
                    protected_branch="main", backlog_backend="jira",
                    backlog={"base_url": "https://x.atlassian.net", "project_key": "EU"})],
    audit_path=str(tmp / "audit.jsonl"),
    max_tickets_per_run=1,   # cap=1, mirrors config.yaml:44
)

# Simulate a drain that already applied a jql_override ranking: 5 To Do tickets ahead of the
# lone In Progress fragment, exactly as backlog/jira.get_ready_tasks (fixed above) would hand
# back once it draws the In-Progress fragment into the window at all.
DRAIN = [_ticket(f"EU-24{i}", "To Do") for i in range(5)] + [_ticket("EU-233", "In Progress")]

autopilot.usage.budget_status = lambda c: {"over": False, "alert": False, "used": 0, "cap": 999, "pct": 0.0}
autopilot.notify.configured = lambda: False
autopilot.notify.send = lambda *a, **k: None
autopilot._commander_mid_git = lambda c: None
autopilot._resumable_answered = lambda cfg, app, blocked: {}
autopilot.load_blocked = lambda cfg: set()
autopilot.save_blocked = lambda cfg, s: None
autopilot.load_error_counts = lambda cfg: {}
autopilot.save_error_counts = lambda cfg, d: None

intake.from_drain = lambda cfg, app, n: [(cfg.apps[0], t) for t in DRAIN]
intake.LAST_DRAIN_ERRORS.clear()

worklist_seen = []
async def _fake_loop(cfg, wl, audit):
    worklist_seen.extend(t.id for _, t in wl)
    return []
autopilot.run_loop = _fake_loop

asyncio.run(autopilot.autopilot(cfg, once=True))

chk("integration: with jql override + cap=1, EU-233 (In Progress) is picked ahead of the To Do queue",
    worklist_seen[:1] == ["EU-233"], str(worklist_seen))


# ========================================================================= #
# Part 4 — existing eu61 answered-resume contract is unchanged by this fix.
# ========================================================================= #

blocked_ans = _ticket("EU-99", None)
resumed_map = {"EU-99": (_app(), blocked_ans)}
order_ans = _run_cycle([ip] + many_todo, resumed_map=resumed_map, blocked_set=set(), cap=1)
chk("eu61 unchanged: answered resume still rides above the (To Do-only) cap",
    "EU-99" in order_ans, str(order_ans))
chk("eu61 unchanged: In Progress before answered before To Do",
    order_ans.index("EU-233") < order_ans.index("EU-99"), str(order_ans))


print("\n===== EU-252 DRAIN-STARVES-IN-PROGRESS QA =====")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-" * 48)
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
