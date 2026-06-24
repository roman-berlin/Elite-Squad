"""EU-11: an unparseable reviewer verdict triggers ONE re-review, not a rebuild."""
import sys, types, asyncio
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

# ---- unit: _parse marks parse_failed on unparseable output, clears it on good JSON ----
bad = reviewer_mod._parse("the model rambled and emitted no json block at all")
chk("_parse flags parse_failed on unparseable output", bad.parse_failed and bad.verdict == Verdict.FAIL)
good = reviewer_mod._parse('```json\n{"verdict":"PASS","spec_conformance":{"met":true,"gaps":[]},'
                           '"quality":{"issues":[]},"required_changes":[],"summary":"ok"}\n```')
chk("_parse leaves parse_failed False on valid JSON", good.parse_failed is False and good.verdict == Verdict.PASS)

# ---- loop: forced parse-failure -> one re-review, no rebuild ----
class Audit:
    def __init__(s): s.ev = []
    def record(s, e, **k): s.ev.append({"event": e, **k})
class Git:
    def has_changes(s): return True
    def diff_against_base(s): return "diff --git a/x b/x\n+change"
    def changed_paths(s): return ["apps/automatixy/x.ts"]   # EU-19: gate detects touched apps from the diff

loop._notify = lambda c, t: None
loop.run_gate = lambda app, changed_paths=None: GateResult(passed=True, report="")  # EU-19: gate now takes the diff's changed paths
# Don't exercise real merge/git plumbing — certify the decision routed to land.
loop._land = lambda *a, **k: TicketReport("EU-11", Outcome.MERGED, 1, 0.0, "automatixy", "b")
# EU-37: the Test Engineer coverage stage runs between gate and review — stub it out here so this
# test stays focused on the review-retry path (it has its own dedicated harness).
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

review_calls = []
async def fake_review(diff, ticket, app, cfg):
    review_calls.append(1)
    if len(review_calls) == 1:
        # first pass: unparseable -> fail-safe FAIL with parse_failed set
        return ReviewResult(verdict=Verdict.FAIL, spec_met=False,
                            required_changes=["Reviewer output was unparseable; re-run review."],
                            parse_failed=True, cost_usd=0.0)
    # re-review parses cleanly and ships
    return ReviewResult(verdict=Verdict.PASS, spec_met=True, cost_usd=0.0)
reviewer_mod.review = fake_review

def mkcfg(**kw):
    return Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                                  protected_branch="MAIN", backlog_backend="none")],
                  audit_path="/tmp/x.jsonl", use_worktree=False, max_iterations=3, **kw)
cfg = mkcfg()
app = cfg.app("automatixy")
ticket = Ticket(id="EU-11", key="EU-11", summary="s", description="d", ephemeral=False, app="automatixy")

au = Audit()
rep = asyncio.run(loop._attempt(ticket, app, cfg, Git(), None, au, loop.Budget(0), "autodev/EU-11"))

chk("re-reviewed exactly once (2 review calls total)", len(review_calls) == 2, f"calls={len(review_calls)}")
chk("did NOT rebuild — builder ran once", len(built) == 1, f"builds={built}")
chk("review_parse_retry audited", any(e["event"] == "review_parse_retry" for e in au.ev))
chk("re-review parsed -> shipped (MERGED)", rep.outcome == Outcome.MERGED, str(rep.outcome))

print("\n========== REVIEW PARSE-RETRY QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------")
print(f"  {passed}/{len(results)} passed", "✅" if passed == len(results) else "❌")
assert passed == len(results)
