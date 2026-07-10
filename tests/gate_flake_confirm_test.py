"""Gate flake honesty (2026-07-09): a red per-pass gate must REPRODUCE before it costs anything.

Live incident: 5 of 14 "max passes — PM escalated" strandings in 2 days were full-suite flakes
(EU-129/139/201/204/206 — four later merged with ZERO extra fix passes). A single flaky red burned
a builder pass, and the SAME flake twice tripped the gate_fingerprint_stuck breaker ("rebuild didn't
move it") → false escalation. The fix mirrors the base-gate confirmation re-run (gate.py): after a
red run_gate, re-run once; only a REPRODUCED red proceeds to fingerprint/stuck handling.

Pins:
  1. red → green re-run: ticket proceeds to review/land in the SAME pass; gate audited as passed;
     a gate_flake_confirmed_green event carries the first (flaky) report.
  2. red → red re-run: behaves exactly as before (fixes go back to the builder; second identical
     pair still trips the fingerprint-stuck breaker — real reds are not retried forever).
"""
import sys, types, asyncio, tempfile
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.loop as loop
from orchestrator import reviewer as reviewer_mod
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import (Ticket, BuildResult, ReviewResult, GateResult,
                                     Verdict, Outcome, TicketReport)

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

class Audit:
    def __init__(s): s.ev = []
    def record(s, e, **k): s.ev.append({"event": e, **k})
class Git:
    def has_changes(s): return True
    def diff_against_base(s): return "diff --git a/x b/x\n+change"
    def changed_paths(s): return ["apps/automatixy/x.ts"]

loop._notify = lambda c, t: None
loop.run_deterministic_checks = lambda app, paths, diff: GateResult(passed=True, report="")
loop._land = lambda *a, **k: TicketReport("AUTO-1", Outcome.MERGED, 1, 0.0, "automatixy", "b")

class FakeBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "sized")
    @staticmethod
    async def build(req, app, cfg, audit=None, **_):
        return BuildResult(ok=True, summary="did it", cost_usd=0.0, num_turns=1, raw="did it", tools=[])
loop.builder_mod = FakeBuilder

async def fake_review(diff, ticket, app, cfg, iteration=1, **_):
    return ReviewResult(verdict=Verdict.PASS, spec_met=True, cost_usd=0.0, summary="ok")
reviewer_mod.review = fake_review

def _run(gate_sequence):
    """Drive one _attempt with run_gate popping canned results; returns (report, audit, calls)."""
    seq = list(gate_sequence)
    calls = []
    def fake_gate(app, paths):
        calls.append(1)
        return seq.pop(0) if seq else GateResult(passed=True, report="")
    loop.run_gate = fake_gate
    tmp = Path(tempfile.mkdtemp())
    cfg = Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                                 protected_branch="MAIN", backlog_backend="none")],
                 audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
    app = cfg.app("automatixy")
    tk = Ticket(id="AUTO-1", key="AUTO-1", summary="s", description="d", ephemeral=True, app="automatixy")
    audit = Audit()
    report = asyncio.run(loop._attempt(tk, app, cfg, Git(), None, audit, loop.Budget(0), "autodev/AUTO-1"))
    return report, audit, calls

# ---- 1) flaky red: red once, green on confirmation → proceeds in the same pass ---- #
red = GateResult(passed=False, report="✗ FAILED: officer_rename_regression_test.py (flake)")
green = GateResult(passed=True, report="")
report, audit, calls = _run([red, green])
ev = [e["event"] for e in audit.ev]
chk("flaky red proceeds to MERGED in the same pass (no builder pass burned)",
    report.outcome == Outcome.MERGED, report.outcome)
chk("confirmation re-run actually happened (2 gate calls)", len(calls) == 2, calls)
chk("gate_flake_confirmed_green audited with the first report",
    any(e["event"] == "gate_flake_confirmed_green" and "officer_rename" in e.get("first_report", "")
        for e in audit.ev), ev)
chk("the gate event is audited as PASSED (the confirmed truth)",
    any(e["event"] == "gate" and e.get("passed") for e in audit.ev), ev)

# ---- 2) reproduced red: red, red → fixes go back to the builder exactly as before ---- #
red2 = GateResult(passed=False, report="✗ FAILED: real_regression_test.py")
report, audit, calls = _run([red2, red2, red2, red2])   # every gate red, reproduced
ev = [e["event"] for e in audit.ev]
chk("reproduced red does NOT merge", report.outcome != Outcome.MERGED, report.outcome)
chk("reproduced red is audited as a failed gate",
    any(e["event"] == "gate" and not e.get("passed") for e in audit.ev), ev)
chk("identical reproduced reds still trip the fingerprint-stuck breaker (real reds don't loop forever)",
    any(e["event"] == "gate_fingerprint_stuck" for e in audit.ev), ev)

print("\n========== GATE FLAKE CONFIRMATION QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
