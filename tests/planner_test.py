"""The Planner officer (Phase-2 §2) — parse + fail-safe + burn accounting + builder brief.

The Planner is the single up-front design call that replaces the Architect ADR + squad-lead
planning + Scrum split + Senior-PM prebuild triage. Pinned here (no live model — SDK stubbed):

  1. parse_plan — a clean JSON reply yields verdict + approach + testable_ac + in_scope_files;
     tolerant of prose/fences around the JSON and of a comma-string list.
  2. fail-safe — empty/garbled reply, or an invalid verdict, defaults to BUILD with empty fields
     (so the Builder always proceeds from the raw ticket; a Planner hiccup never blocks).
  3. as_builder_brief — renders approach + testable AC + in-scope files for the Builder prompt,
     and is EMPTY for a non-BUILD verdict or an empty plan (so the prompt is unchanged then).
  4. plan() — threads burn (cost/tokens) onto the result, audits a 'planner' event, and an agent
     exception still returns a BUILD result (never raises).
"""
import asyncio
import sys
import types

# ── SDK stub (no real model calls) ───────────────────────────────────────────
sdk = types.ModuleType("claude_agent_sdk")


class _Opts:
    def __init__(self, **kw): self.__dict__.update(kw)


class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self


sdk.ClaudeAgentOptions = _Opts
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.planner as planner              # noqa: E402
from orchestrator.agent import AgentRun             # noqa: E402
from orchestrator.config import Config, AppConfig   # noqa: E402
from orchestrator.contracts import Ticket           # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def _ticket(tid="EU-1", **kw) -> Ticket:
    return Ticket(id=tid, key=tid, summary=kw.get("summary", "Add a retry to the deploy step"),
                  description=kw.get("description", "The deploy flakes; add a bounded retry."),
                  acceptance_criteria=kw.get("ac", ["deploy retries up to 3×", "gives up after 3"]),
                  app="automatixy", ephemeral=True, labels=kw.get("labels", []))


# ══════════════════════════════════════════════════════════════════════════════
# 1. parse_plan — clean JSON
# ══════════════════════════════════════════════════════════════════════════════
CLEAN = ('{"verdict": "BUILD", "approach": "Wrap deploy in a retry loop (deploy.py).",'
         ' "testable_ac": ["3 failures → aborts", "1st-try success → no retry"],'
         ' "in_scope_files": ["orchestrator/deploy.py"], "answer": ""}')
p = planner.parse_plan(CLEAN)
chk("parse: verdict BUILD", p.verdict == "BUILD", p.verdict)
chk("parse: approach extracted", "retry loop" in p.approach, p.approach)
chk("parse: testable_ac list", p.testable_ac == ["3 failures → aborts", "1st-try success → no retry"],
    str(p.testable_ac))
chk("parse: in_scope_files list", p.in_scope_files == ["orchestrator/deploy.py"], str(p.in_scope_files))

# tolerant of prose + code fences around the JSON
WRAPPED = "Here's the plan:\n```json\n" + CLEAN + "\n```\nDone."
chk("parse: tolerates prose + fences", planner.parse_plan(WRAPPED).approach == p.approach)

# comma-string list coerced
COMMA = '{"verdict":"BUILD","in_scope_files":"a.py, b.py","testable_ac":"x"}'
pc = planner.parse_plan(COMMA)
chk("parse: comma-string in_scope_files coerced", pc.in_scope_files == ["a.py", "b.py"], str(pc.in_scope_files))
chk("parse: comma-string testable_ac coerced", pc.testable_ac == ["x"], str(pc.testable_ac))

# non-BUILD verdict carries the answer
ANS = '{"verdict":"ANSWER","answer":"Already implemented at loop.py:42.","approach":"","testable_ac":[]}'
pa = planner.parse_plan(ANS)
chk("parse: ANSWER verdict + answer", pa.verdict == "ANSWER" and "loop.py:42" in pa.answer, str(pa))

# ══════════════════════════════════════════════════════════════════════════════
# 2. fail-safe defaults
# ══════════════════════════════════════════════════════════════════════════════
chk("failsafe: empty reply → BUILD, empty fields",
    planner.parse_plan("").verdict == "BUILD" and planner.parse_plan("").testable_ac == [])
chk("failsafe: garbage reply → BUILD", planner.parse_plan("total nonsense, no json").verdict == "BUILD")
chk("failsafe: invalid verdict → BUILD",
    planner.parse_plan('{"verdict":"NONSENSE","approach":"x"}').verdict == "BUILD")

# ══════════════════════════════════════════════════════════════════════════════
# 3. as_builder_brief
# ══════════════════════════════════════════════════════════════════════════════
brief = p.as_builder_brief()
chk("brief: carries approach", "APPROACH" in brief and "retry loop" in brief, brief[:80])
chk("brief: carries testable AC with 'write a test for EACH'", "write a test for EACH" in brief.upper()
    or "TESTABLE ACCEPTANCE" in brief, brief[:120])
chk("brief: lists in-scope files", "orchestrator/deploy.py" in brief)
chk("brief: EMPTY for a non-BUILD verdict", planner.parse_plan(ANS).as_builder_brief() == "")
chk("brief: EMPTY for an empty BUILD plan", planner.parse_plan("").as_builder_brief() == "")

# ══════════════════════════════════════════════════════════════════════════════
# 4. plan() — burn threaded, audited, exception-safe
# ══════════════════════════════════════════════════════════════════════════════
_cfg = Config(apps=[AppConfig(name="automatixy", repo_path="/tmp", base_branch="dev",
                              backlog_backend="none")], audit_path="/tmp/audit.jsonl",
              use_worktree=False)


class _Audit:
    def __init__(self): self.events = []
    def record(self, e, **k): self.events.append({"event": e, **k})


async def _fake_run_agent(prompt, options, tag="", ticket_id=None, **kw):
    chk("plan: run_agent tagged 'planner' with ticket_id", tag == "planner" and ticket_id == "EU-1")
    return AgentRun(text=CLEAN, final=CLEAN, cost_usd=0.03, num_turns=2, is_error=False,
                    tools=["Read"], input_tokens=5000, output_tokens=300,
                    provider="Anthropic", model_version="claude-opus-4-8")


_orig = planner.run_agent
planner.run_agent = _fake_run_agent
au = _Audit()
res = asyncio.run(planner.plan(_cfg, _ticket(), audit=au))
planner.run_agent = _orig
chk("plan: parsed the agent reply", res.verdict == "BUILD" and res.in_scope_files == ["orchestrator/deploy.py"])
chk("plan: burn threaded onto the result (Planner spend is countable)",
    abs(res.cost_usd - 0.03) < 1e-9 and res.input_tokens == 5000 and res.output_tokens == 300,
    f"{res.cost_usd},{res.input_tokens},{res.output_tokens}")
chk("plan: 'planner' audit event with verdict + counts",
    any(e["event"] == "planner" and e["verdict"] == "BUILD" and e["testable_ac"] == 2
        for e in au.events), str(au.events))


async def _boom(prompt, options, tag="", **kw):
    raise RuntimeError("model down")


planner.run_agent = _boom
res_err = asyncio.run(planner.plan(_cfg, _ticket(), audit=_Audit()))
planner.run_agent = _orig
chk("plan: agent exception → BUILD result, never raises",
    res_err.verdict == "BUILD" and "planner error" in res_err.raw, res_err.raw[:80])

# ══════════════════════════════════════════════════════════════════════════════
# 5. loop wiring — planner_enabled runs the Planner before build, injects the brief into the
#    Builder, sharpens the SpecArtifact AC, routes SPLIT to Scrum; off → inert.
# ══════════════════════════════════════════════════════════════════════════════
import orchestrator.loop as loop                     # noqa: E402
from orchestrator.contracts import (                 # noqa: E402
    BuildArtifact, BuildResult, GateResult, Outcome, ReviewResult, ReviewVerdict,
    TestEngineerResult, TicketReport, Verdict,
)

_APP = AppConfig(name="automatixy", repo_path="/tmp", base_branch="DEV",
                 protected_branch="MAIN", backlog_backend="none")
_captured_req = {}


class _Git:
    def has_changes(self): return True
    def diff_against_base(self): return "diff --git a/x b/x\n+line"
    def changed_paths(self): return ["orchestrator/deploy.py"]
    def current_sha(self): return "s1"
    def base_sha(self): return "s1"


class _Backlog:
    def set_status(self, *a, **k): pass
    def add_comment(self, *a, **k): pass


class _StubBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "sized")

    @staticmethod
    async def build(req, app, cfg, audit=None, store=None, spec=None):
        _captured_req["adr"] = req.adr
        _captured_req["spec_ac"] = list(store.spec.acceptance) if (store and store.spec) else None
        if store is not None:
            store.put(BuildArtifact(files_changed=[], diff_digest="d", decisions=[], open_questions=[]))
        return BuildResult(ok=True, summary="built", cost_usd=0.1, num_turns=2)


class _StubReviewer:
    @staticmethod
    async def review(diff, ticket, app, cfg, iteration=1, store=None, build_artifact=None):
        if store is not None:
            store.put(ReviewVerdict(verdict=Verdict.PASS, blocking=[], notes=[]))
        return ReviewResult(verdict=Verdict.PASS, spec_met=True, cost_usd=0.1)


def _fake_land(tk, app, cfg, git, backlog, audit, branch, iteration, cost, build, review, coverage=""):
    return TicketReport(tk.id, Outcome.MERGED, iteration, cost, app.name, branch)


_BUILD_PLAN = planner.PlannerResult(
    verdict="BUILD", approach="Wrap deploy in a bounded retry (deploy.py).",
    testable_ac=["3 fails → abort", "first success → no retry"],
    in_scope_files=["orchestrator/deploy.py"], cost_usd=0.02, input_tokens=4000, output_tokens=200)


def _mkcfg(**kw):
    from pathlib import Path
    import tempfile
    d = Path(tempfile.mkdtemp())
    base = dict(apps=[_APP], audit_path=str(d / "audit.jsonl"), use_worktree=False,
                pm_enabled=False, test_gate=False, red_base_check=False, max_iterations=1)
    base.update(kw)
    return Config(**base)


_orig_loop = (loop.builder_mod, loop.reviewer_mod, loop.run_gate, loop._land, loop._notify)
loop.builder_mod = _StubBuilder
loop.reviewer_mod = _StubReviewer
loop.run_gate = lambda app, changed=None, **_: GateResult(passed=True, report="")
loop._land = _fake_land
loop._notify = lambda c, t: None

_plan_calls = {"n": 0}


async def _stub_plan(cfg, ticket, app=None, audit=None):
    _plan_calls["n"] += 1
    if audit is not None:
        audit.record("planner", ticket_id=ticket.id, verdict=_BUILD_PLAN.verdict,
                     testable_ac=len(_BUILD_PLAN.testable_ac), in_scope_files=len(_BUILD_PLAN.in_scope_files))
    return _BUILD_PLAN


_orig_plan = planner.plan
try:
    import orchestrator.planner as _pmod
    loop.__dict__.setdefault("planner", _pmod)
    _pmod.plan = _stub_plan

    # planner_enabled → Planner runs, brief injected, AC sharpened, ticket lands
    _captured_req.clear(); _plan_calls["n"] = 0
    au = _Audit()
    rep = asyncio.run(loop._attempt(_ticket("EU-P1"), _APP, _mkcfg(planner_enabled=True),
                                    _Git(), _Backlog(), au, loop.Budget(0), "autodev/EU-P1"))
    chk("loop: Planner ran once (planner_enabled)", _plan_calls["n"] == 1, str(_plan_calls))
    chk("loop: design brief injected into the Builder (req.adr)",
        _captured_req.get("adr") and "bounded retry" in _captured_req["adr"], str(_captured_req.get("adr"))[:80])
    chk("loop: testable AC sharpened the SpecArtifact the Builder reads",
        _captured_req.get("spec_ac") == ["3 fails → abort", "first success → no retry"],
        str(_captured_req.get("spec_ac")))
    chk("loop: 'planner' audit event recorded", any(e["event"] == "planner" for e in au.events))
    chk("loop: ticket built + landed", rep.outcome == Outcome.MERGED, str(rep.outcome))

    # planner_enabled=False → Planner NOT called (inert)
    _plan_calls["n"] = 0
    asyncio.run(loop._attempt(_ticket("EU-P2"), _APP, _mkcfg(planner_enabled=False),
                              _Git(), _Backlog(), _Audit(), loop.Budget(0), "autodev/EU-P2"))
    chk("loop: Planner OFF → not called (inert by default)", _plan_calls["n"] == 0, str(_plan_calls))

    # SPLIT verdict → Scrum split → REQUEUED (builder never runs)
    async def _split_plan(cfg, ticket, app=None, audit=None):
        return planner.PlannerResult(verdict="SPLIT", answer="two unrelated asks", cost_usd=0.01)
    _pmod.plan = _split_plan

    class _Scrum:
        @staticmethod
        async def split(cfg, app_name, ticket, recap="", reason=""):
            return {"ok": True, "keys": ["EU-P3a", "EU-P3b"]}
    import sys as _sys
    _sys.modules["orchestrator.scrum"] = _Scrum
    _captured_req.clear()
    rep3 = asyncio.run(loop._attempt(_ticket("EU-P3"), _APP, _mkcfg(planner_enabled=True),
                                     _Git(), _Backlog(), _Audit(), loop.Budget(0), "autodev/EU-P3"))
    chk("loop: SPLIT verdict → REQUEUED (Scrum split), builder never ran",
        rep3.outcome == Outcome.REQUEUED and _captured_req.get("adr") is None, str(rep3.outcome))
finally:
    _pmod.plan = _orig_plan
    (loop.builder_mod, loop.reviewer_mod, loop.run_gate, loop._land, loop._notify) = _orig_loop


# ══════════════════════════════════════════════════════════════════════════════
passed = sum(1 for _, ok, _ in results if ok)
print(f"\nplanner_test: {passed}/{len(results)} passed")
for n, ok, det in results:
    if not ok:
        print(f"  FAIL: {n}" + (f" — {det}" if det else ""))
sys.exit(0 if passed == len(results) else 1)
