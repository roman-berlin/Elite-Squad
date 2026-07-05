"""EU-53 regression: the coverage-pass skip MUST be conditional on the diff being byte-identical.

coverage_skip_test.py proves the *skip* side (unchanged tree → Test Engineer + re-gate skipped).
This pins the *inverse*, which the skip test cannot catch: when the builder actually changes the
tree on a retry pass, the (Opus) coverage agent MUST run again on the new tree. A regression that
skipped unconditionally after the first pass (e.g. `if covered_diff_hash is not None:`) would still
pass the skip test but FAIL here — that is the exact defect this guard freezes out.
"""
import sys, types, asyncio
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.loop as loop
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import (Ticket, BuildResult, ReviewResult, TestEngineerResult,
                                     GateResult, Verdict)

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# -- silence side-effects that would touch the network / Telegram / Jira --------------------- #
loop._notify = lambda c, t: None
loop.decisions.add = lambda *a, **k: None
loop.decisions.reply_hint = lambda *a, **k: ""
loop._route_out_of_scope = lambda *a, **k: None
async def _brief(cfg, tid, raw): return raw
loop._decision_brief = _brief

class Audit:
    def __init__(s): s.ev = []
    def record(s, e, **k): s.ev.append({"event": e, **k})
class Backlog:
    def add_comment(s, *a): pass
    def set_status(s, *a): pass

class Git:
    """Diff = product part (the builder) + tests part (the Test Engineer)."""
    def __init__(s): s.product = "P0"; s.tests = ""
    def has_changes(s): return True
    def diff_against_base(s): return s.product + "|" + s.tests
    def changed_paths(s): return ["orchestrator/loop.py"]

gate_calls = {"n": 0}
def fake_gate(app, changed=None, **_):
    gate_calls["n"] += 1
    return GateResult(passed=True, report="")
loop.run_gate = fake_gate

def mkcfg(**kw):
    return Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                                  protected_branch="MAIN", backlog_backend="none")],
                  audit_path="/tmp/eu53_rerun.jsonl", use_worktree=False, max_iterations=3,
                  pm_enabled=False, **kw)
cfg = mkcfg()
app = cfg.app("automatixy")
ticket = Ticket(id="AUTO-99", key="AUTO-99", summary="s", description="d", ephemeral=False, app="automatixy")

# Shared state for one _attempt run.
git = Git()
te_calls = {"n": 0}
te_hashes = []

class FakeBuilder:
    """Builder that MUTATES the product diff on every pass — i.e. it genuinely acts on feedback."""
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "sized")
    @staticmethod
    async def build(req, app, cfg, audit=None, **_):   # EU-72: absorb store=/spec= kwargs
        git.product = f"P{req.iteration}"      # a different tree every pass
        return BuildResult(ok=True, summary="did it", cost_usd=0.0, num_turns=1, raw="", tools=[])
loop.builder_mod = FakeBuilder

class FakeTE:
    @staticmethod
    async def ensure_coverage(t, a, c, **_):   # EU-72: absorb store=/build_artifact= kwargs
        te_calls["n"] += 1
        te_hashes.append(git.diff_against_base())
        git.tests = f"T{te_calls['n']}"        # the TE writes test files each time it runs
        return TestEngineerResult(ok=True, coverage="lines 80→90", cost_usd=0.0,
                                  num_turns=1, raw="", tools=[])
loop.test_engineer_mod = FakeTE

class FakeReviewer:
    """FAIL with DIFFERENT feedback each pass so the retry-stuck guard does not short-circuit and
    the loop actually runs every iteration (1..max_iterations)."""
    @staticmethod
    async def review(diff, ticket, app, cfg, iteration, **_):   # EU-72: absorb store=/build_artifact= kwargs
        return ReviewResult(verdict=Verdict.FAIL, spec_met=False,
                            required_changes=[f"address gap #{iteration}"], summary="no", raw="")
loop.reviewer_mod = FakeReviewer

au = Audit()
asyncio.run(loop._attempt(ticket, app, cfg, git, Backlog(), au, loop.Budget(0), "autodev/AUTO-99"))

# The builder changed the tree on every pass, so the coverage agent must have run on every pass —
# never wrongly skipped as "unchanged". QW3: loop.HARD_MAX_PASSES clamps the requested
# max_iterations=3 to 2 effective passes; the EU-53 property (re-run on every changed tree) is
# unchanged, only the pass count shrinks.
effective_passes = min(cfg.max_iterations, loop.HARD_MAX_PASSES)
chk("changed diff re-runs the coverage agent every pass (no false skip)",
    te_calls["n"] == effective_passes, f"te={te_calls['n']} passes={effective_passes}")
chk("each coverage pass saw a DISTINCT tree (proves it re-ran on new work, not cached)",
    len(set(te_hashes)) == effective_passes, f"hashes={te_hashes}")
chk("no test_engineer_skipped events were emitted when the diff kept changing",
    not any(e["event"] == "test_engineer_skipped" for e in au.ev),
    f"skips={[e for e in au.ev if e['event']=='test_engineer_skipped']}")

print("\n============== EU-53 COVERAGE-RERUN REGRESSION ==============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
assert passed == len(results), "EU-53 coverage-rerun regression failed"
