"""EU-216: weak (non-Opus, e.g. GLM) backends get one extra review-fix pass.

2026-07-09 forensics: 3 of 14 "max passes" escalations were real GLM-builder shortfalls one small
fix away (AUTO-59 was literally one fix short). HARD_MAX_PASSES=2 is calibrated for Opus; GLM
(glm-4.6) needs more review-fix iterations. This pins:

  1. GLM + a pass-2 review FAIL with only minor/major severity findings (no "blocker") and a NEW
     reject fingerprint -> a 3rd build+review pass runs (effective max_passes == 3), and a single
     `weak_extra_pass` audit event is recorded.
  2. Opus + the identical minor-only pass-2 FAIL -> stops at pass 2 (no 3rd build), escalates via
     the existing max-passes path, and NO `weak_extra_pass` event fires. Opus's effective
     max_passes stays 2.
  3. GLM + a pass-2 FAIL that carries a blocker-severity finding -> stops at pass 2 (no 3rd build)
     and escalates, even though the backend is weak.
  4. GLM + a pass-2 FAIL whose required-changes fingerprint matches the pass-1 reject signature ->
     the existing retry-stuck guard trips FIRST and breaks at pass 2 (no 3rd build), even though
     there is no blocker.

Import / stub pattern matches retry_guard_test.py / eu215_advisory_ship_test.py — no network, no
real models; `_land`/`run_gate`/the builder/reviewer are all stubbed.
"""
import sys
import types
import asyncio
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Stub the Agent SDK so importing the orchestrator never reaches the network.
# ---------------------------------------------------------------------------
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        pass

    def __call__(s, *a, **k):
        return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.loop as loop
from orchestrator import backends
from orchestrator import reviewer as reviewer_mod
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import (
    BuildResult, GateResult, Outcome, QualityIssue, ReviewResult, Ticket, TicketReport, Verdict,
)

results = []


def chk(name, cond, detail=""):
    results.append((name, bool(cond), detail))


# ---------------------------------------------------------------------------
# Sanity: backends.normalize distinguishes GLM from Opus/native the way loop.py relies on.
# ---------------------------------------------------------------------------
chk("sanity: backends.normalize('glm') != backends.NATIVE (weak backend)",
    backends.normalize("glm") != backends.NATIVE)
chk("sanity: backends.normalize('opus') == backends.NATIVE (not weak)",
    backends.normalize("opus") == backends.NATIVE)
chk("sanity: HARD_MAX_PASSES_WEAK is 3", loop.HARD_MAX_PASSES_WEAK == 3)
chk("sanity: HARD_MAX_PASSES is unchanged at 2", loop.HARD_MAX_PASSES == 2)


# ---------------------------------------------------------------------------
# Shared stubs
# ---------------------------------------------------------------------------
class Audit:
    def __init__(s):
        s.ev = []

    def record(s, event, **kw):
        s.ev.append({"event": event, **kw})

    @staticmethod
    def diff_hash(*a):
        return "aabbcc"


class Git:
    def has_changes(s):
        return True

    def diff_against_base(s):
        return "diff --git a/x b/x\n+change"

    def changed_paths(s):
        return ["orchestrator/loop.py"]


loop._notify = lambda cfg, text: None
loop.run_gate = lambda app, changed_paths=None, **_: GateResult(passed=True, report="")
loop._land = lambda *a, **k: TicketReport("EU-216", Outcome.MERGED, 1, 0.0, "Elite-Unit", "b")


def _make_cfg(model_backend: str) -> tuple[Config, "AppConfig"]:
    tmp = Path(tempfile.mkdtemp())
    cfg = Config(
        apps=[AppConfig(name="Elite-Unit", repo_path=".", base_branch="dev",
                        protected_branch="main", backlog_backend="none")],
        audit_path=str(tmp / "audit.jsonl"),
        dry_run=True,
        use_worktree=False,
        pm_enabled=False,       # keep the exhaustion path's mock surface clean
        max_iterations=2,
        max_iterations_weak=3,
        model_backend=model_backend,
    )
    return cfg, cfg.app("Elite-Unit")


def _make_builder(built: list):
    class FakeBuilder:
        @staticmethod
        def effort_plan(cfg, it, ticket):
            return ("low", "sized")

        @staticmethod
        async def build(req, app, cfg, audit=None, **_):
            built.append(req.iteration)
            return BuildResult(ok=True, summary="did it", cost_usd=0.0, num_turns=1, raw="did it",
                               tools=[])
    return FakeBuilder


def _run(ticket_id: str, model_backend: str, reviews: list[ReviewResult]) -> tuple[TicketReport, Audit, list]:
    """Drive one _attempt() call; `reviews[i]` is returned on review call i (1-indexed pass)."""
    cfg, app = _make_cfg(model_backend)
    built: list = []
    loop.builder_mod = _make_builder(built)

    calls = {"n": 0}

    async def fake_review(diff, ticket, app, cfg, iteration=1, **_):
        i = calls["n"]
        calls["n"] += 1
        return reviews[min(i, len(reviews) - 1)]

    reviewer_mod.review = fake_review

    audit = Audit()
    ticket = Ticket(id=ticket_id, key=ticket_id, summary="s", description="d", ephemeral=True,
                    app="Elite-Unit")
    report = asyncio.run(
        loop._attempt(ticket, app, cfg, Git(), None, audit, loop.Budget(0), f"autodev/{ticket_id}")
    )
    return report, audit, built


# ===========================================================================
# Test 1 — GLM, pass-2 minor-only FAIL, distinct fingerprints -> pass 3 runs.
# ===========================================================================
review1_pass1 = ReviewResult(verdict=Verdict.FAIL, spec_met=False,
                             required_changes=["Fix the null check"],
                             quality_issues=[QualityIssue(severity="minor", area="style",
                                                          detail="minor nit")])
review1_pass2 = ReviewResult(verdict=Verdict.FAIL, spec_met=False,
                             required_changes=["Add a regression test"],
                             quality_issues=[QualityIssue(severity="major", area="tests",
                                                          detail="missing regression test")])
review1_pass3 = ReviewResult(verdict=Verdict.FAIL, spec_met=False,
                             required_changes=["Annotate the return type"],
                             quality_issues=[QualityIssue(severity="minor", area="types",
                                                          detail="loose return type")])

report1, audit1, built1 = _run("EU-216a", "glm", [review1_pass1, review1_pass2, review1_pass3])

chk("GLM minor-only FAIL: build runs a 3rd pass (iteration 3)", built1 == [1, 2, 3], built1)
chk("GLM minor-only FAIL: exactly one weak_extra_pass audit event",
    len([e for e in audit1.ev if e["event"] == "weak_extra_pass"]) == 1, audit1.ev)
if any(e["event"] == "weak_extra_pass" for e in audit1.ev):
    ev = next(e for e in audit1.ev if e["event"] == "weak_extra_pass")
    chk("GLM minor-only FAIL: weak_extra_pass grants iteration 3", ev.get("iteration") == 3, ev)
    chk("GLM minor-only FAIL: weak_extra_pass records the glm backend", ev.get("backend") == "glm", ev)


# ===========================================================================
# Test 2 — Opus, the identical minor-only pass-2 FAIL -> stops at pass 2.
# ===========================================================================
report2, audit2, built2 = _run("EU-216b", "opus", [review1_pass1, review1_pass2, review1_pass3])

chk("Opus minor-only FAIL: builds stop at 2 (no 3rd pass)", built2 == [1, 2], built2)
chk("Opus minor-only FAIL: no weak_extra_pass audit event",
    not any(e["event"] == "weak_extra_pass" for e in audit2.ev), audit2.ev)
chk("Opus minor-only FAIL: escalates via the max-passes path",
    any(e["event"] == "needs_human" and "max passes" in (e.get("reason") or "") for e in audit2.ev),
    audit2.ev)
chk("Opus minor-only FAIL: outcome is ESCALATED", report2.outcome == Outcome.ESCALATED, report2.outcome)


# ===========================================================================
# Test 3 — GLM, pass-2 FAIL WITH a blocker -> stops at pass 2 regardless of backend.
# ===========================================================================
review3_pass2_blocker = ReviewResult(
    verdict=Verdict.FAIL, spec_met=False,
    required_changes=["Fix the SQL injection"],
    quality_issues=[QualityIssue(severity="blocker", area="security",
                                 detail="SQL built via string concat")],
)

report3, audit3, built3 = _run("EU-216c", "glm", [review1_pass1, review3_pass2_blocker])

chk("GLM blocker FAIL: builds stop at 2 (no 3rd pass)", built3 == [1, 2], built3)
chk("GLM blocker FAIL: no weak_extra_pass audit event",
    not any(e["event"] == "weak_extra_pass" for e in audit3.ev), audit3.ev)
chk("GLM blocker FAIL: escalates via the max-passes path", report3.outcome == Outcome.ESCALATED,
    report3.outcome)


# ===========================================================================
# Test 4 — GLM, pass-2 FAIL repeats the pass-1 fingerprint -> retry-stuck guard
#           trips FIRST and breaks at pass 2, even with no blocker.
# ===========================================================================
SAME = ["Add an explicit tenant_id filter to the query"]
review4_same = ReviewResult(verdict=Verdict.FAIL, spec_met=False, required_changes=list(SAME))

report4, audit4, built4 = _run("EU-216d", "glm", [review4_same, review4_same])

chk("GLM stuck fingerprint: builds stop at 2 (no 3rd pass)", built4 == [1, 2], built4)
chk("GLM stuck fingerprint: retry_stuck fired", any(e["event"] == "retry_stuck" for e in audit4.ev),
    audit4.ev)
chk("GLM stuck fingerprint: no weak_extra_pass audit event",
    not any(e["event"] == "weak_extra_pass" for e in audit4.ev), audit4.ev)
chk("GLM stuck fingerprint: outcome is ESCALATED", report4.outcome == Outcome.ESCALATED,
    report4.outcome)


# ===========================================================================
# Results
# ===========================================================================
passed = sum(1 for _, ok, _ in results if ok)
print("\n========== EU-216 WEAK-BACKEND 3RD PASS QA ==========")
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))
print("------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
