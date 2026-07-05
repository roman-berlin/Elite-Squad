"""Run-cost accounting — the two telemetry gaps found on the EU-139 run (2026-07-05 audit).

Gap 1 — run_end under-reported the run cost. The run_end audit event's total_cost_usd sums the
per-ticket report costs, but three per-ticket stages never reached that sum: the provost security
gate ($0.289 — its (ok, report) return can't carry cost), and the gap-detect ($0.042) + squad-lead
($0.344) plan-phase calls when the plan is thin and build_delegated returns (None, 0) for a solo
fallback. Ledger-true cost $2.156, reported $1.481.

Gap 2 — the gap-detect and squad-lead usage-ledger rows carried no ticket key ("k") even though
they ran inside the EU-139 ticket flow, so per-ticket burn slicing undercounts. The follow-up
sweep found the same hole in two more per-ticket stages: the soldier·<role> dispatch rows and
the provost-gate row.

Pinned here:
  1. detect_domain_gap returns its own burn and threads ticket_id into run_agent.
  2. build_delegated (thin plan) reports the sunk plan-phase burn via the `sunk` out-param, and
     the squad-lead call carries the ticket id.
  3. builder.build folds the sunk burn into the solo BuildResult (so loop's `cost += build.cost_usd`
     and _burn("builder", …) see it).
  4. provost.gate writes its cost into store.stage_costs["provost"] (USD mirror of the EU-96
     token_burn write) without breaking legacy store stubs or store=None.
  5. loop._attempt: the ticket report cost — the number run_end sums — includes the gate spend.
  6. usage.record stamps "k" on the ledger row when a ticket_id is given (the choke-point that
     makes 1/2 land in usage_ledger.jsonl).
  7. _soldier threads req.ticket.id into run_agent so every soldier·<role> ledger row carries
     the ticket key (full build_delegated dispatch path, not just the planner).
  8. provost.gate takes an additive ticket_id kwarg and threads it to run_agent; loop._attempt
     passes ticket.id at the gate call site. Legacy calls without the kwarg stay unchanged.

All offline — SDK and agents are stubbed; no real models, no network.
"""
import asyncio
import json
import os
import sys
import tempfile
import types

# ── SDK stub (no real model calls) ───────────────────────────────────────────
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k): self.__dict__.update(k)
    def __call__(self, *a, **k): return self


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.builder as builder                 # noqa: E402
import orchestrator.loop as loop                       # noqa: E402
import orchestrator.provost as provost_mod             # noqa: E402
import orchestrator.squad as squad                     # noqa: E402
import orchestrator.usage as usage                     # noqa: E402
from orchestrator.agent import AgentRun                # noqa: E402
from orchestrator.config import Config, AppConfig      # noqa: E402
from orchestrator.contracts import (                   # noqa: E402
    BuildArtifact, BuildRequest, BuildResult, GateResult, Outcome,
    PerTicketArtifactStore, ReviewResult, ReviewVerdict, SecurityArtifact,
    TestEngineerResult, Ticket, TicketReport, Verdict,
)

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


_aud_fd, _aud_path = tempfile.mkstemp(suffix=".jsonl")
os.close(_aud_fd)

_APP = AppConfig(name="automatixy", repo_path="/tmp",
                 base_branch="DEV", protected_branch="MAIN", backlog_backend="none")


def _ticket(tid: str = "EU-139") -> Ticket:
    return Ticket(id=tid, key=tid,
                  summary="Fix provost gate contradictory error handling",
                  description="A sizable change.",
                  acceptance_criteria=["one", "two", "three", "four"],
                  app="automatixy", ephemeral=True)


# The EU-139 run's actual ledger numbers — pinned so the harness mirrors the audit evidence.
GAP_COST, GAP_IN, GAP_OUT = 0.041794, 18898, 552
LEAD_COST, LEAD_IN, LEAD_OUT = 0.344076, 68517, 1507
PROVOST_COST, PROVOST_IN, PROVOST_OUT = 0.288974, 28044, 845

_calls: list[tuple[str, object]] = []   # (tag, ticket_id) per fake run_agent call

_THIN_PLAN = '[{"role":"ordnance-be","title":"only slice","detail":"one slice","size":"M"}]'


async def _fake_run_agent(prompt, options, tag="", ticket_id=None, pass_number=None, **kw):
    _calls.append((tag, ticket_id))
    if tag == "gap-detect":
        reply = '{"covered": true, "domain": "vanguard-fe"}'
        return AgentRun(text=reply, final=reply, cost_usd=GAP_COST, num_turns=1,
                        is_error=False, tools=[], input_tokens=GAP_IN, output_tokens=GAP_OUT)
    if tag == "squad-lead":
        return AgentRun(text=_THIN_PLAN, final=_THIN_PLAN, cost_usd=LEAD_COST, num_turns=2,
                        is_error=False, tools=["Read"], input_tokens=LEAD_IN, output_tokens=LEAD_OUT)
    return AgentRun(text="done", final="done", cost_usd=0.0, num_turns=1,
                    is_error=False, tools=[])


# ══════════════════════════════════════════════════════════════════════════════
# 1. detect_domain_gap — burn returned, ticket id threaded to the ledger choke-point
# ══════════════════════════════════════════════════════════════════════════════
_orig_squad_runner = squad.run_agent
squad.run_agent = _fake_run_agent

_calls.clear()
res = asyncio.run(squad.detect_domain_gap("some ticket", squad.SQUAD, ticket_id="EU-139"))
chk("gap-detect: (gap, domain) prefix unchanged", res[:2] == (False, None), str(res[:2]))
chk("gap-detect: burn dict carries the call's cost",
    len(res) > 2 and abs(res[2].get("cost_usd", 0) - GAP_COST) < 1e-9, str(res[2:]))
chk("gap-detect: burn dict carries the call's tokens",
    res[2].get("input_tokens") == GAP_IN and res[2].get("output_tokens") == GAP_OUT, str(res[2]))
chk("gap-detect: run_agent received ticket_id='EU-139' (ledger 'k' wiring)",
    _calls == [("gap-detect", "EU-139")], str(_calls))

_calls.clear()
res_nt = asyncio.run(squad.detect_domain_gap("some ticket", squad.SQUAD))
chk("gap-detect: no ticket context → ticket_id=None (no bogus 'k')",
    _calls == [("gap-detect", None)] and res_nt[:2] == (False, None), str(_calls))


async def _explode(prompt, options, tag="", **kw):
    raise RuntimeError("model down")

squad.run_agent = _explode
res_err = asyncio.run(squad.detect_domain_gap("some ticket", squad.SQUAD, ticket_id="EU-139"))
chk("gap-detect: exception → (False, None, {}) fail-safe",
    res_err[:2] == (False, None) and res_err[2] == {}, str(res_err))
squad.run_agent = _fake_run_agent


# ══════════════════════════════════════════════════════════════════════════════
# 2. build_delegated (thin plan) — sunk burn reported, squad-lead tagged with the ticket
#    This is the exact EU-139 path: gap-detect → squad-lead → thin plan → solo fallback.
# ══════════════════════════════════════════════════════════════════════════════
_cfg = Config(apps=[_APP], audit_path=_aud_path, use_worktree=False, delegation_enabled=True)

_calls.clear()
_sunk: dict = {}
_res, _n = asyncio.run(squad.build_delegated(
    BuildRequest(_ticket(), "autodev/EU-139", iteration=1), _APP, _cfg, sunk=_sunk))
chk("thin plan: still returns (None, 0) — caller falls back to solo", _res is None and _n == 0,
    f"{_res},{_n}")
chk("thin plan: sunk cost = gap-detect + squad-lead ($0.042 + $0.344)",
    abs(_sunk.get("cost_usd", 0) - (GAP_COST + LEAD_COST)) < 1e-9, str(_sunk))
chk("thin plan: sunk tokens = gap-detect + squad-lead",
    _sunk.get("input_tokens") == GAP_IN + LEAD_IN
    and _sunk.get("output_tokens") == GAP_OUT + LEAD_OUT, str(_sunk))
chk("thin plan: squad-lead run_agent received ticket_id='EU-139' (ledger 'k' wiring)",
    ("squad-lead", "EU-139") in _calls, str(_calls))
chk("thin plan: gap-detect inside the ticket flow also carried the ticket id",
    ("gap-detect", "EU-139") in _calls, str(_calls))

# Legacy direct call without sunk= — the old contract must be preserved (additive param).
_res_legacy, _n_legacy = asyncio.run(squad.build_delegated(
    BuildRequest(_ticket(), "autodev/EU-139", iteration=1), _APP, _cfg))
chk("thin plan: legacy call without sunk= still returns (None, 0)",
    _res_legacy is None and _n_legacy == 0, f"{_res_legacy},{_n_legacy}")


# ══════════════════════════════════════════════════════════════════════════════
# 2b. build_delegated (two-subtask plan) — every soldier dispatch carries the ticket id
#     so the soldier·<role> ledger rows get a "k" (the second hole of Gap 2).
# ══════════════════════════════════════════════════════════════════════════════
SOLDIER_COST = 0.15

_PLAN_TWO = ('[{"role":"ordnance-be","title":"add endpoint","detail":"POST /export","size":"M"},'
             '{"role":"logistics-db","title":"migration","detail":"export_jobs table","size":"M"}]')


async def _fake_run_agent_squad(prompt, options, tag="", ticket_id=None, pass_number=None, **kw):
    _calls.append((tag, ticket_id))
    if tag == "gap-detect":
        reply = '{"covered": true, "domain": "vanguard-fe"}'
        return AgentRun(text=reply, final=reply, cost_usd=GAP_COST, num_turns=1,
                        is_error=False, tools=[], input_tokens=GAP_IN, output_tokens=GAP_OUT)
    if tag == "squad-lead":
        return AgentRun(text=_PLAN_TWO, final=_PLAN_TWO, cost_usd=LEAD_COST, num_turns=2,
                        is_error=False, tools=["Read"], input_tokens=LEAD_IN, output_tokens=LEAD_OUT)
    return AgentRun(text="done", final=f"implemented {tag}", cost_usd=SOLDIER_COST, num_turns=4,
                    is_error=False, tools=["Edit"])


squad.run_agent = _fake_run_agent_squad
_calls.clear()
_res2, _n2 = asyncio.run(squad.build_delegated(
    BuildRequest(_ticket(), "autodev/EU-139", iteration=1), _APP, _cfg))
_soldier_calls = [c for c in _calls if c[0].startswith("soldier·")]
chk("squad dispatch: two subtasks → BuildResult, n=2", _res2 is not None and _n2 == 2,
    f"{_res2 is not None},{_n2}")
chk("squad dispatch: every soldier run_agent received ticket_id='EU-139' (ledger 'k' wiring)",
    _soldier_calls == [("soldier·ordnance-be", "EU-139"), ("soldier·logistics-db", "EU-139")],
    str(_soldier_calls))
chk("squad dispatch: aggregate cost = gap-detect + squad-lead + both soldiers",
    _res2 is not None and abs(_res2.cost_usd - (GAP_COST + LEAD_COST + 2 * SOLDIER_COST)) < 1e-9,
    str(getattr(_res2, "cost_usd", None)))

squad.run_agent = _orig_squad_runner


# ══════════════════════════════════════════════════════════════════════════════
# 3. builder.build — sunk delegation burn folds into the solo BuildResult
# ══════════════════════════════════════════════════════════════════════════════
SOLO_COST, SOLO_IN, SOLO_OUT = 0.652455, 636632, 8773   # EU-139's solo builder ledger row


async def _fake_build_delegated(req, app, cfg, audit=None, sunk=None):
    if sunk is not None:
        sunk["cost_usd"] = GAP_COST + LEAD_COST
        sunk["num_turns"] = 3
        sunk["input_tokens"] = GAP_IN + LEAD_IN
        sunk["output_tokens"] = GAP_OUT + LEAD_OUT
    return None, 0


async def _fake_solo(req, app, cfg, *, spec=None):
    return BuildResult(ok=True, summary="SOLO build done", cost_usd=SOLO_COST, num_turns=22,
                       raw="", tools=[], input_tokens=SOLO_IN, output_tokens=SOLO_OUT)


_orig_delegated = squad.build_delegated
_orig_solo = builder._solo_build
squad.build_delegated = _fake_build_delegated
builder._solo_build = _fake_solo
try:
    _b = asyncio.run(builder.build(
        BuildRequest(_ticket(), "autodev/EU-139", iteration=1), _APP, _cfg))
finally:
    squad.build_delegated = _orig_delegated
    builder._solo_build = _orig_solo

chk("builder.build: solo result absorbs the sunk plan-phase cost",
    abs(_b.cost_usd - (SOLO_COST + GAP_COST + LEAD_COST)) < 1e-9, str(_b.cost_usd))
chk("builder.build: solo result absorbs the sunk tokens (EU-96 _burn sees them)",
    _b.input_tokens == SOLO_IN + GAP_IN + LEAD_IN
    and _b.output_tokens == SOLO_OUT + GAP_OUT + LEAD_OUT,
    f"in={_b.input_tokens},out={_b.output_tokens}")


# ══════════════════════════════════════════════════════════════════════════════
# 4. provost.gate — cost lands in store.stage_costs; legacy stores stay safe
# ══════════════════════════════════════════════════════════════════════════════
_provost_calls: list[tuple[str, object]] = []   # (tag, ticket_id) per fake gate runner call


async def _fake_provost_runner(prompt, options, tag="", ticket_id=None, **kw):
    _provost_calls.append((tag, ticket_id))
    report = "\n".join([
        "§1-secrets: no secrets touched.",
        "§2-authz: no routes changed.",
        "§3-injection: no injection surface.",
        "SECURITY GATE: PASS",
    ])
    return AgentRun(text=report, final=report, cost_usd=PROVOST_COST, num_turns=4,
                    is_error=False, tools=["Grep"],
                    input_tokens=PROVOST_IN, output_tokens=PROVOST_OUT)


_orig_provost_runner = provost_mod.run_agent
provost_mod.run_agent = _fake_provost_runner

_store = PerTicketArtifactStore()
_ok, _rep = asyncio.run(provost_mod.gate(_cfg, _APP, "diff --git a b", store=_store))
chk("provost.gate: verdict unchanged (PASS)", _ok is True, _rep[:60])
chk("provost.gate: cost recorded in store.stage_costs['provost']",
    abs(_store.stage_costs.get("provost", 0) - PROVOST_COST) < 1e-9, str(_store.stage_costs))
chk("provost.gate: token burn still recorded (EU-96 unchanged)",
    _store.token_burn.get("provost") == PROVOST_IN + PROVOST_OUT, str(_store.token_burn))

# Legacy store stub without stage_costs — the guarded write must not flip PASS to fail-closed.
_legacy_store = types.SimpleNamespace(token_burn={}, put=lambda a: None,
                                      get_security=lambda: None)
_ok2, _rep2 = asyncio.run(provost_mod.gate(_cfg, _APP, "diff --git a b", store=_legacy_store))
chk("provost.gate: legacy store without stage_costs → still PASS (no fail-closed regression)",
    _ok2 is True, _rep2[:60])
chk("provost.gate: legacy store token burn unchanged",
    _legacy_store.token_burn.get("provost") == PROVOST_IN + PROVOST_OUT,
    str(_legacy_store.token_burn))

_ok3, _rep3 = asyncio.run(provost_mod.gate(_cfg, _APP, "diff --git a b", store=None))
chk("provost.gate: store=None → still PASS (no crash)", _ok3 is True, _rep3[:60])

# Ticket threading (the second hole of Gap 2): gate(ticket_id=…) reaches run_agent so the
# 'provost-gate' ledger row gets a "k"; the three calls above (no kwarg) stay ticket-less.
chk("provost.gate: legacy calls without ticket_id → run_agent got ticket_id=None",
    _provost_calls == [("provost-gate", None)] * 3, str(_provost_calls))
_ok4, _rep4 = asyncio.run(provost_mod.gate(_cfg, _APP, "diff --git a b", store=None,
                                           ticket_id="EU-139"))
chk("provost.gate: ticket_id kwarg keeps the (ok, report) contract (PASS)", _ok4 is True,
    _rep4[:60])
chk("provost.gate: run_agent received ticket_id='EU-139' (ledger 'k' wiring)",
    _provost_calls[-1] == ("provost-gate", "EU-139"), str(_provost_calls))

provost_mod.run_agent = _orig_provost_runner


# ══════════════════════════════════════════════════════════════════════════════
# 5. loop._attempt — the ticket report cost (what run_end sums) includes the gate.
#    EU-139 arithmetic: builder 0.6524547 + TE 0.3760782 + review 0.4528022 = 1.4813351
#    was the OLD run_end total; + provost 0.288974 is what it must report now.
# ══════════════════════════════════════════════════════════════════════════════
BUILD_C, TE_C, REVIEW_C = 0.6524547, 0.3760782, 0.4528022


class _Audit:
    def __init__(self): self.events = []
    def record(self, e, **k): self.events.append({"event": e, **k})


class _Backlog:
    def set_status(self, *a, **k): pass
    def add_comment(self, *a, **k): pass


class _Git:
    def has_changes(self): return True
    def diff_against_base(self): return "diff --git a/x b/x\n+added_line"
    def changed_paths(self): return ["orchestrator/loop.py"]


class _StubBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "sized")

    @staticmethod
    async def build(req, app, cfg, audit=None, store=None, spec=None):
        if store is not None:
            store.put(BuildArtifact(files_changed=[], diff_digest="d",
                                    decisions=[], open_questions=[]))
        return BuildResult(ok=True, summary="built", cost_usd=BUILD_C, num_turns=22)


class _StubTE:
    @staticmethod
    async def ensure_coverage(ticket, app, cfg, store=None, build_artifact=None):
        return TestEngineerResult(ok=True, coverage="all green", cost_usd=TE_C)


class _StubReviewer:
    @staticmethod
    async def review(diff, ticket, app, cfg, iteration=1, store=None, build_artifact=None):
        if store is not None:
            store.put(ReviewVerdict(verdict=Verdict.PASS, blocking=[], notes=[]))
        return ReviewResult(verdict=Verdict.PASS, spec_met=True, cost_usd=REVIEW_C)


_gate_ticket_ids: list = []


async def _fake_gate(cfg, app, diff, store=None, ticket_id=None, **kw):
    """Mimic the real gate's store writes: signed artifact + stage_costs + token burn."""
    _gate_ticket_ids.append(ticket_id)
    if store is not None:
        store.put(SecurityArtifact(s1_secrets="no secrets touched",
                                   s2_authz="no routes changed",
                                   s3_injection="parameterised throughout", signed=True))
        store.token_burn["provost"] = store.token_burn.get("provost", 0) + PROVOST_IN + PROVOST_OUT
        store.stage_costs["provost"] = store.stage_costs.get("provost", 0.0) + PROVOST_COST
    return (True, "SECURITY GATE: PASS")


_land_costs: list[float] = []


def _fake_land(tk, app, cfg, git, backlog, audit, branch, iteration, cost, build, review,
               security_block=None, coverage=""):
    _land_costs.append(cost)
    return TicketReport(tk.id, Outcome.MERGED, iteration, cost, app.name, branch)


_orig = (loop.builder_mod, loop.test_engineer_mod, loop.reviewer_mod,
         loop.run_gate, loop._land, loop._notify, provost_mod.gate)
loop.builder_mod = _StubBuilder
loop.test_engineer_mod = _StubTE
loop.reviewer_mod = _StubReviewer
loop.run_gate = lambda app, changed=None, **_: GateResult(passed=True, report="")
loop._land = _fake_land
loop._notify = lambda c, t: None
provost_mod.gate = _fake_gate

_loop_cfg = Config(apps=[_APP], audit_path=_aud_path, use_worktree=False,
                   security_gate=True, pm_enabled=False, max_iterations=1)
try:
    _report = asyncio.run(loop._attempt(_ticket(), _APP, _loop_cfg, _Git(), _Backlog(),
                                        _Audit(), loop.Budget(0), "autodev/EU-139"))
finally:
    (loop.builder_mod, loop.test_engineer_mod, loop.reviewer_mod,
     loop.run_gate, loop._land, loop._notify, provost_mod.gate) = _orig

_expected = BUILD_C + TE_C + REVIEW_C + PROVOST_COST
chk("loop: ticket landed (MERGED)", _report.outcome == Outcome.MERGED, str(_report.outcome))
chk("loop: report cost = builder + TE + review + provost gate (run_end now sums the gate)",
    _land_costs and abs(_land_costs[0] - _expected) < 1e-9,
    f"got={_land_costs}, want={_expected}")
chk("loop: TicketReport.cost_usd carries the full ticket spend",
    abs(_report.cost_usd - _expected) < 1e-9, str(_report.cost_usd))
chk("loop: gate call site passes ticket.id (so the 'provost-gate' ledger row gets a 'k')",
    _gate_ticket_ids == ["EU-139"], str(_gate_ticket_ids))


# ══════════════════════════════════════════════════════════════════════════════
# 6. usage.record — a tagged call with ticket_id lands a "k" on the ledger row
#    (the choke-point that turns the ticket_id threading into sliceable burn).
# ══════════════════════════════════════════════════════════════════════════════
_led_fd, _led_audit = tempfile.mkstemp(suffix=".jsonl")
os.close(_led_fd)
usage.configure(_led_audit)
try:
    usage.record("claude-haiku-4-5", GAP_IN, GAP_OUT, GAP_COST, "gap-detect", ticket_id="EU-139")
    usage.record("claude-sonnet-4-6", LEAD_IN, LEAD_OUT, LEAD_COST, "squad-lead", ticket_id="EU-139")
    usage.record("claude-sonnet-4-6", 1000, 200, SOLDIER_COST, "soldier·ordnance-be", ticket_id="EU-139")
    usage.record("claude-opus-4-8", PROVOST_IN, PROVOST_OUT, PROVOST_COST, "provost-gate", ticket_id="EU-139")
    usage.record("claude-haiku-4-5", 10, 5, 0.001, "gap-detect")   # no ticket context
    _ledger = usage._path()
    rows = [json.loads(ln) for ln in _ledger.read_text(encoding="utf-8").splitlines()]
finally:
    usage._PATH = None   # unconfigure — later harness code must not touch the tmp ledger

chk("ledger: gap-detect row carries k=EU-139",
    any(r.get("g") == "gap-detect" and r.get("k") == "EU-139" for r in rows), str(rows))
chk("ledger: squad-lead row carries k=EU-139",
    any(r.get("g") == "squad-lead" and r.get("k") == "EU-139" for r in rows), str(rows))
chk("ledger: soldier row carries k=EU-139",
    any(r.get("g") == "soldier·ordnance-be" and r.get("k") == "EU-139" for r in rows), str(rows))
chk("ledger: provost-gate row carries k=EU-139",
    any(r.get("g") == "provost-gate" and r.get("k") == "EU-139" for r in rows), str(rows))
chk("ledger: no-ticket gap-detect row has no k (shape additive, not forced)",
    any(r.get("g") == "gap-detect" and "k" not in r for r in rows), str(rows))


# ══════════════════════════════════════════════════════════════════════════════
# Report
# ══════════════════════════════════════════════════════════════════════════════
passed = [n for n, ok, _ in results if ok]
failed = [(n, d) for n, ok, d in results if not ok]
print(f"\nrun_cost_accounting_test: {len(passed)}/{len(results)} passed")
for n, d in failed:
    print(f"  FAIL: {n}" + (f" — {d}" if d else ""))
if failed:
    sys.exit(1)
