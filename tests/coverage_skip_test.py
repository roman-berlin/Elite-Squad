"""EU-53: the Test Engineer (a full-tools Opus agent) and its post-coverage re-gate must NOT
re-run on every retry pass. The loop hashes the diff and (a) skips the whole coverage stage when
the tree is byte-identical to what it last covered, and (b) skips the re-gate when the Test
Engineer added no files."""
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
                                     GateResult, Verdict, Outcome)

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
    """Diff = product part (set by the builder) + tests part (set by the Test Engineer)."""
    def __init__(s): s.product = "P1"; s.tests = ""
    def has_changes(s): return True
    def diff_against_base(s): return s.product + "|" + s.tests
    def changed_paths(s): return ["orchestrator/loop.py"]

class FakeBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "sized")
    @staticmethod
    async def build(req, app, cfg, audit=None):
        return BuildResult(ok=True, summary="did it", cost_usd=0.0, num_turns=1, raw="", tools=[])
loop.builder_mod = FakeBuilder

# Reviewer always FAILs with the SAME feedback -> loop runs 2 passes then the retry-stuck guard
# breaks (we only need >1 pass to prove the skip on the second pass).
class FakeReviewer:
    @staticmethod
    async def review(diff, ticket, app, cfg, iteration):
        return ReviewResult(verdict=Verdict.FAIL, spec_met=False,
                            required_changes=["address the gap"], summary="no", raw="")
loop.reviewer_mod = FakeReviewer

gate_calls = {"n": 0}
def fake_gate(app, changed=None):
    gate_calls["n"] += 1
    return GateResult(passed=True, report="")
loop.run_gate = fake_gate

def mkcfg(**kw):
    return Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                                  protected_branch="MAIN", backlog_backend="none")],
                  audit_path="/tmp/eu53.jsonl", use_worktree=False, max_iterations=3,
                  pm_enabled=False, **kw)
cfg = mkcfg()
app = cfg.app("automatixy")
ticket = Ticket(id="AUTO-99", key="AUTO-99", summary="s", description="d", ephemeral=False, app="automatixy")


def run_attempt(te_adds_files: bool):
    """Drive one _attempt with a Test Engineer that either adds test files or doesn't."""
    gate_calls["n"] = 0
    git = Git()
    te_calls = {"n": 0}
    class FakeTE:
        @staticmethod
        async def ensure_coverage(t, a, c):
            te_calls["n"] += 1
            if te_adds_files:
                git.tests = "T1"          # the TE wrote test files -> the diff changes
            return TestEngineerResult(ok=True, coverage="lines 80→90", cost_usd=0.0,
                                      num_turns=1, raw="", tools=[])
    loop.test_engineer_mod = FakeTE
    au = Audit()
    rep = asyncio.run(loop._attempt(ticket, app, cfg, git, Backlog(), au, loop.Budget(0),
                                    "autodev/AUTO-99"))
    return te_calls["n"], gate_calls["n"], au, rep


# ---- Scenario A: Test Engineer adds files -> covered once, then skipped on the unchanged pass ----
te_n, gate_n, au, rep = run_attempt(te_adds_files=True)
chk("TE adds files: coverage agent runs exactly ONCE across the retry passes", te_n == 1, f"te={te_n}")
chk("TE adds files: re-gate ran on pass 1 (pre-review + post-coverage), skip pass = pre-review only",
    gate_n == 3, f"gate={gate_n}")
chk("TE adds files: the unchanged pass is audited as test_engineer_skipped",
    any(e["event"] == "test_engineer_skipped" for e in au.ev))

# ---- Scenario B: Test Engineer adds NO files -> the post-coverage re-gate is skipped ----------
te_n2, gate_n2, au2, rep2 = run_attempt(te_adds_files=False)
chk("TE adds nothing: coverage agent still runs once (first pass)", te_n2 == 1, f"te={te_n2}")
chk("TE adds nothing: NO post-coverage re-gate — only the two pre-review gates", gate_n2 == 2, f"gate={gate_n2}")
chk("TE adds nothing: 2nd pass skips the coverage agent (diff unchanged)",
    any(e["event"] == "test_engineer_skipped" for e in au2.ev))

print("\n================ EU-53 COVERAGE-SKIP QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
assert passed == len(results), "EU-53 coverage-skip test failed"
