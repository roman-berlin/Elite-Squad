"""EU-737: Clarity gate — guard tests for all acceptance criteria.

Failing checks go RED, exit 1. Harness uses soft-tally k/n pattern so run_all reads counts.
This harness MUST exit non-zero if any assert fires or soft-check fails.

Coverage:
  AC6-case 1  -- thin-but-inferable -> rewritten + built
  AC6-case 2  -- genuinely ambiguous -> Blocked + question + NO build event
  AC6-case 3  -- clear ticket -> untouched (no rewrite, no park)
  AC6-case 4  -- clarity-check exception -> proceeds to build (fail-open)
  AC5         -- fire-once: prior parked/clarified suppresses re-ask/re-write
  AC2         -- rewrite keeps ONLY the Commander's words (injected loop context stripped)
  parse_plan  -- fail-safe defaults, unclear/clarified/clear paths
"""
import asyncio
import os
import sys
import json
import tempfile
from pathlib import Path

# ===========================================================================
# STUBS -- prevent SDK/model imports during unit tests
# ===========================================================================
sdk_mod = type(sys)("claude_agent_sdk")


class _StubAttr:
    def __init__(self, *a, **k): pass

    def __call__(self, *a, **k): return self

    def __getattr__(self, name): return self

sdk_mod.__getattr__ = lambda name: _StubAttr
sys.modules["claude_agent_sdk"] = sdk_mod
sys.modules["openai"] = type(sys)("openai")

os.environ["GENERAL_COCKPIT_PROMOTE"] = "1"

test_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(test_root))

from orchestrator.config import Config
from orchestrator.planner import PlannerResult, parse_plan, PLANNER_SYSTEM

results = []   # list of (label: str, passed: bool, detail_if_failed: str)


def chk(label, condition, detail=""):
    """Record one check result."""
    results.append((label, bool(condition), detail))


# ===========================================================================
# TEST GROUP 1: parse_plan -- fail-safe defaults and clarity fields
# ===========================================================================
print("\n========== PARSE_PLAN (parse_plan fail-safe and clarity fields) ===========\n")

# Default: garbled input -> BUILD with clarity="clear" (fail-safe default)
r = parse_plan("garbage {{{ unparseable")
chk("garbled -> verdict=BUILD", r.verdict == "BUILD", f"got {r.verdict}")
chk("garbled -> clarity='clear'", r.clarity == "clear", f"got {r.clarity!r}")
chk("garbled -> clarified_description empty", r.clarified_description == "")
chk("garbled -> clarification_request empty", r.clarification_request == "")
chk("garbled -> has raw text", len(r.raw) > 0)

# Explicitly clear
r = parse_plan(json.dumps({
    "verdict": "BUILD", "approach": "add a function",
    "testable_ac": ["it works"], "in_scope_files": ["x.py"],
    "clarity": "clear", "clarified_description": "", "clarification_request": ""
}))
chk("explicit clear -> verdict=BUILD", r.verdict == "BUILD")
chk("explicit clear -> clarity='clear'", r.clarity == "clear")
chk("explicit clear -> approach preserved", r.approach == "add a function")

# Clarified path: planner rewrites spec
r = parse_plan(json.dumps({
    "verdict": "BUILD", "approach": "build the feature",
    "testable_ac": ["AC1", "AC2"], "in_scope_files": ["mod.py"],
    "clarity": "clarified",
    "clarified_description": "Build FooService with create/read endpoints in src/foo_service/",
    "clarification_request": "",
}))
chk("clarified -> clarity='clarified'", r.clarity == "clarified", f"got {r.clarity!r}")
chk("clarified -> description populated",
    "FooService" in r.clarified_description,
    f"got {r.clarified_description[:80]}")
chk("clarified -> still BUILD", r.verdict == "BUILD")

# Unclear path: planner needs help
r = parse_plan(json.dumps({
    "verdict": "BUILD", "approach": "do something",
    "testable_ac": [], "in_scope_files": [],
    "clarity": "unclear",
    "clarification_request": "What should this ticket build? Options:\nA) Auth module (recommended)\nB) API gateway\n",
    "answer": "",
}))
chk("unclear -> clarity='unclear'", r.clarity == "unclear", f"got {r.clarity!r}")
chk("unclear -> request populated",
    "auth" in r.clarification_request.lower(),
    f"got {r.clarification_request[:80]}")

# to_dict includes all new fields
d = r.to_dict()
chk("to_dict has 'clarity' key", "clarity" in d)
chk("to_dict has 'clarified_description' key", "clarified_description" in d)
chk("to_dict has 'clarification_request' key", "clarification_request" in d)
chk("to_dict carries clarity='unclear'", d.get("clarity") == "unclear")

# Empty string -> BUILD default
r = parse_plan("")
chk("empty string -> BUILD", r.verdict == "BUILD")
chk("empty string -> clarity='clear'", r.clarity == "clear")

# Non-BUILD verdicts get clarity="clear" (field absent -> default)
r = parse_plan('{"verdict": "CLOSE", "answer": "duplicate"}')
chk("CLOSE -> clarity='clear'", r.clarity == "clear")

# Missing/invalid verdict falls through but clarity defaults
r = parse_plan('{"approach": "xyz"}')
chk("missing verdict field -> clarity='clear'", r.clarity == "clear")

# ===========================================================================
# TEST GROUP 2: config knob
# ===========================================================================
print("\n========== CONFIG KNOB ===========\n")

_tmpdir = Path(tempfile.mkdtemp())
cfg = Config(apps=[], audit_path=str(_tmpdir / "audit.jsonl"))
chk("config has planner_clarity_gate attr", hasattr(cfg, "planner_clarity_gate"))
chk("default value is True", cfg.planner_clarity_gate is True)

# ===========================================================================
# TEST GROUP 3: backlog.base update_description exists as no-op
# ===========================================================================
print("\n========== BACKLOG ADAPTER ===========\n")

from orchestrator.backlog.base import BacklogAdapter, NoneBacklog

# Abstract method must exist
assert hasattr(BacklogAdapter, "update_description")
chk("BacklogAdapter declares update_description",
    "update_description" in BacklogAdapter.__dict__)

nb = NoneBacklog()
try:
    nb.update_description(None, "new body")
    chk("NoneBacklog.update_description returns without raising", True)
except Exception as exc:
    chk("NoneBacklog.update_description returns without raising", False, str(exc))

# JiraAdapter must also have it (method-level, no actual network)
from orchestrator.backlog.jira import JiraAdapter
assert hasattr(JiraAdapter, "update_description")
chk("JiraAdapter implements update_description",
    "update_description" in JiraAdapter.__dict__)

# ===========================================================================
# TEST GROUP 4: fire-once helpers via audit log scan
# ===========================================================================
print("\n========== FIRE-ONCE HELPERS ===========\n")

audit_dir = Path(tempfile.mkdtemp(prefix="eu737-audit-"))
audit_file = audit_dir / "audit.jsonl"

cfg_limited = Config(apps=[], audit_path=str(audit_file))

# Import AFTER stubs are set up
from orchestrator.loop import (_clarity_parked_before, _clarity_clarified_before,
                               _strip_injected_context)


def _write_event(path, event, ticket_id="TICK-1"):
    """Append one JSON line to the audit log."""
    with open(path, "a") as fh:
        fh.write(json.dumps({"event": event, "ticket_id": ticket_id}) + "\n")


def _clear_events(path):
    """Remove the audit file so there are zero events."""
    if path.exists():
        path.unlink()


# Case: no prior events -> both False
_clear_events(audit_file)
chk("no prior events -> parked_before=False",
    _clarity_parked_before(cfg_limited, "TICK-1") is False)
chk("no prior events -> clarified_before=False",
    _clarity_clarified_before(cfg_limited, "TICK-1") is False)

# Case: prior parked event -> parked_before=True
_write_event(str(audit_file), "ticket_unclear_parked", "TICK-1")
chk("prior unclear_parked -> parked_before=True",
    _clarity_parked_before(cfg_limited, "TICK-1") is True)
chk("prior unclear_parked -> clarified_before still False",
    _clarity_clarified_before(cfg_limited, "TICK-1") is False)

# Case: prior clarified event -> clarified_before=True
_write_event(str(audit_file), "ticket_clarified", "TICK-1")
chk("prior clarified -> clarified_before=True",
    _clarity_clarified_before(cfg_limited, "TICK-1") is True)

# Different ticket ID does not match previous events
_clear_events(audit_file)
_write_event(str(audit_file), "ticket_unclear_parked", "OTHER-TICK")
chk("different ticket ID ignored by parked_before",
    _clarity_parked_before(cfg_limited, "TICK-1") is False)

# Corrupted audit lines don't crash -> fail open (returns False)
with open(str(audit_file), "w") as fh:
    fh.write("this is not json at all\n")
    fh.write("{broken}\n")
chk("corrupt audit lines -> fail open (False)",
    _clarity_parked_before(cfg_limited, "TICK-1") is False)

# Event from another ticket doesn't trigger
_clear_events(audit_file)
_write_event(str(audit_file), "ticket_unclear_parked", "ANOTHER")
chk("another ticket's event doesn't block current ticket",
    _clarity_parked_before(cfg_limited, "TICK-1") is False)

# ===========================================================================
# TEST GROUP 5: PLANNER_SYSTEM instruction completeness
# ===========================================================================
print("\n========== PLANNER SYSTEM INSTRUCTION ===========\n")

chk("PLANNER_SYSTEM mentions EU-737", "EU-737" in PLANNER_SYSTEM)
chk("PLANNER_SYSTEM mentions clarity gate", "clarity" in PLANNER_SYSTEM.lower())
chk("PLANNER_SYSTEM describes 'clear'", '"clear"' in PLANNER_SYSTEM)
chk("PLANNER_SYSTEM describes 'clarified'", '"clarified"' in PLANNER_SYSTEM)
chk("PLANNER_SYSTEM describes 'unclear'", '"unclear"' in PLANNER_SYSTEM)
chk("PLANNER_SYSTEM says Original request heading",
    "## Original request" in PLANNER_SYSTEM)
chk("PLANNER_SYSTEM mentions decision-card format",
    "decision-card" in PLANNER_SYSTEM.lower() or "recommendation" in PLANNER_SYSTEM.lower())
chk("PLANNER_SYSTEM warns about overwriting Commander words",
    "Commander" in PLANNER_SYSTEM and "overwrite" in PLANNER_SYSTEM.lower())

# ===========================================================================
# TEST GROUP 6: _strip_injected_context — the rewrite keeps ONLY the Commander's words
# ===========================================================================
print("\n========== STRIP INJECTED CONTEXT ===========\n")

# Plain description passes through unchanged (modulo strip).
chk("plain description passes through unchanged",
    _strip_injected_context("Fix the login bug.") == "Fix the login bug.")

# Empty string stays empty.
chk("empty description stays empty", _strip_injected_context("") == "")

# The EU-395 prior-attempts digest is cut off before the body goes back to the board.
_desc = ("Add a retry button.\n\n---\nPrior attempts on this ticket (from the audit log — "
         "what was tried and why it failed; do not repeat a dead approach):\n- attempt 1 errored")
chk("prior-attempts injection stripped from original",
    _strip_injected_context(_desc) == "Add a retry button.",
    repr(_strip_injected_context(_desc)))

# The EU-61 resolved-decision block is cut off too.
_desc = ("Add a retry button.\n\n---\nResolved decision (the Commander answered on the ticket — "
         "act on it, do not re-raise it):\nQ: which icon?\nA: the round one")
chk("resolved-decision injection stripped from original",
    _strip_injected_context(_desc) == "Add a retry button.",
    repr(_strip_injected_context(_desc)))

# The PM decide-first block is cut off too.
_desc = ("Add a retry button.\n\n---\nPRODUCT MANAGER DECISION (resolves the open product "
         "question — act on it, do not re-raise it):\nUse the round icon.")
chk("PM-decision injection stripped from original",
    _strip_injected_context(_desc) == "Add a retry button.",
    repr(_strip_injected_context(_desc)))

# Multiple injections -> cut at the EARLIEST marker; commander text before it survives whole.
_desc = ("Do X.\nThen Y.\n\n---\nPrior attempts on this ticket (from the audit log — x):\n- a\n"
         "\n\n---\nResolved decision (the Commander answered on the ticket — y):\nQ: q\nA: a")
chk("earliest marker wins when several injections exist",
    _strip_injected_context(_desc) == "Do X.\nThen Y.",
    repr(_strip_injected_context(_desc)))

# A commander description that merely MENTIONS the words without the marker survives whole.
_desc = "There were prior attempts on this ticket but none landed."
chk("prose mention without the marker is not stripped",
    _strip_injected_context(_desc) == _desc)

# ===========================================================================
# TEST GROUP 7: AC6 end-to-end — drive loop._attempt with fakes (the eu217 pattern:
# stub the SDK above, no git/network/LLM calls). One run per clarity verdict:
#   thin-but-inferable -> rewritten + built
#   genuinely ambiguous -> parked with a question, NO build event
#   clear               -> untouched (no rewrite, no park)
#   gate exception      -> builds anyway (fail-open)
# ===========================================================================
print("\n========== AC6 END-TO-END (loop._attempt) ===========\n")

import orchestrator.loop as loop_mod
import orchestrator.planner as planner_mod
from orchestrator.config import AppConfig
from orchestrator.contracts import BuildResult, Outcome, Ticket

_e2e_dir = Path(tempfile.mkdtemp(prefix="eu737-e2e-"))

_e2e_app = AppConfig(
    name="Elite-Unit",
    repo_path=str(_e2e_dir),
    base_branch="dev",
    protected_branch="main",
    backlog_backend="none",
)

_E2E_BRANCH = "autodev/EU-737-clarity-test"


class _E2EGit:
    def has_changes(self): return True
    def diff_against_base(self): return "--- a\n+++ b\n@@ stub diff @@"
    def changed_paths(self): return []
    def branch(self): return _E2E_BRANCH


class _E2EBacklog:
    def __init__(self):
        self.comments: list[str] = []
        self.description_writes: list[str] = []
        self.statuses: list[str] = []

    def add_comment(self, ticket, body: str) -> None:
        self.comments.append(body)

    def update_description(self, ticket, body: str) -> None:
        self.description_writes.append(body)

    def set_status(self, ticket, status: str) -> None:
        self.statuses.append(status)


class _E2EAudit:
    def __init__(self):
        self.events: list[dict] = []

    def record(self, event, **kw):
        self.events.append({"event": event, **kw})

    @staticmethod
    def diff_hash(*a):
        return "eu737stub"


class _E2EBuilder:
    built = 0

    @staticmethod
    def effort_plan(cfg, iteration, ticket): return ("low", "stub")

    @staticmethod
    def turns_for(cfg, effort): return 99

    @staticmethod
    async def build(req, app, cfg, audit=None, **_):
        _E2EBuilder.built += 1
        return BuildResult(ok=False, summary="stub build failed", raw="stub build failed",
                           cost_usd=0.0, num_turns=1, tools=[])


class _FakeCommenter:
    def summarize_gate_event(self, *a, **k): return None
    def post_comment(self, *a, **k): return True


_e2e_dec_calls: list[dict] = []
_e2e_planner_result: list = []   # single-element box so the stub reads the current value


async def _e2e_stub_plan(cfg, ticket, app=None, audit=None):
    return _e2e_planner_result[0]


def _e2e_run(plan_result, desc="Fix the thing.", mutate_gate=None):
    """One _attempt pass with the planner stubbed to `plan_result`. Returns
    (report, backlog, audit, dec_calls)."""
    _e2e_dec_calls.clear()
    _e2e_planner_result.clear()
    _e2e_planner_result.append(plan_result)
    _E2EBuilder.built = 0

    # Wire capturing stubs (restore of the function-valued ones happens by reassignment each call).
    loop_mod.decisions.add = lambda cfg, ticket, app_name, question, entry_id=None, **kw: (
        _e2e_dec_calls.append({"question": question}) or (entry_id or ticket.id))
    loop_mod.decisions.load = lambda cfg: []
    loop_mod.decisions.reply_hint = lambda ticket_id: ""
    loop_mod._notify = lambda cfg, text: None
    loop_mod.builder_mod = _E2EBuilder
    planner_mod.plan = _e2e_stub_plan
    # restore a pristine fire-once guard (an earlier case may have mutated it)
    loop_mod._clarity_clarified_before = _clarity_clarified_before
    loop_mod._clarity_parked_before = _clarity_parked_before
    if mutate_gate is not None:
        mutate_gate()

    t = Ticket(id="EU-737", key="EU-737", summary="clarity gate e2e",
               description=desc, ephemeral=False, app="Elite-Unit")
    cfg = Config(apps=[_e2e_app], audit_path=str(_e2e_dir / "audit-e2e.jsonl"),
                 dry_run=False, max_iterations=1, pm_enabled=False,
                 planner_enabled=True, planner_clarity_gate=True, use_worktree=False)
    bl = _E2EBacklog()
    aud = _E2EAudit()
    report = asyncio.run(loop_mod._attempt(t, _e2e_app, cfg, _E2EGit(), bl, aud,
                                           loop_mod.Budget(0), _E2E_BRANCH,
                                           commenter=_FakeCommenter()))
    return report, bl, aud, list(_e2e_dec_calls)


# ── AC6 case 2: genuinely ambiguous -> Blocked with a question, NO build ──
_rep, _bl, _aud, _decs = _e2e_run(PlannerResult(
    verdict="BUILD", clarity="unclear",
    clarification_request="What should the retry button do? Options: A) retry the last step "
                          "(recommended) B) retry the whole flow."))
_e2e_events = [e["event"] for e in _aud.events]
chk("AC6 unclear: outcome ESCALATED (parked before building)",
    _rep.outcome == Outcome.ESCALATED, f"got {_rep.outcome}")
chk("AC6 unclear: exactly one decision card raised",
    len(_decs) == 1, str(_decs))
chk("AC6 unclear: the question carries the Planner's decision-card",
    _decs and "retry button" in _decs[0]["question"], str(_decs))
chk("AC6 unclear: ticket_unclear_parked audit event recorded (AC5 measurable)",
    "ticket_unclear_parked" in _e2e_events, str(_e2e_events))
chk("AC6 unclear: NO build event and builder never ran (no tokens burned)",
    "build" not in _e2e_events and _E2EBuilder.built == 0,
    f"events={_e2e_events} built={_E2EBuilder.built}")
chk("AC6 unclear: description NOT rewritten",
    len(_bl.description_writes) == 0, str(_bl.description_writes))

# ── AC6 case 1: thin-but-inferable -> rewritten + built ──
_orig = "Add a retry button."
_injected = (_orig + "\n\n---\nPrior attempts on this ticket (from the audit log — what was "
             "tried and why it failed; do not repeat a dead approach):\n- attempt 1 errored")
_rep, _bl, _aud, _decs = _e2e_run(PlannerResult(
    verdict="BUILD", approach="add retry", testable_ac=["button retries"],
    in_scope_files=["x.py"], clarity="clarified",
    clarified_description="Build a retry button in the toolbar that re-runs the last step. "
                          "AC: click retries once; disabled while running."),
    desc=_injected)
_e2e_events = [e["event"] for e in _aud.events]
chk("AC6 clarified: description rewritten exactly once",
    len(_bl.description_writes) == 1, str(len(_bl.description_writes)))
_body = _bl.description_writes[0] if _bl.description_writes else ""
chk("AC6 clarified: new body leads with the buildable spec",
    _body.startswith("Build a retry button in the toolbar"), _body[:80])
chk("AC6 clarified: original words preserved under '## Original request'",
    "\n\n---\n## Original request\n" in _body and "Add a retry button." in _body,
    _body[-200:])
chk("AC6 clarified: the loop's own injected context is NOT written back to the board",
    "Prior attempts on this ticket" not in _body, _body[-300:])
chk("AC6 clarified: '[Squad] Clarified the ticket' comment posted (prefix added by add_comment)",
    any(c.startswith("Clarified the ticket") for c in _bl.comments), str(_bl.comments))
chk("AC6 clarified: ticket_clarified audit event recorded (AC5 measurable)",
    "ticket_clarified" in _e2e_events, str(_e2e_events))
chk("AC6 clarified: the build WAS attempted after the rewrite",
    _E2EBuilder.built == 1 and "build" in _e2e_events,
    f"built={_E2EBuilder.built} events={_e2e_events}")
chk("AC6 clarified: no park raised for a clarified ticket",
    len(_decs) == 0, str(_decs))

# ── AC6 case 3: clear ticket -> untouched ──
_rep, _bl, _aud, _decs = _e2e_run(PlannerResult(
    verdict="BUILD", approach="fix it", testable_ac=["works"],
    in_scope_files=["y.py"], clarity="clear"))
_e2e_events = [e["event"] for e in _aud.events]
chk("AC6 clear: description NOT rewritten, no comment, no park",
    len(_bl.description_writes) == 0 and len(_bl.comments) == 0 and len(_decs) == 0,
    f"writes={_bl.description_writes} comments={_bl.comments} decs={_decs}")
chk("AC6 clear: neither clarity audit event recorded",
    "ticket_clarified" not in _e2e_events and "ticket_unclear_parked" not in _e2e_events,
    str(_e2e_events))
chk("AC6 clear: the build proceeds as usual",
    _E2EBuilder.built == 1 and "build" in _e2e_events,
    f"built={_E2EBuilder.built} events={_e2e_events}")

# ── AC6 case 4: clarity-check exception -> builds anyway (fail-open) ──
def _break_guard():
    def _boom(cfg, ticket_id):
        raise RuntimeError("audit exploded")
    loop_mod._clarity_clarified_before = _boom


_rep, _bl, _aud, _decs = _e2e_run(PlannerResult(
    verdict="BUILD", clarity="unclear",
    clarification_request="would have parked if the guard hadn't exploded"),
    mutate_gate=_break_guard)
_e2e_events = [e["event"] for e in _aud.events]
chk("AC6 exception: fail-open — the build is attempted despite the gate error",
    _E2EBuilder.built == 1 and "build" in _e2e_events,
    f"built={_E2EBuilder.built} events={_e2e_events}")
chk("AC6 exception: no park, no clarity audit events after a gate failure",
    len(_decs) == 0 and "ticket_unclear_parked" not in _e2e_events
    and "ticket_clarified" not in _e2e_events,
    f"decs={_decs} events={_e2e_events}")

# ===========================================================================
# SUMMARY
# ===========================================================================
total = len(results)
passed = sum(1 for _, ok, _ in results if ok)
fail_details = [(n, d) for n, ok, d in results if not ok]

print("\n=========================")
print(f"  PASS/FAIL SUMMARY")
print("=========================")
for label, ok, det in results:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {label}" + (f"  ({det})" if det and not ok else ""))
print("---")
print(f"  {passed}/{total} passed")
if fail_details:
    print("\n  FAILED CHECKS:")
    for n, d in fail_details:
        print(f"    [{n}] {d}")
print(f"\n  RESULT: {'ALL GREEN' if passed == total else f'{total - passed} FAIL'}")

if passed < total:
    sys.exit(1)
