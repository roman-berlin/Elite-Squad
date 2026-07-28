"""EU-374 (EU-301 step 5): auto-close the Epic when its VERIFY child lands.

EU-301 decomposes an oversized ticket into an Epic + Task children linked via `parent`, with a
mandatory final "Verify & close" child — but nothing ever transitioned the Epic itself, so
auto-split Epics accumulated open forever. This harness pins the close loop:

  1. scrum: the verify-child producer and the detector cannot drift — the child scrum.split files
     is recognised by scrum.is_verify_child; an ordinary work piece is not.
  2. jira adapter: parent_epic_key resolves the team-managed `parent` field (Epic parents only —
     a sub-task's Task parent never counts); epic_children queries `parent = "<EPIC>"` and returns
     key/summary/status/status_category per child.
  3. loop._land wiring (real _land over a spy git + stub backlog, eu81 pattern):
       • verify child lands + every sibling Done/QA  -> the Epic is closed with a summary comment
         naming the children + an `epic_autoclosed` audit event;
       • a sibling still To Do                        -> the Epic stays open;
       • an ordinary (non-verify) child lands         -> the Epic stays open;
       • the child-list snapshot MISSES the landed child (auth-blind 200 / hiccup) -> stays open
         (an empty search result must never vacuously "prove" the siblings done);
       • a backlog with no Epic support               -> land unaffected;
       • close_ticket RAISES                          -> the land still reports MERGED
         (best-effort: a board hiccup leaves the Epic open, never un-lands a merge).

All offline — SDK stubbed; no network, no real models.
"""
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import loop, scrum                      # noqa: E402
from orchestrator.backlog import jira                     # noqa: E402
from orchestrator.config import Config, AppConfig         # noqa: E402
from orchestrator.contracts import Ticket, Outcome        # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(n, c, d=""):
    results.append((n, bool(c), str(d)))


# ══════════════════════════════════════════════════════════════════════════════
# 1. scrum: producer/detector coherence
# ══════════════════════════════════════════════════════════════════════════════
_parent = SimpleNamespace(id="EU-500", summary="Big feature", description="do the thing",
                          acceptance_criteria=["works end to end"], ephemeral=False)
_vc = scrum.verify_child(_parent)
chk("verify_child: title carries the 'Verify & close:' prefix",
    _vc["title"].startswith("Verify & close:"), _vc["title"])
chk("verify_child: body carries the parent's AC",
    "works end to end" in _vc["body"], _vc["body"][:120])
chk("is_verify_child recognises the child scrum files (producer/detector coherence)",
    scrum.is_verify_child(_vc["title"], _vc["body"]) is True)
chk("is_verify_child: an ordinary work piece is NOT a verify child",
    scrum.is_verify_child("Add the /models list route", "What/Where/AC…") is False)
chk("is_verify_child: empty strings -> False",
    scrum.is_verify_child("", "") is False and scrum.is_verify_child(None, None) is False)
# Jira may re-render the title; the body marker alone still identifies the child.
chk("is_verify_child: body marker alone is enough (title edited on the board)",
    scrum.is_verify_child("Big feature — final check", _vc["body"]) is True)


# ══════════════════════════════════════════════════════════════════════════════
# 2. jira adapter: parent_epic_key + epic_children (fake session, eu365 pattern)
# ══════════════════════════════════════════════════════════════════════════════
class _Resp:
    def __init__(self, payload=None):
        self._p = payload or {}
        self.headers = {}
    def raise_for_status(self): pass
    def json(self): return self._p


class _EpicSess:
    """GET issue/<k> serves the parent field; POST search/jql serves the children."""
    def __init__(self, parent_field=None, children=None):
        self.parent_field = parent_field
        self.children = children or []
        self.jqls: list[str] = []
    def get(self, url, params=None, **k):
        return _Resp({"fields": {"parent": self.parent_field}})
    def post(self, url, json=None, **k):
        self.jqls.append((json or {}).get("jql", ""))
        return _Resp({"issues": self.children})


def _adapter(session):
    s = SimpleNamespace(app_name="Elite-Unit", base_url="https://x.atlassian.net", project="EU",
                        session=session)
    s._url = lambda p: f"https://x.atlassian.net/rest/api/3/{p.lstrip('/')}"
    s._raise_if_unauthenticated = lambda r: None
    return s


_epic_parent = {"key": "EU-500", "fields": {"issuetype": {"name": "Epic"}, "summary": "Big feature"}}
_task_parent = {"key": "EU-400", "fields": {"issuetype": {"name": "Task"}, "summary": "not an epic"}}

ad = _adapter(_EpicSess(parent_field=_epic_parent))
chk("parent_epic_key: an Epic parent resolves to its key",
    jira.JiraAdapter.parent_epic_key(ad, "EU-503") == "EU-500")
chk("parent_epic_key: a NON-Epic parent (sub-task under a Task) -> None",
    jira.JiraAdapter.parent_epic_key(_adapter(_EpicSess(parent_field=_task_parent)), "EU-503") is None)
chk("parent_epic_key: no parent at all -> None",
    jira.JiraAdapter.parent_epic_key(_adapter(_EpicSess(parent_field=None)), "EU-503") is None)

_children_payload = [
    {"key": "EU-501", "fields": {"summary": "piece 1",
                                 "status": {"name": "Done", "statusCategory": {"key": "done"}}}},
    {"key": "EU-502", "fields": {"summary": "piece 2",
                                 "status": {"name": "QA", "statusCategory": {"key": "indeterminate"}}}},
    {"key": "EU-503", "fields": {"summary": "Verify & close: Big feature",
                                 "status": {"name": "In Progress",
                                            "statusCategory": {"key": "indeterminate"}}}},
]
sess = _EpicSess(children=_children_payload)
kids = jira.JiraAdapter.epic_children(_adapter(sess), "EU-500")
chk("epic_children: queries parent = \"EU-500\"",
    any('parent = "EU-500"' in q for q in sess.jqls), str(sess.jqls))
chk("epic_children: returns every child with key + status",
    [c["key"] for c in kids] == ["EU-501", "EU-502", "EU-503"]
    and kids[0]["status"] == "Done" and kids[1]["status"] == "QA", str(kids))
chk("epic_children: statusCategory key is carried through",
    kids[0].get("status_category") == "done", str(kids[0]))


# ══════════════════════════════════════════════════════════════════════════════
# 3. loop._land wiring (real _land, spy git, stub backlog — eu81 pattern)
# ══════════════════════════════════════════════════════════════════════════════
tmp = Path(tempfile.mkdtemp())
app = AppConfig(name="Elite-Unit", repo_path=str(tmp), base_branch="dev",
                protected_branch="main", backlog_backend="none")
cfg = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), dry_run=False,
             sync_base_after_merge=False)
BRANCH = "autodev/EU-503"


class _SpyGit:
    def commit_all(self, *a, **k): return "feat_sha"
    def trial_merge(self, *a, **k): return True
    def changed_paths(self, *a, **k): return []
    def current_sha(self, *a, **k): return "merge_sha"
    def land_trial(self, temp): pass
    def delete_local_branch(self, branch): pass
    def delete_remote_branch(self, branch): pass
    def sync_main_base(self): return ""
    def abandon_trial(self, *a, **k): pass


class _EpicBacklog:
    """A Jira-shaped backlog with the EU-374 Epic helpers."""
    def __init__(self, epic_key="EU-500", children=None, fail_close=False):
        self.epic_key = epic_key
        self.children = children if children is not None else []
        self.fail_close = fail_close
        self.closed: list[tuple[str, str]] = []
        self.handed: list[tuple[str, str]] = []      # EU-734: (epic_key, status) hand-offs
        self.comments: list[tuple[str, str]] = []    # EU-734: (epic_key, body)
    # EU-734: the roll-up now hands the finished Epic to the Commander for sign-off (QA) instead of
    # closing it, so the stub records the TRANSITION the way it used to record the close. Both are
    # kept: `handed` is the new contract, `closed` proves nothing auto-closes any more.
    def set_status(self, t, status=None, *a, **k):
        key = getattr(t, "key", t)
        if key == self.epic_key:
            if self.fail_close:
                raise RuntimeError("board down")
            self.handed.append((key, status))
    def add_comment(self, t, body="", *a, **k):
        key = getattr(t, "key", t)
        if key == self.epic_key:
            self.comments.append((key, body))
    def get_task(self, key):
        import types as _t
        return _t.SimpleNamespace(key=key, id=key)
    def parent_epic_key(self, key): return self.epic_key
    def epic_children(self, epic_key): return list(self.children)
    def close_ticket(self, ticket_id, comment="", audit=None):
        if self.fail_close:
            raise RuntimeError("board down")
        self.closed.append((ticket_id, comment))
        return True


class _PlainBacklog:
    """No Epic support at all (e.g. a non-Jira backend)."""
    def set_status(self, *a, **k): pass
    def add_comment(self, *a, **k): pass


class _Audit:
    def __init__(self): self.events = []
    def record(self, e, **k): self.events.append({"event": e, **k})


class _Commenter:
    """Hermetic no-op commenter — the LLM ticket commenter is out of scope here."""
    def summarize_gate_event(self, *a, **k): return None
    def post_comment(self, *a, **k): pass
    def post_unverifiable_gaps(self, *a, **k): pass


def _kids(verify_status="In Progress", sibling2_status="QA", sibling2_cat="indeterminate"):
    return [
        {"key": "EU-501", "summary": "piece 1", "status": "Done", "status_category": "done"},
        {"key": "EU-502", "summary": "piece 2", "status": sibling2_status,
         "status_category": sibling2_cat},
        {"key": "EU-503", "summary": "Verify & close: Big feature", "status": verify_status,
         "status_category": "indeterminate"},
    ]


VERIFY_TKT = Ticket(id="EU-503", key="EU-503", summary="Verify & close: Big feature",
                    description=scrum.verify_child(_parent)["body"])
PLAIN_TKT = Ticket(id="EU-502", key="EU-502", summary="piece 2", description="What/Where/AC…")

bld = SimpleNamespace(summary="built it")
rev = SimpleNamespace(summary="reviewed it")

_orig = (loop.run_gate, loop._notify, loop._record_changelog)
loop.run_gate = lambda *a, **k: SimpleNamespace(passed=True, report="ok")
loop._notify = lambda *a, **k: None
loop._record_changelog = lambda *a, **k: None


def land(tkt, backlog, audit):
    return loop._land(tkt, app, cfg, _SpyGit(), backlog, audit, branch=BRANCH, iteration=1,
                      cost=0.0, build=bld, review=rev, commenter=_Commenter())


try:
    # (a) verify child lands, all siblings Done/QA -> the Epic closes with a summary comment.
    bl = _EpicBacklog(children=_kids())
    au = _Audit()
    rep = land(VERIFY_TKT, bl, au)
    # EU-734 (Commander 2026-07-28, "ok do it - no zombies"): the Epic is his acceptance unit for
    # the whole feature, so a finished Epic is now ROUTED TO QA for sign-off instead of closing
    # itself — Jira must never assert an acceptance no human performed. The rest of EU-374's
    # contract is unchanged and still asserted below: an open sibling keeps the Epic open, a board
    # failure leaves it open, and a non-Epic parent is a no-op.
    chk("all children finished -> Epic handed to QA for sign-off (EU-734)",
        bl.handed and bl.handed[0] == ("EU-500", "QA"), str(bl.handed))
    chk("…and NOTHING is auto-closed any more", not bl.closed, str(bl.closed))
    chk("the hand-off comment names the children",
        bl.comments and "EU-501" in bl.comments[0][1] and "EU-502" in bl.comments[0][1],
        str(bl.comments)[:200])
    chk("epic_ready_for_signoff audit event recorded (epic + children)",
        any(e["event"] == "epic_ready_for_signoff" and e.get("epic") == "EU-500" for e in au.events),
        str([e["event"] for e in au.events]))
    chk("land still reports MERGED", rep.outcome == Outcome.MERGED, str(rep.outcome))

    # (b) a sibling still To Do -> the Epic stays open.
    bl2 = _EpicBacklog(children=_kids(sibling2_status="To Do", sibling2_cat="new"))
    rep2 = land(VERIFY_TKT, bl2, _Audit())
    chk("a sibling still To Do -> Epic stays open", not bl2.closed, str(bl2.closed))
    chk("...and the land is unaffected", rep2.outcome == Outcome.MERGED, str(rep2.outcome))

    # (c) an ordinary (non-verify) child lands, even with every sibling done -> stays open;
    #     only the verify child (which runs LAST by design) triggers the close.
    bl3 = _EpicBacklog(children=[
        {"key": "EU-501", "summary": "piece 1", "status": "Done", "status_category": "done"},
        {"key": "EU-502", "summary": "piece 2", "status": "In Progress",
         "status_category": "indeterminate"},
        {"key": "EU-503", "summary": "Verify & close: Big feature", "status": "Done",
         "status_category": "done"},
    ])
    land(PLAIN_TKT, bl3, _Audit())
    chk("an ordinary child landing never closes the Epic", not bl3.closed, str(bl3.closed))

    # (d) the child snapshot MISSES the landed child (hiccup / auth-blind 200 empty result):
    #     an empty list must never vacuously prove the siblings done.
    bl4 = _EpicBacklog(children=[])
    rep4 = land(VERIFY_TKT, bl4, _Audit())
    chk("empty child snapshot -> Epic stays open (hiccup never vacuously closes)",
        not bl4.closed, str(bl4.closed))
    chk("...and the land is unaffected", rep4.outcome == Outcome.MERGED, str(rep4.outcome))

    # (e) a backlog with no Epic helpers -> nothing breaks.
    rep5 = land(VERIFY_TKT, _PlainBacklog(), _Audit())
    chk("no-Epic-support backlog -> land unaffected", rep5.outcome == Outcome.MERGED,
        str(rep5.outcome))

    # (f) close_ticket raising -> best-effort: land still MERGED, Epic left open for a human.
    bl6 = _EpicBacklog(children=_kids(), fail_close=True)
    au6 = _Audit()
    rep6 = land(VERIFY_TKT, bl6, au6)
    chk("close_ticket raising -> land still MERGED (never un-lands a merge)",
        rep6.outcome == Outcome.MERGED, str(rep6.outcome))
    chk("close_ticket raising -> no epic_autoclosed event claimed",
        not any(e["event"] == "epic_autoclosed" for e in au6.events),
        str([e["event"] for e in au6.events]))
finally:
    (loop.run_gate, loop._notify, loop._record_changelog) = _orig

print("\n================= EU-374 EPIC AUTO-CLOSE =================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
