"""EU-92: PM owns Needs-you triage — auto-resolve coverage.

Five cases, all using stubs (no live LLM or Jira calls):

  (a) out-of-scope findings routed through PM auto-resolve → filing.file_findings()
      IS called; decisions.add() is NOT called (the old 'propose-first' path is gone).

  (b) 'noise' out-of-scope finding (report has no ===TICKETS=== block — PM judged skip)
      → neither file_findings nor decisions.add called.

  (c) iteration-exhaustion → PM returns RESOLVE → no Commander escalation
      (decisions.add must NOT be called on the RESOLVE path).

  (d) genuine Commander-only escalation (e.g. 'rotate secret') → decisions.add IS
      called AND the entry body carries the WHY PM CANNOT RESOLVE line.

  (e) ESCALATE verdict missing the mandatory WHY line → the escalation is PRESERVED
      (never silently auto-decided/resolved — that would bury the critical calls this
      gate exists to surface) and a warning is logged flagging it as possible noise.

Import / mock pattern matches eu90_findings_triage_test.py and eu42_route_out_of_scope_test.py.
"""
import asyncio
import logging
import sys
import tempfile
import types
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
from orchestrator import filing
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import (
    BuildResult, GateResult, Outcome, QualityIssue, ReviewResult, TestEngineerResult,
    Ticket, Verdict,
)

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
    id="EU-92",
    key="EU-92",
    summary="PM auto-resolve test",
    description="test fixture",
    ephemeral=False,
    app="Elite-Unit",
)

BRANCH = "autodev/EU-92-test"

# A standard out-of-scope report block for direct _route_out_of_scope tests.
OOS_REPORT = (
    "Reviewer prose.\n"
    "===TICKETS===\n"
    '[{"title": "Fix stale config key in parser", "type": "Task", "severity": "LOW",'
    ' "body": "remove dead config flag from orchestrator/config.py"}]\n'
    "===END===\n"
)

# A noise report: no ===TICKETS=== block at all.
NOISE_REPORT = "Reviewer prose with no off-spec findings detected — nothing to file."


# ---------------------------------------------------------------------------
# Shared stubs for _attempt-level tests (c) and (d)
# ---------------------------------------------------------------------------

class StubGit:
    """Git stub; has_changes() controlled per test via the class attribute."""
    _has_changes = True

    def has_changes(self): return self.__class__._has_changes
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


# ---------------------------------------------------------------------------
# Loop-level stubs — wired once, updated per test
# ---------------------------------------------------------------------------
notify_calls: list[str] = []
dec_calls: list[dict] = []
dec_store: list[dict] = []

def _install_loop_stubs():
    """Reset shared state and wire capturing stubs into the loop module."""
    notify_calls.clear()
    dec_calls.clear()
    dec_store.clear()

    def _capture_decisions_add(cfg, ticket, app_name, question, entry_id=None, **kw):
        dec_calls.append({"question": question, "entry_id": entry_id})
        eid = entry_id or ticket.id
        base = str(ticket.id).split("#", 1)[0]
        fp = " ".join(question.lower().split())
        for it in dec_store:
            if str(it["id"]).split("#", 1)[0] == base and it["fp"] == fp:
                return it["id"]
        dec_store.append({"id": eid, "fp": fp})
        return eid

    def _capture_decisions_load(cfg):
        return [{"id": it["id"]} for it in dec_store]

    loop.decisions.add = _capture_decisions_add
    loop.decisions.load = _capture_decisions_load
    loop._notify = lambda cfg, text: notify_calls.append(text)
    loop.run_gate = lambda app, paths=None, **_: GateResult(passed=True, report="")

    async def _stub_decision_brief(cfg, ticket_id, raw):
        return "(brief)"

    loop._decision_brief = _stub_decision_brief
    loop.decisions.reply_hint = lambda ticket_id: ""


def _make_cfg(dry_run: bool = True, max_iterations: int = 1,
              pm_enabled: bool = False) -> Config:
    return Config(
        apps=[app_cfg],
        audit_path=str(tmp / "audit.jsonl"),
        dry_run=dry_run,
        max_iterations=max_iterations,
        pm_enabled=pm_enabled,
        test_gate=False,
        use_worktree=False,
    )


# ===========================================================================
# Tests (a) and (b) — direct unit tests of _route_out_of_scope
# (pattern from eu42_route_out_of_scope_test.py)
# ===========================================================================

# Capture calls to filing.file_findings and decisions.add (reset per test).
file_findings_calls: list[str] = []   # report text passed to file_findings
_direct_dec_calls: list = []           # (ticket_id, question, entry_id)


class _FakeFilingResult:
    """Minimal stand-in for filing.FileFindings result."""
    filed = ["EU-999"]
    deduped = []
    failed = []
    lines = ["created EU-999: Fix stale config key in parser"]


def _reset_direct_stubs():
    file_findings_calls.clear()
    _direct_dec_calls.clear()


def _run_route(report: str, autofile: bool = False, dry_run: bool = False):
    """Drive _route_out_of_scope once with capture stubs installed."""
    _reset_direct_stubs()

    # Stub filing.file_findings to capture the call without touching the backlog.
    _orig_ff = filing.file_findings
    def _cap_file_findings(app, label, report_text):
        file_findings_calls.append(report_text)
        return _FakeFilingResult()
    filing.file_findings = _cap_file_findings

    # Stub decisions.add on the loop module (not the real store).
    _orig_add = loop.decisions.add
    def _cap_add(cfg, ticket, app_name, question, entry_id=None, **kw):
        _direct_dec_calls.append((ticket.id, question, entry_id))
    loop.decisions.add = _cap_add

    notify_calls.clear()
    loop._notify = lambda cfg, text: notify_calls.append(text)

    cfg = _make_cfg(dry_run=dry_run)
    cfg.out_of_scope_autofile = autofile
    audit = StubAudit()
    try:
        loop._route_out_of_scope(cfg, ticket, app_cfg, audit, report, source="pm-findings")
    finally:
        filing.file_findings = _orig_ff
        loop.decisions.add = _orig_add


# ---------------------------------------------------------------------------
# Test (a): valid OOS findings + out_of_scope_autofile=False (EU-92 default path)
#           → file_findings called, decisions.add NOT called
# ---------------------------------------------------------------------------
_run_route(OOS_REPORT, autofile=False, dry_run=False)

chk(
    "(a) PM auto-resolve: filing.file_findings IS called for worthwhile finding",
    len(file_findings_calls) == 1,
    f"file_findings_calls={file_findings_calls}",
)
chk(
    "(a) PM auto-resolve: decisions.add is NOT called (no Commander approval needed)",
    len(_direct_dec_calls) == 0,
    f"dec_calls={_direct_dec_calls}",
)
chk(
    "(a) PM auto-resolve: no Commander page emitted (out-of-scope is a silent route)",
    not any("needs YOUR decision" in t or "needs you" in t.lower() for t in notify_calls),
    f"notify_calls={notify_calls}",
)

# ---------------------------------------------------------------------------
# Test (b): noise report (no ===TICKETS=== block) → neither file_findings nor decisions.add
# ---------------------------------------------------------------------------
_run_route(NOISE_REPORT, autofile=False, dry_run=False)

chk(
    "(b) noise: filing.file_findings is NOT called when no ===TICKETS=== block",
    len(file_findings_calls) == 0,
    f"file_findings_calls={file_findings_calls}",
)
chk(
    "(b) noise: decisions.add is NOT called",
    len(_direct_dec_calls) == 0,
    f"dec_calls={_direct_dec_calls}",
)


# ===========================================================================
# Test (c): iteration-exhaustion → PM returns RESOLVE → no Commander escalation
# (decisions.add must NOT be called on the RESOLVE path)
# ===========================================================================

class StubBuilderFail:
    """Builder that always returns ok=True (so the loop can check the review)."""
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "stub")

    @staticmethod
    async def build(req, app, cfg, audit=None, **_):
        return BuildResult(
            ok=True, summary="stub: changed one file",
            cost_usd=0.0, num_turns=1, raw="stub: changed one file", tools=[],
        )


async def _stub_review_fail(diff, ticket, app, cfg, iteration, store=None, build_artifact=None):
    """Reviewer always returns FAIL so the loop exhausts its iterations."""
    return ReviewResult(
        verdict=Verdict.FAIL, spec_met=False,
        quality_issues=[QualityIssue(severity="minor", area="style", detail="stub fail")],
        required_changes=["fix the style issue"],
        summary="stub reviewer fail",
        needs_human=False,
        raw="",
    )


async def _stub_triage_resolve(cfg, app_name, ticket_id, **_):
    """PM triage returns RESOLVE — routine, no Commander needed."""
    return {"action": "RESOLVE", "text": "Re-run the builder with a narrower scope.", "raw": ""}


def _run_exhaustion_resolve() -> StubAudit:
    _install_loop_stubs()
    loop.builder_mod = StubBuilderFail
    loop.reviewer_mod.review = _stub_review_fail

    async def _stub_triage_findings(review, tkt, cfg, diff="", files_changed=None):
        from orchestrator.pm import FindingsTriage
        return FindingsTriage(in_scope=[
            QualityIssue(severity="minor", area="style", detail="stub fail")
        ], out_of_scope=[], decisions=[])

    pm_mod.triage_findings = _stub_triage_findings
    pm_mod.triage = _stub_triage_resolve

    bl = StubBacklog()
    audit = StubAudit()
    cfg = _make_cfg(dry_run=True, max_iterations=1, pm_enabled=True)
    budget = loop.Budget(0)

    asyncio.run(loop._attempt(ticket, app_cfg, cfg, StubGit(), bl, audit, budget, BRANCH))
    return audit


# Commander decision (Phase-2, 2026-07-06): the PM RESOLVE requeue is RETIRED — it granted one
# extra capped attempt, re-opening the QW3 2-pass loop cap. A RESOLVE verdict now escalates like
# everything else, with the PM's corrective instruction carried as the Commander's brief.
_audit_c = _run_exhaustion_resolve()

chk(
    "(c) RESOLVE retired: the ticket escalates (decisions.add IS called)",
    len(dec_calls) == 1,
    f"dec_calls={dec_calls}",
)
chk(
    "(c) RESOLVE retired: the escalation brief carries the PM's corrective instruction",
    dec_calls and "narrower scope" in dec_calls[0]["question"],
    f"dec_calls={dec_calls}",
)
chk(
    "(c) RESOLVE retired: pm_resolve_retired audited, and NO requeue event recorded",
    any(e["event"] == "pm_resolve_retired" for e in _audit_c.events)
    and not any(e["event"] == Outcome.REQUEUED.audit_event for e in _audit_c.events),
    str([e["event"] for e in _audit_c.events]),
)


# ===========================================================================
# Test (d): genuine Commander-only escalation (e.g. 'rotate secret')
#           → decisions.add IS called AND entry body contains the WHY line
# ===========================================================================

# Patch _consult_pm so the PM returns ESCALATE + WHY when the builder halts with no changes.
_PM_WHY = "only the Commander holds the rotation credential for the production secret store"
_PM_BODY = "The builder cannot proceed without rotating the API key."


async def _stub_consult_pm_escalate(cfg, ticket, app, audit, halt_report):
    """Simulates PM returning ESCALATE + WHY for a critical blocker."""
    return {
        "verdict": "ESCALATE",
        "body": _PM_BODY,
        "why": _PM_WHY,
        "raw": f"WHY PM CANNOT RESOLVE: {_PM_WHY}\n{_PM_BODY}\nPM VERDICT: ESCALATE",
    }


class StubBuilderHalt:
    """Builder that returns ok=True but writes no changes (deliberate halt)."""
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "stub")

    @staticmethod
    async def build(req, app, cfg, audit=None, **_):
        return BuildResult(
            ok=True,
            # Two halt markers so _is_deliberate_halt returns True.
            summary="Cannot proceed — needs your approval to rotate the secret key.",
            cost_usd=0.0, num_turns=1, raw="", tools=[],
        )


class StubGitNoChanges:
    """Git stub that reports no changes — triggers the deliberate-halt path."""
    def has_changes(self): return False
    def diff_against_base(self): return ""
    def changed_paths(self): return []
    def branch(self): return BRANCH


def _run_commander_escalation() -> StubBacklog:
    _install_loop_stubs()
    loop.builder_mod = StubBuilderHalt
    # Reviewer should not be reached (no changes), but stub defensively.
    loop.reviewer_mod.review = _stub_review_fail
    loop._consult_pm = _stub_consult_pm_escalate

    async def _stub_triage_findings(review, tkt, cfg, diff="", files_changed=None):
        from orchestrator.pm import FindingsTriage
        return FindingsTriage(in_scope=[], out_of_scope=[], decisions=[])

    pm_mod.triage_findings = _stub_triage_findings

    bl = StubBacklog()
    audit = StubAudit()
    cfg = _make_cfg(dry_run=True, max_iterations=1, pm_enabled=False)
    budget = loop.Budget(0)

    asyncio.run(loop._attempt(ticket, app_cfg, cfg, StubGitNoChanges(), bl, audit, budget, BRANCH))
    return bl


_run_commander_escalation()

# The loop should have called decisions.add with the why-line prepended.
chk(
    "(d) Commander escalation: decisions.add IS called",
    len(dec_calls) >= 1,
    f"dec_calls={dec_calls}",
)
why_prefix = "WHY PM CANNOT RESOLVE:"
chk(
    "(d) Commander escalation: the decisions.add body contains the WHY PM CANNOT RESOLVE line",
    any(why_prefix in c["question"] for c in dec_calls),
    f"dec_calls_questions={[c['question'][:120] for c in dec_calls]}",
)
chk(
    "(d) Commander escalation: the WHY body names the specific blocker (rotation credential)",
    any(_PM_WHY[:30] in c["question"] for c in dec_calls),
    f"dec_calls_questions={[c['question'][:120] for c in dec_calls]}",
)


# ===========================================================================
# Test (e): ESCALATE verdict missing the mandatory WHY line
#           → the escalation is PRESERVED (never silently auto-decided/resolved);
#           a warning is emitted via the logging system flagging it as noise.
#           (Coercing a genuine can't-decide away is the failure mode EU-92 avoids;
#            the WHY line is enforced in the PM prompt, not by parse-time coercion.)
# ===========================================================================

# --- (e1) parse_verdict: explicit ESCALATE without WHY → preserved as ESCALATE (+ warning) ---
with_why = (
    "WHY PM CANNOT RESOLVE: only the Commander has the billing contract access.\n"
    "BLOCKER: billing integration cannot proceed.\n"
    "DECISION: approve or defer the integration?\n"
    "PM VERDICT: ESCALATE"
)
without_why = "The PM has concerns.\nPM VERDICT: ESCALATE"
decide_text = "PM VERDICT: DECIDE\n• Decision: proceed with the default approach."

pv_with_why = pm_mod.parse_verdict(with_why)
pv_without_why = pm_mod.parse_verdict(without_why)
pv_decide = pm_mod.parse_verdict(decide_text)

chk(
    "(e1) parse_verdict: ESCALATE with WHY → verdict is ESCALATE",
    pv_with_why["verdict"] == "ESCALATE",
    f"verdict={pv_with_why['verdict']}",
)
chk(
    "(e1) parse_verdict: ESCALATE with WHY → why field is populated",
    bool(pv_with_why.get("why")),
    f"why={pv_with_why.get('why')!r}",
)
chk(
    "(e1) parse_verdict: ESCALATE missing WHY → still ESCALATE (never silently auto-decided)",
    pv_without_why["verdict"] == "ESCALATE",
    f"verdict={pv_without_why['verdict']}",
)
chk(
    "(e1) parse_verdict: ESCALATE missing WHY → why field is empty/absent",
    not pv_without_why.get("why"),
    f"why={pv_without_why.get('why')!r}",
)
chk(
    "(e1) parse_verdict: clean DECIDE → verdict is DECIDE",
    pv_decide["verdict"] == "DECIDE",
    f"verdict={pv_decide['verdict']}",
)

# --- (e2) parse_triage: explicit TRIAGE: ESCALATE without WHY → preserved as ESCALATE (+ warning) ---
triage_with_why = (
    "WHY PM CANNOT RESOLVE: the Commander must approve the billing scope change.\n"
    "BLOCKER: cannot ship billing screen without scope sign-off.\n"
    "DECISION: approve or defer billing?\n"
    "TRIAGE: ESCALATE"
)
triage_without_why = "The PM cannot resolve this.\nTRIAGE: ESCALATE"
triage_resolve_text = "• Decision: re-run the builder with a narrower scope.\nTRIAGE: RESOLVE"

pt_with_why = pm_mod.parse_triage(triage_with_why)
pt_without_why = pm_mod.parse_triage(triage_without_why)
pt_resolve = pm_mod.parse_triage(triage_resolve_text)

chk(
    "(e2) parse_triage: TRIAGE: ESCALATE with WHY → action is ESCALATE",
    pt_with_why["action"] == "ESCALATE",
    f"action={pt_with_why['action']}",
)
chk(
    "(e2) parse_triage: TRIAGE: ESCALATE with WHY → why field is populated",
    bool(pt_with_why.get("why")),
    f"why={pt_with_why.get('why')!r}",
)
chk(
    "(e2) parse_triage: TRIAGE: ESCALATE missing WHY → still ESCALATE (never silently auto-resolved)",
    pt_without_why["action"] == "ESCALATE",
    f"action={pt_without_why['action']}",
)
chk(
    "(e2) parse_triage: clean TRIAGE: RESOLVE → action is RESOLVE",
    pt_resolve["action"] == "RESOLVE",
    f"action={pt_resolve['action']}",
)

# --- (e3) warning is logged: verify via logging capture ---
import logging as _logging

_warning_log: list[str] = []


class _WarningCapture(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.WARNING:
            _warning_log.append(record.getMessage())


_log = logging.getLogger("orchestrator.pm")
_handler = _WarningCapture()
_log.addHandler(_handler)
_log.setLevel(logging.WARNING)

try:
    _warning_log.clear()
    pm_mod.parse_verdict("PM VERDICT: ESCALATE")
    chk(
        "(e3) parse_verdict: missing WHY emits a WARNING log",
        any("WHY PM CANNOT RESOLVE" in w or "coercing" in w.lower() for w in _warning_log),
        f"warnings={_warning_log}",
    )

    _warning_log.clear()
    pm_mod.parse_triage("TRIAGE: ESCALATE")
    chk(
        "(e3) parse_triage: missing WHY emits a WARNING log",
        any("WHY PM CANNOT RESOLVE" in w or "coercing" in w.lower() for w in _warning_log),
        f"warnings={_warning_log}",
    )
finally:
    _log.removeHandler(_handler)


# ===========================================================================
# Results
# ===========================================================================
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if not ok and detail else ""))
print(f"{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
