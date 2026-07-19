"""Stuck-column routing QA (2026-07-09): stranded tickets must move to QA / Blocked, visibly.

Live incident: 14 tickets sat In Progress because (a) set_status had no fallback when a target
column is missing — every land had been TRYING "QA" and silently no-oping (comment-only) before
the board gained the column; (b) Blocked parks carried no Jira-visible reason; (c) an ERRORED
ticket got zero comment and no state change — invisible on the board; (d) a Jira hiccup AFTER a
successful merge propagated into _exception_report and mislabeled the land as a ticket_exception.

Pins:
  1. set_status fallback chain: QA → Done/Closed when the board lacks QA; primary wins when present.
  2. No-op when the ticket already sits in ANY candidate column (never bounce a Done ticket back).
  3. Missing everything → the manual-move comment, no raise.
  4. _park_on_tracker(reason=…) posts the ⛔ reason comment after the Blocked transition.
  5. _exception_report posts the ❌ error comment on the ERRORED tail (and skips ephemeral/dry-run).
  6. (source) the post-land status update is exception-wrapped; configs route Needs Human → Blocked.
"""
import sys, types, asyncio
from pathlib import Path

req = types.ModuleType("requests")
req.Session = lambda *a, **k: None
req.RequestException = Exception
sys.modules["requests"] = req
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator.backlog import jira

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# ---------- a session stub serving canned status + transitions ---------- #
class _Resp:
    def __init__(s, payload): s._p = payload
    def raise_for_status(s): pass
    def json(s): return s._p

class _Sess:
    def __init__(s, current_status, transitions):
        s.current, s.transitions, s.posts = current_status, transitions, []
    def get(s, url, params=None, timeout=None):
        if url.endswith("/transitions"):
            return _Resp({"transitions": [{"id": str(i), "to": {"name": n}}
                                          for i, n in enumerate(s.transitions, 1)]})
        return _Resp({"fields": {"status": {"name": s.current}}})
    def post(s, url, json=None, timeout=None):
        s.posts.append((url, json)); return _Resp({})

def _adapter(current, transitions, status_map=None, fallbacks=None):
    comments = []
    ns = types.SimpleNamespace(
        session=_Sess(current, transitions),
        status_map=status_map or {},
        status_fallbacks=fallbacks if fallbacks is not None else {"QA": ["Done", "Closed"]},
        _url=lambda path: f"https://x/rest/api/3/{path}",
        _current_status=lambda key: current,
        add_comment=lambda t, body: comments.append(body),
    )
    ns._comments = comments
    return ns

tk = types.SimpleNamespace(key="AUTO-73", id="AUTO-73")

# 1) primary present → primary chosen
a = _adapter("In Progress", ["To Do", "QA", "Done"])
jira.JiraAdapter.set_status(a, tk, "QA")
chk("QA transition chosen when the board has it",
    a.session.posts and a.session.posts[0][1]["transition"]["id"] == "2", a.session.posts)

# 2) primary missing → falls back to Done (the boards-without-QA chain)
a = _adapter("In Progress", ["To Do", "Done"])
jira.JiraAdapter.set_status(a, tk, "QA")
chk("QA falls back to Done when the QA column is absent",
    a.session.posts and a.session.posts[0][1]["transition"]["id"] == "2", a.session.posts)
chk("no manual-move comment when a fallback lands", not a._comments, a._comments)

# 3) already in a candidate column → strict no-op (never bounce Done → QA)
a = _adapter("Done", ["To Do", "QA"])
jira.JiraAdapter.set_status(a, tk, "QA")
chk("ticket already Done: QA request is a no-op (no bounce, no comment)",
    not a.session.posts and not a._comments, (a.session.posts, a._comments))

# 4) nothing available → manual-move comment, no raise
a = _adapter("In Progress", ["To Do"])
jira.JiraAdapter.set_status(a, tk, "QA")
chk("no candidate at all → the manual-move comment names the primary target",
    len(a._comments) == 1 and "'QA'" in a._comments[0], a._comments)
chk("…and nothing was posted to transitions", not a.session.posts)

# 5) status_map applies to fallback names too
a = _adapter("In Progress", ["Fertig"], status_map={"Done": "Fertig"})
jira.JiraAdapter.set_status(a, tk, "QA")
chk("fallbacks are re-mapped through status_map (Done → Fertig)",
    a.session.posts and a.session.posts[0][1]["transition"]["id"] == "1", a.session.posts)

# ---------- decisions._park_on_tracker posts the reason ---------- #
from orchestrator import decisions
from orchestrator.backlog import base as backlog_base

class _FakeBacklog:
    def __init__(s): s.statuses, s.comments = [], []
    def set_status(s, t, status): s.statuses.append(status)
    def add_comment(s, t, body): s.comments.append(body)
    def latest_answer(s, t): return ""

fb = _FakeBacklog()
_orig_make = backlog_base.make_backlog
backlog_base.make_backlog = lambda app: fb
try:
    cfg = types.SimpleNamespace(
        dry_run=False,
        app=lambda name: types.SimpleNamespace(backlog_backend="jira"))
    tkt = types.SimpleNamespace(key="AUTO-98", id="AUTO-98", ephemeral=False)
    decisions._park_on_tracker(cfg, tkt, "automatixy", reason="max passes — PM escalated: fix X")
    chk("park transitions to Blocked", fb.statuses == ["Blocked"], fb.statuses)
    chk("park posts the ⛔ reason comment",
        fb.comments and fb.comments[0].startswith("⛔ Blocked") and "max passes" in fb.comments[0],
        fb.comments)
    fb2 = _FakeBacklog()
    backlog_base.make_backlog = lambda app: fb2
    decisions._park_on_tracker(cfg, tkt, "automatixy")
    chk("no reason → no comment (baseline behaviour intact)", fb2.comments == [], fb2.comments)
finally:
    backlog_base.make_backlog = _orig_make

# ---------- loop._exception_report leaves the ❌ trace ---------- #
import orchestrator.loop as loop
loop._notify = lambda c, t: None

class _Audit:
    def __init__(s): s.ev = []
    def record(s, e, **k): s.ev.append(e)

lcfg = types.SimpleNamespace(dry_run=False)
lapp = types.SimpleNamespace(name="automatixy")
ltk = types.SimpleNamespace(id="AUTO-73", key="AUTO-73", ephemeral=False, summary="s")
fb3 = _FakeBacklog()
r = asyncio.run(loop._exception_report(lcfg, ltk, lapp, RuntimeError("Claude Code returned an error result: success"), _Audit(), backlog=fb3))
chk("ERRORED tail posts the ❌ build-error comment on the ticket",
    fb3.comments and fb3.comments[0].startswith("❌ Build errored") and "success" in fb3.comments[0],
    fb3.comments)
chk("…and does NOT transition (autopilot retries from In Progress)", fb3.statuses == [], fb3.statuses)
chk("report outcome is ERRORED", r.outcome.name == "ERRORED", r.outcome)

fb4 = _FakeBacklog()
etk = types.SimpleNamespace(id="X#1", key="X#1", ephemeral=True, summary="s")
asyncio.run(loop._exception_report(lcfg, etk, lapp, RuntimeError("boom"), _Audit(), backlog=fb4))
chk("ephemeral ticket → no comment", fb4.comments == [], fb4.comments)
_ret = asyncio.run(loop._exception_report(lcfg, ltk, lapp, RuntimeError("boom"), _Audit(),
                                          backlog=None))
chk("backlog=None (pre-bind path) → completes and returns the escalation report",
    getattr(_ret, "outcome", None) is not None, repr(_ret)[:120])

# ---------- source pins ---------- #
_loop_src = Path("orchestrator/loop.py").read_text(encoding="utf-8")
chk("post-land status update is exception-wrapped (a Jira hiccup can't mislabel a merged land)",
    "ticket status update skipped" in _loop_src)
chk("run-loop pre-binds backlog=None before the try (no NameError in the handler)",
    "backlog = None   # pre-bind" in _loop_src)
_ex_src = Path("config.example.yaml").read_text(encoding="utf-8")
chk("config.example.yaml routes Needs Human → Blocked",
    'Needs Human: "Blocked"' in _ex_src)

print("\n========== STUCK-COLUMN STATE ROUTING QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
