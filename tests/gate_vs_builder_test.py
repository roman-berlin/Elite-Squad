"""EU-442 — gate-vs-Builder self-report cross-verification.

EU-151 showed a Builder reporting "all tests pass" while the gate recorded ERRORED, and nothing
caught the contradiction — it burned max-effort passes until a human triaged it. The fix adds a
deterministic cross-check at the one point where BOTH the authoritative gate verdict and the
Builder's self-report are fresh: right after the feature-branch gate runs, before review/continue.

Pins:
  AC 1. Builder claims GREEN ("all tests pass") while the gate is RED → mismatch reason naming both.
  AC 2. Builder admits RED ("tests still failing") while the gate is GREEN → mismatch reason.
  AC 3. Both agree GREEN → None (no false block).
  AC 4. A neutral summary (no explicit test-outcome claim) on a RED gate → None (no false positive).
  AC 5. In a loop pass where the gate is RED and build.summary claims "all tests pass", a
       gate_builder_verdict_mismatch audit event is recorded and the loop ESCALATES instead of
       retrying the builder.
"""
import sys, types, asyncio, tempfile
from pathlib import Path

# Stub the Agent SDK the way every other harness does (the orchestrator imports it transitively).
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator.gate import gate_vs_builder_verdict
import orchestrator.loop as loop
from orchestrator import reviewer as reviewer_mod
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import (Ticket, BuildResult, ReviewResult, GateResult,
                                     Verdict, Outcome, TicketReport)

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --------------------------------------------------------------------------- #
# AC 1–4: the pure cross-verification function.
# --------------------------------------------------------------------------- #

# AC 1: Builder claims GREEN, gate RED (the EU-151 hallucination class).
r1 = gate_vs_builder_verdict("All tests pass.", gate_passed=False)
chk("AC1 green-claim vs red-gate returns a mismatch reason",
    r1 is not None, repr(r1))
chk("AC1 reason names the Builder's GREEN claim",
    r1 and "green" in r1.lower(), repr(r1))
chk("AC1 reason names the gate's RED verdict",
    r1 and "red" in r1.lower(), repr(r1))

# AC 2: Builder admits RED, gate GREEN (wrong-tests / didn't-run-the-suite class).
r2 = gate_vs_builder_verdict("Tests still failing in the auth suite.", gate_passed=True)
chk("AC2 red-admission vs green-gate returns a mismatch reason",
    r2 is not None, repr(r2))
chk("AC2 reason names the Builder's RED admission",
    r2 and "red" in r2.lower(), repr(r2))

# AC 3: both agree GREEN → no mismatch, no false block.
r3 = gate_vs_builder_verdict("All tests pass.", gate_passed=True)
chk("AC3 green-claim vs green-gate returns None (agreement)",
    r3 is None, repr(r3))

# AC 4: a neutral summary on a RED gate → None (no explicit claim → nothing to contradict).
r4 = gate_vs_builder_verdict("Implemented the endpoint and added a fixture.", gate_passed=False)
chk("AC4 neutral summary on a red gate returns None (no false positive)",
    r4 is None, repr(r4))

# Extra guards (EU-249 iter-2 false-positive class):
chk("a negated green claim ('not all tests pass') is NOT a green claim",
    gate_vs_builder_verdict("Not all tests pass yet — two still red.", gate_passed=False) is None
    or "red" in (gate_vs_builder_verdict("Not all tests pass yet — two still red.", gate_passed=False) or "").lower(),
    "negation must not read as a green claim that contradicts a red gate")
chk("an already-resolved failure ('fixed the failing test, all green') is not a mismatch on a green gate",
    gate_vs_builder_verdict("Fixed the failing test; all green now.", gate_passed=True) is None,
    "resolved-context wording must not read as an unresolved red admission")

# --------------------------------------------------------------------------- #
# AC 5: loop integration — gate RED + Builder "all tests pass" → escalate, no retry.
# --------------------------------------------------------------------------- #

class Audit:
    def __init__(s): s.ev = []
    def record(s, e, **k): s.ev.append({"event": e, **k})
class Git:
    def has_changes(s): return True
    def diff_against_base(s): return "diff --git a/x b/x\n+change"
    def changed_paths(s): return ["apps/automatixy/x.ts"]

loop._notify = lambda c, t: None
loop.run_deterministic_checks = lambda app, paths, diff: GateResult(passed=True, report="")

builder_calls = [0]
class LyingBuilder:
    """A Builder whose summary claims GREEN while the gate is RED — the EU-151 shape."""
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "sized")
    @staticmethod
    async def build(req, app, cfg, audit=None, **_):
        builder_calls[0] += 1
        return BuildResult(ok=True, summary="All tests pass.", cost_usd=0.0,
                           num_turns=1, raw="All tests pass.", tools=[])
loop.builder_mod = LyingBuilder

async def fake_review(diff, ticket, app, cfg, iteration=1, **_):
    return ReviewResult(verdict=Verdict.PASS, spec_met=True, cost_usd=0.0, summary="ok")
reviewer_mod.review = fake_review


def _run_lying(gate_result):
    """Drive one _attempt where the gate is the given (red) result every call, and the Builder
    claims 'All tests pass.' Returns (report, audit)."""
    def fake_gate(app, paths):
        return gate_result
    loop.run_gate = fake_gate
    tmp = Path(tempfile.mkdtemp())
    cfg = Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                                 protected_branch="MAIN", backlog_backend="none")],
                 audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
    app = cfg.app("automatixy")
    tk = Ticket(id="AUTO-1", key="AUTO-1", summary="s", description="d", ephemeral=True, app="automatixy")
    audit = Audit()
    report = asyncio.run(loop._attempt(tk, app, cfg, Git(), None, audit, loop.Budget(0), "autodev/AUTO-1"))
    return report, audit


builder_calls[0] = 0
red = GateResult(passed=False, report="✗ FAILED: real_regression_test.py (exit 1)")
report, audit = _run_lying(red)
ev = [e["event"] for e in audit.ev]
chk("AC5 a gate_builder_verdict_mismatch event is recorded",
    any(e["event"] == "gate_builder_verdict_mismatch" for e in audit.ev), ev)
chk("AC5 the loop ESCALATES (does not silently retry to max-effort)",
    report.outcome == Outcome.ESCALATED, report.outcome)
chk("AC5 the Builder is NOT retried (the contradiction breaks the pass loop on pass 1)",
    builder_calls[0] == 1, f"builder calls={builder_calls[0]}")

# --------------------------------------------------------------------------- #
# EU-519 — Quoted / example context must NOT trigger GREEN mismatch.
# The mirror-image of EU-481's RED false-positive class.
# --------------------------------------------------------------------------- #

# EU-519 AC1: inline backtick-quoted green phrase → no mismatch (returns None).
r_q1 = gate_vs_builder_verdict(
    "The fixture asserts the log line `all tests pass` is ignored.", gate_passed=False)
chk("EU-519 quoted inline-backtick 'all tests pass' vs red-gate → None",
    r_q1 is None, repr(r_q1))

# EU-519 AC1b: blockquote-quoted green phrase → no mismatch.
r_q2 = gate_vs_builder_verdict(
    "Example transcript:\n> all tests pass\nhandled as fixture input.", gate_passed=False)
chk("EU-519 blockquoted 'all tests pass' vs red-gate → None",
    r_q2 is None, repr(r_q2))

# EU-519 AC1c: fenced code-block containing green phrase → no mismatch.
r_q3 = gate_vs_builder_verdict(
    "Here is an example:\n```console\nall tests pass\n```\nThat was the old behavior.",
    gate_passed=False)
chk("EU-519 fenced-block 'all tests pass' vs red-gate → None",
    r_q3 is None, repr(r_q3))

# EU-519 AC2: genuine unquoted GREEN claim still fires (EU-151 protection preserved).
r_g1 = gate_vs_builder_verdict("All tests pass now.", gate_passed=False)
chk("EU-519 genuine unquoted 'All tests pass now.' vs red-gate still fires",
    r_g1 is not None, repr(r_g1))

# EU-519 AC3: genuine claim OUTSIDE a quote fires even when a quote also exists.
r_m1 = gate_vs_builder_verdict(
    "All tests pass. Log excerpt: `2 tests failed` was the pre-fix state.",
    gate_passed=False)
chk("EU-519 genuine claim plus quoted phrase → fires on the genuine claim",
    r_m1 is not None, repr(r_m1))
chk("EU-519 mixed claim identifies the real GREEN line",
    r_m1 and "pass" in r_m1.lower(), repr(r_m1))

# --------------------------------------------------------------------------- #
# EU-520 — Quoted / example context must NOT trigger RED admission mismatch.
# The mirror-image of EU-519: when a Builder summary quotes 'tests still failing'
# inside backticks/fences/blockquotes with a GREEN gate, the _red_test_admission()
# scanner must ignore those fixtures.  Genuine first-person RED admissions must
# still fire (the existing AC2 pin covers the bare case; this adds mixed-context).
# --------------------------------------------------------------------------- #

# EU-520 AC1: inline-backtick quoted red phrase → no mismatch (returns None).
r_eu1 = gate_vs_builder_verdict(
    "The parser must ignore the phrase `the test suite is still red` when it appears quoted.",
    gate_passed=True)
chk("EU-520 AC1 inline-backtick quoted 'test suite is still red' vs green-gate → None",
    r_eu1 is None, repr(r_eu1))

# EU-520 AC1b: fenced code-block containing red-admission phrase → no mismatch.
r_eu2 = gate_vs_builder_verdict(
    "Here is an example:\n```\ntests still failing\n```\nThat was the old behavior.",
    gate_passed=True)
chk("EU-520 AC1b fenced-block 'tests still failing' vs green-gate → None",
    r_eu2 is None, repr(r_eu2))

# EU-520 AC1c: blockquote-quoted red-admission phrase → no mismatch.
r_eu3 = gate_vs_builder_verdict(
    "Example:\n> tests still failing\nhandled as fixture.",
    gate_passed=True)
chk("EU-520 AC1c blockquoted 'tests still failing' vs green-gate → None",
    r_eu3 is None, repr(r_eu3))

# EU-520 AC2: genuine unquoted RED admission still fires (existing AC2 pin — must stay green).
r_eu4 = gate_vs_builder_verdict("Tests still failing in the auth suite.", gate_passed=True)
chk("EU-520 AC2 genuine unquoted red admission vs green-gate still fires",
    r_eu4 is not None, repr(r_eu4))
chk("EU-520 AC2 reason names the Builder's RED admission",
    r_eu4 and "red" in r_eu4.lower(), repr(r_eu4))

# EU-520 AC3: genuine admission alongside a quoted phrase → still fires (strip must not swallow real).
r_eu5 = gate_vs_builder_verdict(
    "Tests still failing. The old log said `all tests pass`.",
    gate_passed=True)
chk("EU-520 AC3 genuine admission plus quoted phrase → fires on the real admission",
    r_eu5 is not None, repr(r_eu5))
chk("EU-520 AC3 mismatch identifies the genuine RED line",
    r_eu5 and "fail" in r_eu5.lower(), repr(r_eu5))

# --------------------------------------------------------------------------- #
print("\n========== GATE VS BUILDER VERDICT QA (EU-442) ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
