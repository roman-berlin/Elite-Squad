"""Retry-guard QA: when the reviewer returns a blocking feedback the build has already seen in the last
few passes — whether it repeats back-to-back or oscillates A/B/A/B (EU-56) — the build isn't making
progress, so the loop STOPS burning passes and escalates instead of grinding to max_iterations. Saves the
wasted passes that drove yesterday's 30-retry burn.

QW3 (2026-07-05): loop.HARD_MAX_PASSES clamps every attempt to 2 passes regardless of
max_iterations. The back-to-back A/A repeat still trips retry_stuck inside the 2-pass window;
oscillation/distinct/blank scenarios now end at the cap (2 builds, escalate via max passes) —
the cap preempts the pass-3+ pathologies those scenarios used to exercise."""
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
loop.run_gate = lambda app, changed_paths=None, **_: GateResult(passed=True, report="")
loop._land = lambda *a, **k: TicketReport("AUTO-14", Outcome.MERGED, 1, 0.0, "automatixy", "b")
async def fake_te(ticket, app, cfg, **_):   # EU-72: absorb store=/build_artifact= kwargs
    return TestEngineerResult(ok=True, coverage="lines 80%→85%")
loop.test_engineer_mod.ensure_coverage = fake_te

built = []
class FakeBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "sized")
    @staticmethod
    async def build(req, app, cfg, audit=None, **_):   # EU-72: absorb store=/spec= kwargs
        built.append(req.iteration)
        return BuildResult(ok=True, summary="did it", cost_usd=0.0, num_turns=1, raw="did it", tools=[])
loop.builder_mod = FakeBuilder

SAME = ["Add an explicit tenant_id filter to the query"]
review_calls = []
async def fake_review(diff, ticket, app, cfg, iteration=1, **_):   # EU-52 iter + EU-72 store=/build_artifact=
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

# ---- loop: A/B/A/B oscillation -> the QW3 hard cap ends it at pass 2 (EU-56 guard preempted) ----
# The build alternates between two never-addressed rejections. Pre-QW3 the deque guard tripped on
# the repeat of A at pass 3; with HARD_MAX_PASSES=2 the cap ends the attempt first — oscillation
# can no longer burn a 3rd pass at all.
osc_built = []
class OscBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "sized")
    @staticmethod
    async def build(req, app, cfg, audit=None, **_):   # EU-72: absorb store=/spec= kwargs
        osc_built.append(req.iteration)
        return BuildResult(ok=True, summary="did it", cost_usd=0.0, num_turns=1, raw="did it", tools=[])
loop.builder_mod = OscBuilder

OSC = [["Add a tenant_id filter to the query"], ["Add a regression test for the empty case"]]
osc_reviews = []
async def fake_review_osc(diff, ticket, app, cfg, iteration=1, **_):   # EU-72: absorb store=/build_artifact=
    osc_reviews.append(1)
    changes = OSC[(len(osc_reviews) - 1) % 2]   # A, B, A, B, …
    return ReviewResult(verdict=Verdict.FAIL, spec_met=False, required_changes=list(changes), cost_usd=0.0)
reviewer_mod.review = fake_review_osc

au2 = Audit()
ticket2 = Ticket(id="AUTO-15", key="AUTO-15", summary="s", description="d", ephemeral=True, app="automatixy")
rep2 = asyncio.run(loop._attempt(ticket2, app, cfg, Git(), None, au2, loop.Budget(0), "autodev/AUTO-15"))

chk("A/B oscillation is cut by the QW3 hard cap — built 2×, never a 3rd pass",
    len(osc_built) == 2, f"builds={osc_built}")
chk("no retry_stuck needed — the cap preempts the oscillation window",
    not any(e["event"] == "retry_stuck" for e in au2.ev))
chk("an oscillating ticket escalates, not a silent grind",
    rep2.outcome == Outcome.ESCALATED, str(rep2.outcome))
chk("QW3: escalation records the structured builder/reviewer disagreement",
    any(e["event"] == "builder_reviewer_disagreement" and e.get("reviewer_position")
        for e in au2.ev))

# ---- EU-56 deque guard stays PINNED behind the cap (review fix 2026-07-05) ----------------------
# The A/B/A oscillation detection needs a 3rd pass, unreachable under HARD_MAX_PASSES=2 — but the
# guard code stays live for any future cap raise. Pin it directly by widening the module constant
# for one scenario (test-only; production has no override path).
osc_built.clear(); osc_reviews.clear()
_orig_cap = loop.HARD_MAX_PASSES
loop.HARD_MAX_PASSES = 4
try:
    au2b = Audit()
    ticket2b = Ticket(id="AUTO-15b", key="AUTO-15b", summary="s", description="d", ephemeral=True,
                      app="automatixy")
    rep2b = asyncio.run(loop._attempt(ticket2b, app, cfg, Git(), None, au2b, loop.Budget(0),
                                      "autodev/AUTO-15b"))
finally:
    loop.HARD_MAX_PASSES = _orig_cap
chk("EU-56 pin: with the cap widened, A/B/A trips retry_stuck on pass 3 — built 3×, NOT 4×",
    len(osc_built) == 3 and any(e["event"] == "retry_stuck" for e in au2b.ev),
    f"builds={osc_built} stuck={[e for e in au2b.ev if e['event']=='retry_stuck']}")
chk("EU-56 pin: the widened-cap oscillation still escalates",
    rep2b.outcome == Outcome.ESCALATED, str(rep2b.outcome))

# ---- loop: distinct feedback every pass must NOT trip the guard (no false positive) (EU-56) ----
# Guarding against over-firing: the deque widening (maxlen=3) must still only trip on a REPEAT.
# Genuinely-different rejections are real progress, so the guard must stay silent — the loop runs
# to the QW3 cap (2 passes) and escalates only because passes ran out, NOT via retry_stuck.
prog_built = []
class ProgBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "sized")
    @staticmethod
    async def build(req, app, cfg, audit=None, **_):   # EU-72: absorb store=/spec= kwargs
        prog_built.append(req.iteration)
        return BuildResult(ok=True, summary="did it", cost_usd=0.0, num_turns=1, raw="did it", tools=[])
loop.builder_mod = ProgBuilder

DISTINCT = [["Add a tenant_id filter"], ["Add a regression test"],
            ["Annotate the return type"], ["Add a docstring explaining why"]]
prog_reviews = []
async def fake_review_prog(diff, ticket, app, cfg, iteration=1, **_):   # EU-72: absorb store=/build_artifact=
    changes = DISTINCT[len(prog_reviews) % len(DISTINCT)]   # A, B, C, D — all different
    prog_reviews.append(1)
    return ReviewResult(verdict=Verdict.FAIL, spec_met=False, required_changes=list(changes), cost_usd=0.0)
reviewer_mod.review = fake_review_prog

au3 = Audit()
ticket3 = Ticket(id="AUTO-16", key="AUTO-16", summary="s", description="d", ephemeral=True, app="automatixy")
rep3 = asyncio.run(loop._attempt(ticket3, app, cfg, Git(), None, au3, loop.Budget(0), "autodev/AUTO-16"))

chk("distinct feedback never trips the guard — ran to the QW3 cap (built 2×)",
    len(prog_built) == 2, f"builds={prog_built}")
chk("no false retry_stuck on genuine progress",
    not any(e["event"] == "retry_stuck" for e in au3.ev))
chk("still escalates when passes simply run out (not a silent grind that merges)",
    rep3.outcome == Outcome.ESCALATED, str(rep3.outcome))

# ---- loop: a blank/empty reject signature must NEVER trip the guard (EU-56 edge) ----
# A reviewer can FAIL with no concrete required_changes (spec_gaps/blocking all empty) — then
# `_changes_sig` is "" and progress is unmeasurable. The guard is `if sig and sig in recent_…`: the
# `sig and` clause is what keeps an empty signature from looking like a repeat. Pin it — drop that
# clause and two blank passes would falsely "match" and trip retry_stuck on pass 2. Here every pass is
# blank, so it must run to the QW3 cap and escalate only because passes ran out, never via the
# stuck guard.
blank_built = []
class BlankBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "sized")
    @staticmethod
    async def build(req, app, cfg, audit=None, **_):   # EU-72: absorb store=/spec= kwargs
        blank_built.append(req.iteration)
        return BuildResult(ok=True, summary="did it", cost_usd=0.0, num_turns=1, raw="did it", tools=[])
loop.builder_mod = BlankBuilder

async def fake_review_blank(diff, ticket, app, cfg, iteration=1, **_):   # EU-72: absorb store=/build_artifact=
    # FAIL with nothing actionable — required_changes / spec_gaps / blocking_issues all empty.
    return ReviewResult(verdict=Verdict.FAIL, spec_met=False, required_changes=[], cost_usd=0.0)
reviewer_mod.review = fake_review_blank

au4 = Audit()
ticket4 = Ticket(id="AUTO-17", key="AUTO-17", summary="s", description="d", ephemeral=True, app="automatixy")
rep4 = asyncio.run(loop._attempt(ticket4, app, cfg, Git(), None, au4, loop.Budget(0), "autodev/AUTO-17"))

chk("blank reject signature never trips the guard — ran to the QW3 cap (built 2×)",
    len(blank_built) == 2, f"builds={blank_built}")
chk("no retry_stuck fired on empty/unmeasurable feedback",
    not any(e["event"] == "retry_stuck" for e in au4.ev))
chk("blank-feedback ticket still escalates when passes run out",
    rep4.outcome == Outcome.ESCALATED, str(rep4.outcome))

print("\n============ RETRY-GUARD QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
