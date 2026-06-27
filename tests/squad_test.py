"""QA for squad delegation — planner parsing, routing, aggregation, and fail-safe fallback.
Stubs the Agent SDK + run_agent so no real model runs. Prints a PASS/FAIL checklist."""
import asyncio, sys, types
from pathlib import Path

REPO = "."
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, REPO)

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))

from orchestrator import squad, builder
from orchestrator.agent import AgentRun
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket, BuildRequest

# ---- fake run_agent: planner returns a 3-subtask plan; soldiers + solo return work ----
PLAN = ('Here is the split:\n```json\n'
        '[{"role":"logistics-db","title":"migration","detail":"add export_jobs table","size":"M"},'
        '{"role":"ordnance-be","title":"endpoint","detail":"POST /exports uses the table","size":"L"},'
        '{"role":"frontend","title":"button","detail":"Export button on /leads","size":"S"}]\n```')
_mode = {"plan": PLAN, "raise_plan": False}
async def fake_run_agent(prompt, options, tag="", ticket_id=None, pass_number=None):
    if tag == "squad-lead":
        if _mode["raise_plan"]:
            raise RuntimeError("planner exploded")
        return AgentRun(text=_mode["plan"], final=_mode["plan"], cost_usd=0.10, num_turns=3,
                        is_error=False, tools=["Read"])
    if tag.startswith("soldier"):
        return AgentRun(text="done", final="implemented " + tag, cost_usd=0.20, num_turns=5,
                        is_error=False, tools=["Edit"])
    return AgentRun(text="solo", final="SOLO build done", cost_usd=0.5, num_turns=9,
                    is_error=False, tools=["Edit"])
squad.run_agent = fake_run_agent
builder.run_agent = fake_run_agent

# EU-69 lifecycle: build_delegated() records domain use + may propose promotion after a SUCCESSFUL
# synthesis. Stub all three HR entry points to NON-BLOCKING trackers up front. The earlier iteration
# left hr.check_promote()/synthesize_specialists() defaulting to the bare builtin input(); reached from
# this unattended suite (no Commander at a TTY) it BLOCKED on stdin once the shared audit file crossed
# the promote threshold — that is the 1800 s tests/run_all.py hang this guards against. HR's own
# behaviour (parse / gate / promote) is covered exhaustively by hr_synthesis_test.py; here we only need
# the squad ROUTING + a check that the lifecycle is wired, never a real prompt.
import orchestrator.hr as _hr_mod
_hr_calls = {"record": [], "promote": []}
_synth_mode = {"charters": []}   # what hr.synthesize_specialists returns by default (empty = no hire)
async def _default_synthesize(domain, ticket_text, cfg2, approver=None):
    return list(_synth_mode["charters"])
_hr_mod.synthesize_specialists = _default_synthesize
_hr_mod.record_domain_use = lambda domain, cfg2: _hr_calls["record"].append(domain)
_hr_mod.check_promote = lambda domain, cfg2, *a, **k: (_hr_calls["promote"].append(domain), False)[1]

class FakeAudit:
    def __init__(self): self.events = []
    def record(self, event, **kw): self.events.append((event, kw))

def mk(ac, summary="Add tenant-scoped CSV export", desc="A sizable feature touching db, api and ui."):
    return Ticket(id="AUTO-50", key="AUTO-50", summary=summary, description=desc,
                  acceptance_criteria=ac, app="automatixy")
app = AppConfig(name="automatixy", repo_path="/tmp/x", base_branch="DEV", protected_branch="MAIN",
                backlog_backend="none")
big_ac = ["criteria one", "criteria two", "criteria three", "criteria four"]

# ============================ parsing ============================
subs = squad.parse_subtasks(PLAN)
check("parse: 3 subtasks from fenced+prose JSON", len(subs) == 3, str(len(subs)))
check("parse: unknown role 'frontend' -> generalist", subs[2].role == "generalist", subs[2].role)
check("parse: roles + sizes kept", subs[0].role == "logistics-db" and subs[1].size == "L")
check("parse: size->effort (L->high, S->medium, M->medium)",
      subs[1].effort() == "high" and subs[2].effort() == "medium" and subs[0].effort() == "medium")
check("parse: bad size -> M",
      squad.parse_subtasks('[{"role":"devops","detail":"x","size":"HUGE"}]')[0].size == "M")
check("parse: empty detail dropped",
      squad.parse_subtasks('[{"role":"devops","detail":"","size":"S"}]') == [])
check("parse: garbage -> []", squad.parse_subtasks("no json here") == [])

# ============================ should_delegate ============================
import tempfile, os as _os
_audit_fd, _audit_path = tempfile.mkstemp(suffix=".jsonl"); _os.close(_audit_fd)  # fresh per run — no cross-run accumulation
cfg = Config(apps=[app], audit_path=_audit_path, use_worktree=False)
cfg.delegation_enabled = False
check("gate: disabled -> no delegate", squad.should_delegate(cfg, BuildRequest(mk(big_ac), "b", iteration=1)) is False)
cfg.delegation_enabled = True
check("gate: armed + many AC -> delegate", squad.should_delegate(cfg, BuildRequest(mk(big_ac), "b", iteration=1)) is True)
check("gate: retry (iteration>1) -> no delegate", squad.should_delegate(cfg, BuildRequest(mk(big_ac), "b", iteration=2)) is False)
check("gate: tiny ticket -> no delegate",
      squad.should_delegate(cfg, BuildRequest(mk([], summary="fix typo in label", desc="typo"), "b", iteration=1)) is False)

# ============================ build_delegated (aggregation + audit) ============================
audit = FakeAudit()
res, n = asyncio.run(squad.build_delegated(BuildRequest(mk(big_ac), "b", iteration=1), app, cfg, audit=audit))
check("delegated: 3 soldiers dispatched", n == 3, str(n))
check("delegated: cost aggregated (plan .10 + 3x .20)", abs(res.cost_usd - 0.70) < 1e-6, str(res.cost_usd))
check("delegated: turns aggregated (3 + 3x5)", res.num_turns == 18, str(res.num_turns))
check("delegated: tools aggregated", res.tools == ["Read", "Edit", "Edit", "Edit"], str(res.tools))
check("delegated: ok=True", res.ok is True)
check("delegated: summary names the squad", "Squad delegation" in res.summary and "Ordnance BE" in res.summary)
check("delegated: audit has 1 delegation + 3 soldier_build",
      sum(1 for e, _ in audit.events if e == "delegation") == 1
      and sum(1 for e, _ in audit.events if e == "soldier_build") == 3, str([e for e, _ in audit.events]))

# ============================ fallback: thin plan -> (None,0) ============================
_mode["plan"] = '[{"role":"ordnance-be","title":"only","detail":"one slice","size":"M"}]'
res1, n1 = asyncio.run(squad.build_delegated(BuildRequest(mk(big_ac), "b", iteration=1), app, cfg))
check("fallback: <2 subtasks -> (None, 0)", res1 is None and n1 == 0, f"{res1},{n1}")
_mode["plan"] = PLAN   # restore

# ============================ detect_domain_gap ============================
# Stub detect_domain_gap so these tests never hit a real LLM.
_gap_mode = {"gap": False, "domain": None, "raise": False}
async def fake_detect_domain_gap(ticket_text, sq):
    if _gap_mode["raise"]:
        raise RuntimeError("classifier blew up")
    return _gap_mode["gap"], _gap_mode["domain"]
squad.detect_domain_gap = fake_detect_domain_gap

# No gap: normal squad flow unaffected.
_gap_mode.update(gap=False, domain=None)
res2, n2 = asyncio.run(squad.build_delegated(BuildRequest(mk(big_ac), "b", iteration=1), app, cfg))
check("gap: no gap -> normal delegation (n>=2)", n2 >= 2 and res2 is not None, f"n={n2}")

# Gap detected + synthesis stub (returns None) -> (None, 0) -> caller falls through to solo.
_gap_mode.update(gap=True, domain="mql5")
res3, n3 = asyncio.run(squad.build_delegated(BuildRequest(mk(big_ac), "b", iteration=1), app, cfg))
check("gap: detected, stub -> (None, 0)", res3 is None and n3 == 0, f"{res3},{n3}")

# Gap detected + real synthesis result -> (result, 1).
async def fake_run_synthesis_real(domain, req, app, cfg):
    from orchestrator.contracts import BuildResult
    return BuildResult(ok=True, summary=f"synthesis:{domain}", cost_usd=0.05,
                       num_turns=2, raw="", tools=[])
_orig_synthesis = squad._run_synthesis
squad._run_synthesis = fake_run_synthesis_real
_gap_mode.update(gap=True, domain="mql5")
res4, n4 = asyncio.run(squad.build_delegated(BuildRequest(mk(big_ac), "b", iteration=1), app, cfg))
check("gap: detected, real synthesis -> (result, 1)", res4 is not None and n4 == 1 and "synthesis:mql5" in res4.summary, f"n={n4},{getattr(res4,'summary','?')[:40]}")
# EU-69 ephemeral lifecycle is WIRED: a successful synthesis records the domain's use and runs the
# promote check (both best-effort, both non-blocking). This is the path whose default approver=input
# wedged the suite for 1800 s in iteration 1; assert it fires AND returns without a prompt.
check("lifecycle: synthesis result records domain_use", "mql5" in _hr_calls["record"], str(_hr_calls["record"]))
check("lifecycle: synthesis result runs check_promote (no blocking prompt)", "mql5" in _hr_calls["promote"], str(_hr_calls["promote"]))

# EU-69 iter-3: a FAILED synthesis (ok=False — a real executed gate returned non-zero) must NOT accrue
# auto-promotion credit. record_domain_use / check_promote are gated on synthesis.ok, so a domain we
# couldn't verify never counts toward promoting it to a permanent officer.
async def fake_run_synthesis_failed(domain, req, app, cfg):
    from orchestrator.contracts import BuildResult
    return BuildResult(ok=False, summary=f"synthesis-failed:{domain}", cost_usd=0.05,
                       num_turns=2, raw="", tools=[])
squad._run_synthesis = fake_run_synthesis_failed
_hr_calls["record"].clear(); _hr_calls["promote"].clear()
_gap_mode.update(gap=True, domain="solidity")
res4b, n4b = asyncio.run(squad.build_delegated(BuildRequest(mk(big_ac), "b", iteration=1), app, cfg))
check("lifecycle: failed synthesis still returns (result, 1)", res4b is not None and n4b == 1, f"{res4b},{n4b}")
check("lifecycle: FAILED synthesis does NOT record domain_use", "solidity" not in _hr_calls["record"], str(_hr_calls["record"]))
check("lifecycle: FAILED synthesis does NOT run check_promote", "solidity" not in _hr_calls["promote"], str(_hr_calls["promote"]))
squad._run_synthesis = _orig_synthesis   # restore stub

# detect_domain_gap fail-safe: exception -> (False, None) so delegation continues normally.
# We test it at the function level (no need to hit LLM).
_gap_mode.update(gap=False, domain=None)   # reset for delegation tests below

# ============================ build() routing ============================
async def run_build(c, req): return await builder.build(req, app, c, audit=FakeAudit())
cfg.delegation_enabled = False
r = asyncio.run(run_build(cfg, BuildRequest(mk(big_ac), "b", iteration=1)))
check("route: disabled -> SOLO build", "SOLO build done" in r.summary, r.summary[:40])
cfg.delegation_enabled = True
r = asyncio.run(run_build(cfg, BuildRequest(mk(big_ac), "b", iteration=1)))
check("route: armed + big -> delegated", "Squad delegation" in r.summary)
r = asyncio.run(run_build(cfg, BuildRequest(mk([], summary="tiny tweak", desc="x"), "b", iteration=1)))
check("route: armed + tiny -> SOLO build", "SOLO build done" in r.summary)
_mode["raise_plan"] = True
r = asyncio.run(run_build(cfg, BuildRequest(mk(big_ac), "b", iteration=1)))
check("route: planner exception -> SOLO build (fail-safe)", "SOLO build done" in r.summary)
_mode["raise_plan"] = False

# route: synthesis result (n=1) -> builder returns it without falling back to solo.
cfg.delegation_enabled = True
squad._run_synthesis = fake_run_synthesis_real
_gap_mode.update(gap=True, domain="mql5")
r = asyncio.run(run_build(cfg, BuildRequest(mk(big_ac), "b", iteration=1)))
check("route: gap + synthesis result -> synthesis used (not solo)", "synthesis:mql5" in r.summary, r.summary[:60])
squad._run_synthesis = _orig_synthesis   # restore
_gap_mode.update(gap=False, domain=None)  # reset

# ============================ ephemeral specialist path ============================
# The stub SDK's _D() ignores all kwargs, so options.system_prompt is always "".
# Replace ClaudeAgentOptions in squad with a thin wrapper that stores kwargs as attributes,
# so _soldier()'s constructed system_prompt is actually retrievable from the options object.
class _TrackingOpts:
    """Minimal ClaudeAgentOptions stand-in that preserves keyword arguments as attributes."""
    def __init__(self, *a, **k):
        self.__dict__.update(k)
squad.ClaudeAgentOptions = _TrackingOpts

# Capture the system_prompt passed to run_agent so we can assert on it without a real model.
_captured_sysprompts: list[tuple[str, str]] = []  # [(tag, system_prompt)]

async def capturing_run_agent(prompt, options, tag="", ticket_id=None, pass_number=None):
    _captured_sysprompts.append((tag, getattr(options, "system_prompt", "")))
    if tag.startswith("soldier"):
        return AgentRun(text="done", final="implemented " + tag, cost_usd=0.20, num_turns=5,
                        is_error=False, tools=["Edit"])
    return AgentRun(text="solo", final="SOLO build done", cost_usd=0.5, num_turns=9,
                    is_error=False, tools=["Edit"])

squad.run_agent = capturing_run_agent

_spec = {
    "name": "MQL5 Algo Engineer",
    "lane_key": "mql5-algo",
    "identity": "owns the EA logic end-to-end",
    "knowledge": "MQL5 platform, MetaEditor, Strategy Tester conventions",
    "skills": ["1. Analyse signal requirements", "2. Implement OnTick / OnInit", "3. Self-check compile"],
    "constraints": "only .mq5 files; no other repo areas",
    "domain_gate": "echo gate-pass",
    "ephemeral": True,
}
_specialists_map = {"mql5-algo": _spec}
_st_ephemeral = squad.Subtask(role="mql5-algo", title="MQL5 Algo Engineer", detail="build an EA", size="M")
_req_eph = BuildRequest(mk(big_ac, summary="Build MQL5 EA"), "b", iteration=1)

_captured_sysprompts.clear()
asyncio.run(squad._soldier(_st_ephemeral, _req_eph, app, cfg, 1, 1, specialists=_specialists_map))
_sp = _captured_sysprompts[-1][1] if _captured_sysprompts else ""
check("ephemeral soldier: system prompt contains specialist name", "MQL5 Algo Engineer" in _sp, _sp[:80])
check("ephemeral soldier: system prompt contains identity", "owns the EA logic" in _sp, _sp[:80])
check("ephemeral soldier: system prompt contains charter section marker", "SPECIALIST CHARTER" in _sp, _sp[:80])
check("ephemeral soldier: house rules preserved", "HOUSE RULES" in _sp, _sp[:80])

# EU-69 iter-3 (non-overlap): with >1 ephemeral specialist sharing ONE worktree, each soldier's prompt
# must name the OTHER lanes so they stay off each other's files (the planner gets this for free by
# handing out non-overlapping slices; synthesized specialists each get the whole ticket, so we name the
# co-specialists explicitly). A single specialist gets NO co-specialist block.
_spec2 = {**_spec, "name": "MQL5 Backtester", "lane_key": "mql5-backtest"}
_two_specialists = {"mql5-algo": _spec, "mql5-backtest": _spec2}
_p_algo = squad._soldier_prompt(_st_ephemeral, _req_eph, 1, 2, specialists=_two_specialists)
check("multi-specialist prompt: names the co-specialist lane",
      "CO-SPECIALISTS" in _p_algo and "mql5-backtest" in _p_algo and "MQL5 Backtester" in _p_algo, _p_algo[:180])
_p_single = squad._soldier_prompt(_st_ephemeral, _req_eph, 1, 1, specialists=_specialists_map)
check("single-specialist prompt: no co-specialist block", "CO-SPECIALISTS" not in _p_single, _p_single[:120])
_p_fixed_prompt = squad._soldier_prompt(
    squad.Subtask(role="ordnance-be", title="x", detail="x", size="M"), _req_eph, 1, 2, specialists=None)
check("fixed-lane prompt: no co-specialist block (specialists=None)", "CO-SPECIALISTS" not in _p_fixed_prompt, _p_fixed_prompt[:120])

# Fixed SQUAD lane must be unchanged by the new specialists parameter.
_captured_sysprompts.clear()
_st_fixed = squad.Subtask(role="ordnance-be", title="add endpoint", detail="add endpoint", size="M")
asyncio.run(squad._soldier(_st_fixed, _req_eph, app, cfg, 1, 1, specialists=None))
_sp_fixed = _captured_sysprompts[-1][1] if _captured_sysprompts else ""
check("fixed lane: no charter section when specialists=None", "SPECIALIST CHARTER" not in _sp_fixed, _sp_fixed[:80])
check("fixed lane: SQUAD label still used", "Ordnance BE" in _sp_fixed, _sp_fixed[:80])

# specialists={} (empty) also falls through to the fixed path.
_captured_sysprompts.clear()
asyncio.run(squad._soldier(_st_fixed, _req_eph, app, cfg, 1, 1, specialists={}))
_sp_empty = _captured_sysprompts[-1][1] if _captured_sysprompts else ""
check("fixed lane: empty specialists dict -> SQUAD prompt", "Ordnance BE" in _sp_empty, _sp_empty[:80])

squad.run_agent = fake_run_agent  # restore

# ============================ _run_gate (EU-69 iter-3: explicit tri-state) ============================
# _run_gate returns ('pass'|'fail'|'manual', report). Only a REAL executed non-zero exit is 'fail';
# everything that couldn't run AS a verification (empty / prose / command-not-found / blocked / timeout)
# is 'manual' — those must NOT fail the build (an MQL5-style domain lands as a manual-QA item).
import tempfile as _tmp

with _tmp.TemporaryDirectory() as _tmpdir:
    _g_pass, _g_out = asyncio.run(squad._run_gate("echo gate-passed", _tmpdir))
    check("gate: passing command -> 'pass'", _g_pass == "pass", f"status={_g_pass}")
    check("gate: passing command report contains PASS", "PASS" in _g_out, _g_out[:80])

    _g_fail, _g_out2 = asyncio.run(squad._run_gate("exit 1", _tmpdir))
    check("gate: failing command (exit 1) -> 'fail'", _g_fail == "fail", f"status={_g_fail}")
    check("gate: failing command report contains FAIL", "FAIL" in _g_out2, _g_out2[:80])

    # A prose-only gate runs as a shell line whose first word isn't a command -> exit 127 -> 'manual'.
    _g_prose, _g_out6 = asyncio.run(squad._run_gate("eu69nosuchtool compile the expert advisor", _tmpdir))
    check("gate: prose / command-not-found (exit 127) -> 'manual'", _g_prose == "manual", f"status={_g_prose}")
    check("gate: prose gate report mentions manual QA", "manual QA" in _g_out6, _g_out6[:100])

_g_empty, _g_out3 = asyncio.run(squad._run_gate("", "/tmp"))
check("gate: empty string gate -> 'manual' + manual QA note", _g_empty == "manual" and "manual QA" in _g_out3, f"{_g_empty}:{_g_out3[:60]}")

_g_none, _g_out4 = asyncio.run(squad._run_gate(None, "/tmp"))
check("gate: None gate -> 'manual' + manual QA note", _g_none == "manual" and "manual QA" in _g_out4, f"{_g_none}:{_g_out4[:60]}")

# Dangerous command must be blocked by the guard (no subprocess spawned) -> 'manual', not 'fail'.
_g_danger, _g_out5 = asyncio.run(squad._run_gate("rm -rf /tmp/noexist", "/tmp"))
check("gate: dangerous command -> 'manual' + BLOCKED", _g_danger == "manual" and "BLOCKED" in _g_out5, f"{_g_danger}:{_g_out5[:80]}")

# ============================ _run_synthesis (ephemeral soldier + gate integration) ============================
import orchestrator.hr as _hr_mod

# EU-88: _run_synthesis now has a non-automode Telegram approval gate. The soldier-dispatch
# tests below are testing the DISPATCH path (charter → soldiers → gate runner), not the
# approval gate itself (which is covered by eu88_specialist_approval_test.py). Use
# auto_mode=True so the gate is bypassed and synthesis proceeds directly to dispatch.
import os as _os_syn, tempfile as _tmp_syn
_aud_syn_fd, _aud_syn_path = _tmp_syn.mkstemp(suffix=".jsonl"); _os_syn.close(_aud_syn_fd)
_cfg_syn = Config(apps=[app], audit_path=_aud_syn_path, use_worktree=False, auto_mode=True)

_CHARTER_SINGLE = [{
    "name": "MQL5 Algo Engineer",
    "lane_key": "mql5-algo",
    "identity": "owns the EA",
    "knowledge": "mql5 docs",
    "skills": ["write EA", "verify"],
    "constraints": "no other files",
    "domain_gate": "echo gate-pass",
    "ephemeral": True,
}]

_orig_synthesize = _hr_mod.synthesize_specialists
async def _fake_synthesize(domain, ticket_text, cfg2, approver=None):
    return list(_CHARTER_SINGLE)
_hr_mod.synthesize_specialists = _fake_synthesize

with _tmp.TemporaryDirectory() as _syn_dir:
    _app_syn = AppConfig(name="x", repo_path=_syn_dir, base_branch="DEV", protected_branch="MAIN",
                         backlog_backend="none")
    _syn_res = asyncio.run(squad._run_synthesis(
        "mql5", BuildRequest(mk(big_ac, summary="Build MQL5 EA"), "b", iteration=1), _app_syn, _cfg_syn))

check("synthesis: returns BuildResult (not None)", _syn_res is not None)
check("synthesis: ok=True when gate passes", getattr(_syn_res, "ok", None) is True)
check("synthesis: summary mentions ephemeral delegation", "Ephemeral-specialist delegation" in getattr(_syn_res, "summary", ""))
check("synthesis: summary mentions domain", "mql5" in getattr(_syn_res, "summary", "").lower())
check("synthesis: summary mentions gate PASS", "gate:PASS" in getattr(_syn_res, "summary", ""))
check("synthesis: cost aggregated (1 soldier x 0.20)", abs(getattr(_syn_res, "cost_usd", -1) - 0.20) < 1e-6, str(getattr(_syn_res, "cost_usd", "?")))

# No charters returned by HR -> None (solo fallback).
async def _fake_synthesize_empty(domain, tt, c2, approver=None):
    return []
_hr_mod.synthesize_specialists = _fake_synthesize_empty
_syn_none = asyncio.run(squad._run_synthesis("mql5", BuildRequest(mk(big_ac), "b", iteration=1), app, _cfg_syn))
check("synthesis: no charters -> None (solo fallback)", _syn_none is None)

# Failing gate -> ok=False in BuildResult.
_CHARTER_FAIL_GATE = [{**_CHARTER_SINGLE[0], "domain_gate": "exit 1"}]
async def _fake_synthesize_fail_gate(domain, tt, c2, approver=None):
    return list(_CHARTER_FAIL_GATE)
_hr_mod.synthesize_specialists = _fake_synthesize_fail_gate

with _tmp.TemporaryDirectory() as _fail_dir:
    _app_fail = AppConfig(name="x", repo_path=_fail_dir, base_branch="DEV", protected_branch="MAIN",
                          backlog_backend="none")
    _syn_fail = asyncio.run(squad._run_synthesis(
        "mql5", BuildRequest(mk(big_ac), "b", iteration=1), _app_fail, _cfg_syn))

check("synthesis: failing gate -> ok=False", getattr(_syn_fail, "ok", None) is False)
check("synthesis: failing gate -> FAIL in summary", "gate:FAIL" in getattr(_syn_fail, "summary", ""))

# Prose-only / command-not-found gate -> ok=True with a manual-QA note (EU-69 iter-3). This is the
# headline fix: an MQL5-style domain whose gate is a prose description (its first word isn't a runnable
# command, so the shell exits 127) LANDS as a manual-QA item instead of being mislabelled a builder
# failure and routed to Outcome.ERRORED at loop.py's `if not build.ok`.
_CHARTER_PROSE_GATE = [{**_CHARTER_SINGLE[0],
                        "domain_gate": "Manually open the EA in MetaTrader 5 Strategy Tester and confirm it loads"}]
async def _fake_synthesize_prose(domain, tt, c2, approver=None):
    return list(_CHARTER_PROSE_GATE)
_hr_mod.synthesize_specialists = _fake_synthesize_prose

with _tmp.TemporaryDirectory() as _prose_dir:
    _app_prose = AppConfig(name="x", repo_path=_prose_dir, base_branch="DEV", protected_branch="MAIN",
                           backlog_backend="none")
    _syn_prose = asyncio.run(squad._run_synthesis(
        "mql5", BuildRequest(mk(big_ac, summary="Build MQL5 EA"), "b", iteration=1), _app_prose, _cfg_syn))

check("synthesis: prose gate -> ok=True (lands, NOT ERRORED)",
      getattr(_syn_prose, "ok", None) is True, str(getattr(_syn_prose, "ok", None)))
check("synthesis: prose gate -> manual-QA note in summary",
      "manual qa" in getattr(_syn_prose, "summary", "").lower(), getattr(_syn_prose, "summary", "")[:140])
check("synthesis: prose gate -> gate:MANUAL marker in summary",
      "gate:MANUAL" in getattr(_syn_prose, "summary", ""))

_hr_mod.synthesize_specialists = _orig_synthesize  # restore

# ============================ report ============================
print("\n================ SQUAD DELEGATION QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if (detail and not ok) else ""))
print("-----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAILURE(S) ❌")
