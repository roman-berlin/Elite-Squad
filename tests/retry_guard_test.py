"""Retry-guard QA: when the reviewer returns the SAME blocking feedback a second time, the build isn't
making progress on it — so the loop STOPS burning identical passes and escalates, instead of grinding to
max_iterations. Saves the wasted passes that drove yesterday's 30-retry burn."""
import sys, types, asyncio, tempfile
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.loop as loop
from orchestrator import reviewer as reviewer_mod
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import (Ticket, BuildResult, ReviewResult, GateResult,
                                     TestEngineerResult, Verdict, Outcome, TicketReport)

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# ---- unit: _changes_sig is order/case/whitespace-independent, so a true repeat is detected ----
chk("_changes_sig equal for the same changes reordered + recased + respaced",
    loop._changes_sig(["Add tenant filter", "Fix  the   NULL check"])
    == loop._changes_sig(["fix the null check", "ADD TENANT FILTER"]))
chk("_changes_sig differs when the feedback actually changes",
    loop._changes_sig(["Add tenant filter"]) != loop._changes_sig(["Add a unit test"]))
chk("_changes_sig empty for no changes", loop._changes_sig([]) == "" and loop._changes_sig([" "]) == "")

# ---- loop: identical rejection twice -> stop at pass 2, escalate (don't grind to max_iterations) ----
class Audit:
    def __init__(s): s.ev = []
    def record(s, e, **k): s.ev.append({"event": e, **k})
class Git:
    def has_changes(s): return True
    def diff_against_base(s): return "diff --git a/x b/x\n+change"
    def changed_paths(s): return ["apps/automatixy/x.ts"]

loop._notify = lambda c, t: None
loop.run_gate = lambda app, changed_paths=None: GateResult(passed=True, report="")
loop._land = lambda *a, **k: TicketReport("AUTO-14", Outcome.MERGED, 1, 0.0, "automatixy", "b")
async def fake_te(ticket, app, cfg):
    return TestEngineerResult(ok=True, coverage="lines 80%→85%")
loop.test_engineer_mod.ensure_coverage = fake_te

built = []
class FakeBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "sized")
    @staticmethod
    async def build(req, app, cfg, audit=None):
        built.append(req.iteration)
        return BuildResult(ok=True, summary="did it", cost_usd=0.0, num_turns=1, raw="did it", tools=[])
loop.builder_mod = FakeBuilder

SAME = ["Add an explicit tenant_id filter to the query"]
review_calls = []
async def fake_review(diff, ticket, app, cfg, iteration=1):   # EU-52: review() now takes the build iteration
    review_calls.append(1)
    return ReviewResult(verdict=Verdict.FAIL, spec_met=False, required_changes=list(SAME), cost_usd=0.0)
reviewer_mod.review = fake_review

tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False, max_iterations=4)
app = cfg.app("automatixy")
ticket = Ticket(id="AUTO-14", key="AUTO-14", summary="s", description="d", ephemeral=True, app="automatixy")

au = Audit()
rep = asyncio.run(loop._attempt(ticket, app, cfg, Git(), None, au, loop.Budget(0), "autodev/AUTO-14"))

chk("stopped after the 2nd identical rejection — built twice, NOT 4× (saved 2 passes)",
    len(built) == 2, f"builds={built}")
chk("the repeat was detected and audited (retry_stuck)", any(e["event"] == "retry_stuck" for e in au.ev))
chk("a stuck ticket escalates to you, not a silent grind to max_iterations",
    rep.outcome == Outcome.ESCALATED, str(rep.outcome))
chk("only escalated once it was actually stuck (2 reviews, not 1)", len(review_calls) == 2, f"reviews={review_calls}")

print("\n============ RETRY-GUARD QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
