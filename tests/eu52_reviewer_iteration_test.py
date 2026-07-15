"""EU-52 regression: the economical model ladder must actually REACH the reviewer and the
non-Builder officers — the two paths the ticket proved were inert.

Fix (2) — the Reviewer never escalated. `reviewer.review` called `models.for_reviewer(cfg, diff)`
with no iteration, so it always defaulted to 1 and the iteration>1 escalation branch was dead code.
We pin it at two levels:
  • UNIT: reviewer.review forwards its `iteration` arg straight into models.for_reviewer (and the
    model the ladder returns is the model the agent actually runs on).
  • INTEGRATION: the build loop threads the REAL, escalating build iteration (1,2,3…) into each
    re-review — not a constant 1. On the old code every call was iteration=1.

Fix (3) — most officer calls bypassed the ladder by hardcoding cfg.reviewer_model / cfg.builder_model
(Opus-always). We pin (by source) that every officer the ticket named now routes through
models.for_officer and carries no bare `model=cfg.*_model` pin, so it can't silently regress.
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
from orchestrator import reviewer as reviewer_mod, models
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import (Ticket, BuildResult, ReviewResult, GateResult,
                                     Verdict, Outcome, TicketReport)

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ============ UNIT: reviewer.review forwards `iteration` into the ladder ============ #
class RR:
    def __init__(s, t): s.final, s.text, s.is_error, s.cost_usd, s.num_turns, s.tools, s.provider, s.model_version = t, t, False, 0.0, 1, [], "Anthropic", "claude-sonnet-4-6"

seen = {}
def fake_for_reviewer(cfg, diff="", iteration=1):
    seen["iteration"] = iteration
    return models.SONNET, f"sonnet (it={iteration})"
ran = {}
async def fake_run_agent(prompt, options, tag="", cfg=None, routing_tier=None):
    ran["model"] = getattr(options, "model", None)
    return RR('{"verdict": "PASS", "spec_met": true}')
reviewer_mod.run_agent = fake_run_agent
# EU-108/EU-174: the reviewer now routes through run_agent_with_fallback — stub it too.
reviewer_mod.run_agent_with_fallback = fake_run_agent
models.for_reviewer = fake_for_reviewer   # captured by reviewer.review's `from . import models`

d = Path(tempfile.mkdtemp())
ucfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV",
                              protected_branch="MAIN", backlog_backend="none")],
              audit_path=str(d / "audit.jsonl"), use_worktree=False, auto_model=True)
uapp = ucfg.app("automatixy"); uapp.workdir = str(d)
utk = Ticket(id="AUTO-9", key="AUTO-9", summary="s", description="x", acceptance_criteria=["a"])

seen.clear(); ran.clear()
asyncio.run(reviewer_mod.review("diff", utk, uapp, ucfg, iteration=3))
chk("reviewer.review forwards iteration=3 to models.for_reviewer (was hardcoded to default 1)",
    seen.get("iteration") == 3, seen.get("iteration"))
chk("the ladder-chosen model is the one the reviewer agent actually runs on",
    ran.get("model") == models.SONNET, ran.get("model"))

seen.clear()
asyncio.run(reviewer_mod.review("diff", utk, uapp, ucfg))   # omitted -> back-compat default
chk("reviewer.review still defaults iteration to 1 when omitted (CLI/direct callers unaffected)",
    seen.get("iteration") == 1, seen.get("iteration"))


# ============ INTEGRATION: the loop threads the REAL escalating build iteration ============ #
# Each review returns DIFFERENT required changes so the retry-stuck guard never fires; the loop
# rebuilds up to the effective pass cap, and each re-review must receive the incrementing build
# iteration. QW3 (2026-07-05): loop.HARD_MAX_PASSES clamps the cap to 2 — max_iterations=3 below
# deliberately exercises the clamp (3 requested, only 2 passes run).
class Audit:
    def __init__(s): s.ev = []
    def record(s, e, **k): s.ev.append({"event": e, **k})
class Git:
    def has_changes(s): return True
    def diff_against_base(s): return "diff --git a/x b/x\n+change"
    def changed_paths(s): return ["apps/automatixy/x.ts"]

loop._notify = lambda c, t: None
loop.run_gate = lambda app, changed_paths=None, **_: GateResult(passed=True, report="")
loop._land = lambda *a, **k: TicketReport("AUTO-52", Outcome.MERGED, 1, 0.0, "automatixy", "b")

built = []
class FakeBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "sized")
    @staticmethod
    async def build(req, app, cfg, audit=None, **_):   # EU-72: absorb store=/spec= kwargs
        built.append(req.iteration)
        return BuildResult(ok=True, summary="did it", cost_usd=0.0, num_turns=1, raw="did it", tools=[])
loop.builder_mod = FakeBuilder

review_iters = []
async def fake_review(diff, ticket, app, cfg, iteration=1, **_):   # EU-72: absorb store=/build_artifact= kwargs
    review_iters.append(iteration)
    # unique feedback each pass -> not "stuck", so the loop keeps rebuilding to max_iterations
    return ReviewResult(verdict=Verdict.FAIL, spec_met=False,
                        required_changes=[f"Fix distinct issue #{iteration}"], cost_usd=0.0)
reviewer_mod.review = fake_review

tmp = Path(tempfile.mkdtemp())
icfg = Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                              protected_branch="MAIN", backlog_backend="none")],
              audit_path=str(tmp / "audit.jsonl"), use_worktree=False, max_iterations=3)
iapp = icfg.app("automatixy")
itk = Ticket(id="AUTO-52", key="AUTO-52", summary="s", description="d", ephemeral=True, app="automatixy")
asyncio.run(loop._attempt(itk, iapp, icfg, Git(), None, Audit(), loop.Budget(0), "autodev/AUTO-52"))

chk("loop threads the REAL escalating build iteration into each re-review (1,2 — not constant 1) "
    "and HARD_MAX_PASSES clamps max_iterations=3 to 2 passes (QW3)",
    review_iters == [1, 2], f"iters={review_iters}")
chk("review iteration tracks the build iteration one-for-one",
    review_iters == built, f"reviews={review_iters} builds={built}")


# ============ SOURCE: every officer the ticket named routes through the ladder (fix #3) ============ #
# pm/scrum/drillmaster/quartermaster were the calls the ticket flagged as still hardcoding Opus.
# Scout/provost are pinned by officer_ladder_test; these complete it. (squad's build-delegation
# officers, and later its read-only domain classifier, were removed in the Phase-2 collapse —
# squad has no ladder officer left. adjutant.py itself was deleted in EU-325.)
import re
_pin = re.compile(r"[^_]model=cfg\.(reviewer_model|builder_model)")   # the bare hardcode pattern
for _mod in ("pm", "scrum", "drillmaster", "quartermaster"):
    _src = (Path("orchestrator") / f"{_mod}.py").read_text(encoding="utf-8")
    chk(f"{_mod}: routes through the economical ladder (models.for_officer)",
        "models.for_officer(" in _src)
    chk(f"{_mod}: no bare cfg.reviewer_model/builder_model agent pin left (Opus-always removed)",
        not _pin.search(_src), _mod)

print("\n========== EU-52 REVIEWER-ITERATION + OFFICER-LADDER QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
