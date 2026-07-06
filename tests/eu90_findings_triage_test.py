"""EU-90: PM findings-triage routes reviewer quality_issues to the right destination.

Three routes are exercised, all using mocks (no live LLM or Jira calls):

  Test 1 — in_scope   : PM triage returns one in-scope finding → Jira add_comment is
                        posted with the issue title; _route_out_of_scope is NOT invoked
                        with source='pm-findings'.

  Test 2 — out_of_scope: PM triage returns one out-of-scope finding → _route_out_of_scope
                        is called with source='pm-findings'; no Jira comment is posted
                        from the EU-90 route.

  Test 3 — decision   : PM triage returns one decision-bucket finding → decisions.add is
                        called with a bulleted brief; neither add_comment nor
                        _route_out_of_scope (source='pm-findings') is invoked.

Import / mock pattern matches eu42_route_out_of_scope_test.py.
"""
import sys
import types
import json
import asyncio
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Stub the Agent SDK so importing the orchestrator never reaches the network.
# ---------------------------------------------------------------------------
sdk = types.ModuleType("claude_agent_sdk")

class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self

sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.loop as loop
from orchestrator import pm as pm_mod
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import (
    BuildResult, GateResult, QualityIssue, ReviewResult, TestEngineerResult,
    Ticket, Verdict,
)
from orchestrator.pm import FindingsTriage

# ---------------------------------------------------------------------------
# Test result tracking
# ---------------------------------------------------------------------------
results = []

def chk(name, cond, detail=""):
    """Record one pass/fail assertion."""
    results.append((name, bool(cond), detail))


# ---------------------------------------------------------------------------
# Common fixtures
# ---------------------------------------------------------------------------
tmp = Path(tempfile.mkdtemp())

app_cfg = AppConfig(
    name="Elite-Unit",
    repo_path=str(tmp),
    base_branch="dev",
    protected_branch="main",
    backlog_backend="none",
)

ticket = Ticket(
    id="EU-90",
    key="EU-90",
    summary="auto-route reviewer findings",
    description="test fixture",
    ephemeral=False,
    app="Elite-Unit",
)

BRANCH = "autodev/EU-90-test"

# --- one unique quality issue per route so we can verify it appears in the output ---
ISSUE_IN_SCOPE = QualityIssue(severity="major", area="logic",
                               detail="in-scope-detail-unique-x1")
ISSUE_OUT_OF_SCOPE = QualityIssue(severity="minor", area="style",
                                   detail="out-of-scope-detail-unique-x2")
ISSUE_DECISION = QualityIssue(severity="major", area="product",
                               detail="decision-detail-unique-x3")
# A SECOND, distinct in-scope finding for the cross-pass signature test (EU-90 rejection #2).
ISSUE_IN_SCOPE_2 = QualityIssue(severity="blocker", area="reliability",
                                 detail="in-scope-detail-unique-y9")


# ---------------------------------------------------------------------------
# Shared stubs (overwritten per-test run as needed)
# ---------------------------------------------------------------------------

class StubGit:
    """Git stub that always reports files changed (so the loop proceeds past build)."""
    def has_changes(self): return True
    def diff_against_base(self): return "--- a\n+++ b\n@@ stub diff @@"
    def changed_paths(self): return []
    def branch(self): return BRANCH


class StubBacklog:
    """Captures add_comment calls; all writes are no-ops."""
    def __init__(self):
        self.comments: list[tuple[object, str]] = []

    def add_comment(self, ticket, body: str) -> None:
        self.comments.append((ticket, body))

    def set_status(self, *a, **k): pass

    def find_open_by_summary(self, *a, **k): return None
    def create_task(self, *a, **k): return "EU-999"


class StubAudit:
    """Captures audit events."""
    def __init__(self):
        self.events: list[dict] = []

    def record(self, event, **kw):
        self.events.append({"event": event, **kw})

    @staticmethod
    def diff_hash(*a):
        return "aabbcc"


# --- Builder: returns a successful single-pass build with no halt language ---
class StubBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "stub")

    @staticmethod
    async def build(req, app, cfg, audit=None, **_):
        return BuildResult(
            ok=True,
            summary="stub: changed one file",
            cost_usd=0.0,
            num_turns=1,
            raw="stub: changed one file",
            tools=[],
        )


# --- Reviewer: returns FAIL with a single quality_issue (replaced per test) ---
_review_issue: list[QualityIssue] = []   # replaced per test
_review_raw: list[str] = [""]            # review.raw (reviewer ===TICKETS=== block), replaced per test


async def _stub_review(diff, ticket, app, cfg, iteration, store=None, build_artifact=None):
    return ReviewResult(
        verdict=Verdict.FAIL,
        spec_met=False,
        quality_issues=list(_review_issue),
        required_changes=["fix the issue"],
        summary="stub reviewer",
        needs_human=False,
        raw=_review_raw[0],   # default "" → reviewer _route_out_of_scope is a no-op
    )


# ---------------------------------------------------------------------------
# Wire permanent stubs into the loop module
# ---------------------------------------------------------------------------
notify_calls: list[str] = []   # every _notify(cfg, text) body the loop emits this run

loop.builder_mod = StubBuilder
loop.reviewer_mod.review = _stub_review
loop._notify = lambda cfg, text: notify_calls.append(text)
loop.run_gate = lambda app, paths=None, **_: GateResult(passed=True, report="")

# Stub async _decision_brief so the post-loop escalation path doesn't network.
async def _stub_decision_brief(cfg, ticket_id, raw):
    return ""

loop._decision_brief = _stub_decision_brief
loop.decisions.reply_hint = lambda ticket_id: ""

# pm.triage (exhaustion path) — return ESCALATE so the post-loop path is short-circuit clean.
async def _stub_pm_triage(cfg, app_name, ticket_id, **_):
    return {"action": "ESCALATE", "text": "stub escalate", "raw": ""}

pm_mod.triage = _stub_pm_triage

# Skip the Test Engineer gate (prevents an Opus call and simplifies the mock surface).
# We set test_gate=False on every Config below.


# ---------------------------------------------------------------------------
# Per-test helper: capture calls to decisions.add and _route_out_of_scope
# ---------------------------------------------------------------------------
dec_calls: list[dict] = []      # {"question": ..., "entry_id": ...}
route_calls: list[dict] = []    # {"source": ...}
triage_args: dict = {}          # the {diff, files_changed} the loop passed to triage_findings
# Faithful in-memory mirror of pending_decisions.json. The loop's page guard (EU-90) snapshots
# decisions.load() before decisions.add() and pages only when a NEW id appears, so the stubs must
# honour the EU-89 dedup contract: a repeated (ticket, question) returns the EXISTING id with no
# new row, and load() reflects exactly what add() stored.
dec_store: list[dict] = []


def _install_capture_hooks():
    """Reset accumulators and re-install capturing mocks (idempotent per test run)."""
    dec_calls.clear()
    route_calls.clear()
    notify_calls.clear()
    dec_store.clear()

    def _capture_decisions_add(cfg, ticket, app_name, question, entry_id=None, **kw):
        dec_calls.append({"question": question, "entry_id": entry_id})
        eid = entry_id or ticket.id
        base = str(ticket.id).split("#", 1)[0]
        fp = " ".join(question.lower().split())   # mirror decisions._question_fingerprint's normalise
        for it in dec_store:
            if str(it["id"]).split("#", 1)[0] == base and it["fp"] == fp:
                return it["id"]            # EU-89 dedup hit → existing id, no new row written
        dec_store.append({"id": eid, "fp": fp})
        return eid

    def _capture_decisions_load(cfg):
        return [{"id": it["id"]} for it in dec_store]

    def _capture_route(cfg, ticket, app, audit, report, source: str) -> None:
        route_calls.append({"source": source, "report": report})

    loop.decisions.add = _capture_decisions_add
    loop.decisions.load = _capture_decisions_load
    loop._route_out_of_scope = _capture_route


def _make_cfg(dry_run: bool, max_iterations: int = 1) -> Config:
    """Build a Config with PM exhaustion triage disabled and test_gate off."""
    return Config(
        apps=[app_cfg],
        audit_path=str(tmp / "audit.jsonl"),
        dry_run=dry_run,
        max_iterations=max_iterations,
        pm_enabled=False,       # skip PM exhaustion triage → cleaner mock surface
        test_gate=False,        # skip Test Engineer
        use_worktree=False,
    )


def _run(issue: QualityIssue, ftr: FindingsTriage, dry_run: bool,
         review_raw: str = "") -> StubBacklog:
    """Drive one _attempt call with the given FindingsTriage mock, return the backlog stub."""
    _install_capture_hooks()
    _review_issue.clear()
    _review_issue.append(issue)
    _review_raw[0] = review_raw
    triage_args.clear()

    async def _stub_triage_findings(review, tkt, cfg, diff="", files_changed=None):
        # EU-90 (rejection #1): capture the real diff evidence the loop now passes through.
        triage_args["diff"] = diff
        triage_args["files_changed"] = files_changed
        return ftr

    pm_mod.triage_findings = _stub_triage_findings

    bl = StubBacklog()
    audit = StubAudit()
    cfg = _make_cfg(dry_run=dry_run)
    budget = loop.Budget(0)

    asyncio.run(loop._attempt(ticket, app_cfg, cfg, StubGit(), bl, audit, budget, BRANCH))
    return bl


def _run_passes(ftrs: list[FindingsTriage], dry_run: bool) -> StubBacklog:
    """Drive _attempt across len(ftrs) FAIL passes; triage_findings returns ftrs[i] on pass i+1.

    The stub reviewer's constant required_changes trips the retry guard right after the final pass,
    so the EU-90 findings triage runs exactly len(ftrs) times. Used to prove the cross-pass guards:
    a repeated decision pages once, and an unchanged in-scope set comments once."""
    _install_capture_hooks()
    _review_issue.clear()
    _review_issue.append(ISSUE_IN_SCOPE)   # any FAIL quality_issue keeps the triage path alive
    _review_raw[0] = ""
    triage_args.clear()

    seq = {"i": 0}

    async def _stub_triage_findings(review, tkt, cfg, diff="", files_changed=None):
        ftr = ftrs[min(seq["i"], len(ftrs) - 1)]
        seq["i"] += 1
        return ftr

    pm_mod.triage_findings = _stub_triage_findings

    bl = StubBacklog()
    audit = StubAudit()
    cfg = _make_cfg(dry_run=dry_run, max_iterations=len(ftrs))
    budget = loop.Budget(0)

    asyncio.run(loop._attempt(ticket, app_cfg, cfg, StubGit(), bl, audit, budget, BRANCH))
    return bl


# ===========================================================================
# Test 1 — IN-SCOPE finding → Jira add_comment; no out-of-scope routing
# ===========================================================================
ftr_in = FindingsTriage(
    in_scope=[ISSUE_IN_SCOPE],
    out_of_scope=[],
    decisions=[],
)
bl1 = _run(ISSUE_IN_SCOPE, ftr_in, dry_run=False)

chk(
    "in_scope: add_comment is called with the issue detail",
    any(ISSUE_IN_SCOPE.detail in body for _, body in bl1.comments),
    f"comments={[b for _, b in bl1.comments]}",
)
chk(
    "in_scope: _route_out_of_scope is NOT called with source='pm-findings'",
    not any(c["source"] == "pm-findings" for c in route_calls),
    f"route_calls={route_calls}",
)
# rejection #1 — the loop now feeds the classifier REAL diff evidence (not just prose).
chk(
    "rejection #1: the loop passes the real diff into triage_findings",
    "stub diff" in (triage_args.get("diff") or ""),
    f"triage_args={triage_args}",
)
chk(
    "rejection #1: files_changed is threaded through (None here — stub has no BuildArtifact)",
    "files_changed" in triage_args and triage_args["files_changed"] is None,
    f"triage_args={triage_args}",
)
# rejection #2 — the in-scope route is SILENT (zero Commander action).
chk(
    "in_scope: the Commander is NOT paged (silent route)",
    not any("needs YOUR decision" in t for t in notify_calls),
    f"notify_calls={notify_calls}",
)

# ===========================================================================
# Test 2 — OUT-OF-SCOPE finding → _route_out_of_scope(source='pm-findings');
#           no Jira comment posted from the EU-90 route (dry_run=True)
# ===========================================================================
ftr_out = FindingsTriage(
    in_scope=[],
    out_of_scope=[{
        "severity": ISSUE_OUT_OF_SCOPE.severity,
        "area": ISSUE_OUT_OF_SCOPE.area,
        "detail": ISSUE_OUT_OF_SCOPE.detail,
    }],
    decisions=[],
)
bl2 = _run(ISSUE_OUT_OF_SCOPE, ftr_out, dry_run=True)

chk(
    "out_of_scope: _route_out_of_scope is called with source='pm-findings'",
    any(c["source"] == "pm-findings" for c in route_calls),
    f"route_calls={route_calls}",
)
chk(
    "out_of_scope: the routed block contains the issue detail",
    any(ISSUE_OUT_OF_SCOPE.detail in c["report"] for c in route_calls
        if c["source"] == "pm-findings"),
    f"route_calls={route_calls}",
)
chk(
    "out_of_scope: no Jira add_comment posted from the EU-90 route (dry_run=True prevents it)",
    bl2.comments == [],
    f"comments={bl2.comments}",
)
# rejection #2 — the out-of-scope route is SILENT (auto-filed, zero Commander action).
chk(
    "out_of_scope: the Commander is NOT paged (silent route)",
    not any("needs YOUR decision" in t for t in notify_calls),
    f"notify_calls={notify_calls}",
)

# ===========================================================================
# Test 3 — DECISION finding → decisions.add with bulleted brief;
#           no add_comment, no _route_out_of_scope(source='pm-findings')
# ===========================================================================
ftr_dec = FindingsTriage(
    in_scope=[],
    out_of_scope=[],
    decisions=[ISSUE_DECISION],
)
bl3 = _run(ISSUE_DECISION, ftr_dec, dry_run=True)

# The EU-90 decisions.add call uses entry_id="{ticket_id}#pm-findings-decisions"
eu90_dec_calls = [
    c for c in dec_calls
    if c.get("entry_id") == f"{ticket.id}#pm-findings-decisions"
]
chk(
    "decision: decisions.add is called with a bulleted brief (EU-90 entry_id)",
    len(eu90_dec_calls) == 1,
    f"eu90_dec_calls={eu90_dec_calls}",
)
chk(
    "decision: the brief contains a bullet and the issue detail",
    eu90_dec_calls and "•" in eu90_dec_calls[0]["question"]
    and ISSUE_DECISION.detail in eu90_dec_calls[0]["question"],
    f"question={eu90_dec_calls[0]['question'] if eu90_dec_calls else '(none)'}",
)
chk(
    "decision: no Jira add_comment posted (dry_run=True prevents it)",
    bl3.comments == [],
    f"comments={bl3.comments}",
)
chk(
    "decision: _route_out_of_scope is NOT called with source='pm-findings'",
    not any(c["source"] == "pm-findings" for c in route_calls),
    f"route_calls={route_calls}",
)
# rejection #2 — the decision bucket is the ONLY route that pages the Commander, and it pages
# BRIEFLY (a bulleted Needs-you line, not a wall of text).
eu90_decision_pages = [
    t for t in notify_calls
    if "needs YOUR decision" in t and ISSUE_DECISION.detail in t
]
chk(
    "decision: the Commander IS paged with a brief bulleted Needs-you (rejection #2)",
    len(eu90_decision_pages) == 1 and "•" in eu90_decision_pages[0],
    f"notify_calls={notify_calls}",
)


# ===========================================================================
# Test 4 — DEDUP guard (rejection #3): a finding flagged in BOTH the reviewer's own
#           ===TICKETS=== block AND a quality_issue is filed ONCE, not twice.
# ===========================================================================
REVIEWER_BLOCK = (
    "Reviewer prose about the diff.\n"
    "===TICKETS===\n"
    + json.dumps([{
        "title": "Fix SIGTERM handler crash on cockpit Start",
        "type": "Bug", "severity": "HIGH",
        "body": "signal.signal(SIGTERM) called in a non-main thread raises ValueError and "
                "crashes the cockpit Start path; the orphaned PID file pins the badge ON.",
    }])
    + "\n===END==="
)

# --- 4a: direct unit test of the dedup helper ---
_oos_dup = [{
    "severity": "major", "area": "reliability",
    "detail": "signal.signal(SIGTERM) called in a non-main thread raises ValueError, crashing "
              "the cockpit Start path",
}]
_oos_distinct = [{
    "severity": "minor", "area": "style",
    "detail": "unrelated sidebar footer icon padding is two pixels off",
}]
chk(
    "dedup unit: a finding restating the reviewer's own ticket is dropped",
    loop._dedupe_oos_against_reviewer(_oos_dup, REVIEWER_BLOCK) == [],
    f"-> {loop._dedupe_oos_against_reviewer(_oos_dup, REVIEWER_BLOCK)}",
)
chk(
    "dedup unit: an unrelated finding is kept",
    loop._dedupe_oos_against_reviewer(_oos_distinct, REVIEWER_BLOCK) == _oos_distinct,
    f"-> {loop._dedupe_oos_against_reviewer(_oos_distinct, REVIEWER_BLOCK)}",
)
chk(
    "dedup unit: no reviewer ===TICKETS=== block → nothing is deduped",
    loop._dedupe_oos_against_reviewer(_oos_dup, "no block here") == _oos_dup,
    "",
)

# --- 4b: integration — the same finding via review.raw is NOT re-routed as pm-findings ---
ftr_dup = FindingsTriage(
    in_scope=[],
    out_of_scope=[{
        "severity": "major", "area": "reliability",
        "detail": "signal.signal(SIGTERM) in a non-main thread raises ValueError crashing the "
                  "cockpit Start path",
    }],
    decisions=[],
)
bl4 = _run(ISSUE_OUT_OF_SCOPE, ftr_dup, dry_run=True, review_raw=REVIEWER_BLOCK)
chk(
    "dedup integration: a finding already in the reviewer block is NOT re-filed as pm-findings",
    not any(c["source"] == "pm-findings" for c in route_calls),
    f"route_calls={[c['source'] for c in route_calls]}",
)
chk(
    "dedup integration: the reviewer's own block is still routed once (source='reviewer')",
    any(c["source"] == "reviewer" for c in route_calls),
    f"route_calls={[c['source'] for c in route_calls]}",
)


# ===========================================================================
# Test 5 — _findings_prompt carries REAL diff evidence (rejection #1), and
#           PM_FINDINGS_SYSTEM's labels match what the prompt actually provides.
#           (The integration tests stub triage_findings, so the real prompt
#           builder is only exercised here.)
# ===========================================================================
_rv = ReviewResult(
    verdict=Verdict.FAIL, spec_met=False,
    quality_issues=[QualityIssue(severity="major", area="logic", detail="boom")],
    summary="reviewer prose synopsis here",
)
_prompt = pm_mod._findings_prompt(
    _rv, ticket,
    diff="--- a/orchestrator/loop.py\n+++ b/orchestrator/loop.py\n@@ real-diff-hunk @@",
    files_changed=["orchestrator/loop.py", "orchestrator/pm.py"],
)
chk("findings_prompt: lists the changed files (rejection #1)",
    "orchestrator/loop.py" in _prompt and "orchestrator/pm.py" in _prompt,
    f"prompt={_prompt!r}")
chk("findings_prompt: includes the ACTUAL diff excerpt, under a CHANGED FILES + DIFF EXCERPT layout",
    "real-diff-hunk" in _prompt and "CHANGED FILES" in _prompt and "DIFF EXCERPT" in _prompt,
    f"prompt={_prompt!r}")
chk("findings_prompt: drops the stale 'DIFF SUMMARY (reviewer' label",
    "DIFF SUMMARY (reviewer" not in _prompt, f"prompt={_prompt!r}")

# A huge diff must be bounded, not inlined whole.
_big = "x" * (pm_mod._DIFF_EXCERPT_LIMIT + 5000)
_pbig = pm_mod._findings_prompt(_rv, ticket, diff=_big, files_changed=[])
chk("findings_prompt: the diff excerpt is bounded (truncated, not inlined whole)",
    "diff truncated" in _pbig and ("x" * (pm_mod._DIFF_EXCERPT_LIMIT + 1)) not in _pbig,
    "")

# The system prompt's "You are given" labels must match the sections above.
chk("PM_FINDINGS_SYSTEM: documents CHANGED FILES + DIFF EXCERPT (rejection #1)",
    "CHANGED FILES" in pm_mod.PM_FINDINGS_SYSTEM and "DIFF EXCERPT" in pm_mod.PM_FINDINGS_SYSTEM,
    "")
chk("PM_FINDINGS_SYSTEM: no stale 'DIFF SUMMARY' label that misdescribes the input",
    "DIFF SUMMARY" not in pm_mod.PM_FINDINGS_SYSTEM, "")


# ===========================================================================
# Test 6 — cross-pass (iteration-3 rejection #1): a REPEATED decision finding pages
#           the Commander ONCE, not on every failing retry. Two FAIL passes carry the
#           SAME decision → the EU-89 dedup gate collapses the second decisions.add,
#           and the loop pages only when a NEW entry was written.
# ===========================================================================
ftr_dec_same = FindingsTriage(in_scope=[], out_of_scope=[], decisions=[ISSUE_DECISION])
_run_passes([ftr_dec_same, ftr_dec_same], dry_run=True)
decision_pages = [t for t in notify_calls if "needs YOUR decision" in t]
chk(
    "cross-pass decision: the Commander is paged exactly ONCE across two identical passes",
    len(decision_pages) == 1,
    f"decision_pages={decision_pages}",
)
chk(
    "cross-pass decision: the one page is still the brief bulleted Needs-you",
    bool(decision_pages) and "•" in decision_pages[0]
    and ISSUE_DECISION.detail in decision_pages[0],
    f"decision_pages={decision_pages}",
)
# And exactly one pm-findings decision row was created (the second add deduped, no second row).
eu90_adds = [c for c in dec_calls if c.get("entry_id") == f"{ticket.id}#pm-findings-decisions"]
chk(
    "cross-pass decision: dedup means a single stored decision entry",
    len([it for it in dec_store if str(it["id"]).endswith("#pm-findings-decisions")]) == 1,
    f"adds={len(eu90_adds)} store={dec_store}",
)

# ===========================================================================
# Test 7 — cross-pass (iteration-3 rejection #2): an UNCHANGED in-scope set posts the
#           "Builder retrying" comment ONCE, not once per failing retry.
# ===========================================================================
ftr_in_same = FindingsTriage(in_scope=[ISSUE_IN_SCOPE], out_of_scope=[], decisions=[])
bl7 = _run_passes([ftr_in_same, ftr_in_same], dry_run=False)
retrying7 = [b for _, b in bl7.comments if "Builder retrying" in b]
chk(
    "cross-pass in_scope: an unchanged finding set posts the retrying comment exactly ONCE",
    len(retrying7) == 1,
    f"retrying_comments={retrying7}",
)

# ===========================================================================
# Test 8 — cross-pass: a CHANGED in-scope set posts a FRESH comment (the guard
#           suppresses only identical repeats, never a genuinely new finding set).
# ===========================================================================
ftr_in_a = FindingsTriage(in_scope=[ISSUE_IN_SCOPE], out_of_scope=[], decisions=[])
ftr_in_b = FindingsTriage(in_scope=[ISSUE_IN_SCOPE_2], out_of_scope=[], decisions=[])
bl8 = _run_passes([ftr_in_a, ftr_in_b], dry_run=False)
retrying8 = [b for _, b in bl8.comments if "Builder retrying" in b]
chk(
    "cross-pass in_scope: a changed finding set posts a fresh comment (twice over two passes)",
    len(retrying8) == 2,
    f"retrying_comments={retrying8}",
)
chk(
    "cross-pass in_scope: the second comment names the NEW finding",
    any(ISSUE_IN_SCOPE_2.detail in b for b in retrying8),
    f"retrying_comments={retrying8}",
)


# ===========================================================================
# Results
# ===========================================================================
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if not ok and detail else ""))
print(f"{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
