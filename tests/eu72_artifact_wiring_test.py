"""EU-72 iteration 2 — the structured-artifact handoffs are WIRED, not dead code.

Iteration 1 added the dataclasses + PerTicketArtifactStore but never threaded them through the
pipeline. This harness proves the live wiring at three levels:

  A. builder helpers — _digest bounds the diff_digest to ≤ 500 chars; _section_bullets pulls the
     Decisions/Open-questions sections out of the builder summary.
  B. the REAL officers publish/consume via the store (stubbed run_agent, no models):
       • builder.build  reads the SpecArtifact and publishes a BuildArtifact;
       • test_engineer.ensure_coverage reads the BuildArtifact as primary context;
       • reviewer.review reads the BuildArtifact and publishes a typed ReviewVerdict.
  C. end-to-end _attempt — one store per ticket, SpecArtifact derived from the ticket, files_changed
     stamped by the loop, the BuildArtifact handed to the Test Engineer + Reviewer, and the
     ReviewVerdict landing back in the store.
"""
import sys, types, asyncio

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ============================ A. builder helpers ============================ #
from orchestrator import builder
from orchestrator.contracts import (BuildArtifact, BuildResult, PerTicketArtifactStore,
                                     ReviewResult, ReviewVerdict, SpecArtifact,
                                     TestEngineerResult, Ticket, TicketReport, Outcome,
                                     GateResult, Verdict)

chk("_digest passes short text through unchanged", builder._digest("a short summary") == "a short summary")
_long = "x" * 800
_dig = builder._digest(_long)
chk("_digest bounds an over-long summary to ≤ 500 chars", len(_dig) <= 500, str(len(_dig)))
chk("_digest marks truncation with an ellipsis", _dig.endswith("…"))
chk("_digest collapses whitespace", builder._digest("a   b\n\nc") == "a b c")

_summary = ("Implemented the handoff.\n\nDecisions:\n- chose a dataclass\n- kept it minimal\n\n"
            "Open questions:\n- should we persist to disk?\n\nTEST: (no UI)")
chk("_section_bullets pulls the Decisions section",
    builder._section_bullets(_summary, "decision") == ["chose a dataclass", "kept it minimal"])
chk("_section_bullets pulls the Open questions section",
    builder._section_bullets(_summary, "open question") == ["should we persist to disk?"])
chk("_section_bullets returns [] when the heading is absent",
    builder._section_bullets("no sections here", "decision") == [])

_art = builder._build_artifact(BuildResult(ok=True, summary=_summary, raw=""))
chk("_build_artifact yields a BuildArtifact", isinstance(_art, BuildArtifact))
chk("_build_artifact leaves files_changed for the loop to stamp", _art.files_changed == [])
chk("_build_artifact carries the parsed decisions", _art.decisions == ["chose a dataclass", "kept it minimal"])
chk("_build_artifact diff_digest is bounded", len(_art.diff_digest) <= 500)


# ============================ B. real officers publish/consume ============================ #
from orchestrator import agent as agent_mod, reviewer, test_engineer
from orchestrator.agent import AgentRun
from orchestrator.config import Config, AppConfig


def mkcfg(**kw):
    return Config(apps=[AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                                  protected_branch="MAIN", backlog_backend="none")],
                  audit_path="/tmp/eu72w.jsonl", use_worktree=False, **kw)
cfg = mkcfg()
app = cfg.app("automatixy")
tk = Ticket(id="AUTO-72", key="AUTO-72", summary="structured handoffs",
            description="wire the artifacts in", acceptance_criteria=["UNIQUE_ACCEPT_TOKEN"])

# -- builder.build: reads the spec, publishes a BuildArtifact -------------------------------- #
b_prompt = {}
async def fake_build_agent(prompt, options, tag="", ticket_id=None, pass_number=None):
    b_prompt["p"] = prompt
    return AgentRun(text=_summary, final=_summary, cost_usd=0.1, num_turns=2, is_error=False, tools=[])
builder.run_agent = fake_build_agent
cfg.delegation_enabled = False           # force the solo path (no squad), so build() publishes directly
from orchestrator.contracts import BuildRequest
store_b = PerTicketArtifactStore()
spec = SpecArtifact(acceptance=["UNIQUE_ACCEPT_TOKEN"], scope="s", non_goals=["UNIQUE_NONGOAL"])
res_b = asyncio.run(builder.build(BuildRequest(ticket=tk, branch="b", iteration=1),
                                  app, cfg, store=store_b, spec=spec))
chk("build() returns a BuildResult", isinstance(res_b, BuildResult) and res_b.ok)
chk("build() reads the SpecArtifact acceptance into the prompt", "UNIQUE_ACCEPT_TOKEN" in b_prompt.get("p", ""))
chk("build() reads the SpecArtifact non_goals into the prompt", "UNIQUE_NONGOAL" in b_prompt.get("p", ""))
chk("build() publishes a BuildArtifact into the store", isinstance(store_b.build, BuildArtifact))
chk("build() artifact carries the parsed decisions", store_b.build.decisions == ["chose a dataclass", "kept it minimal"])
chk("build() with store=None does not raise (back-compat)",
    asyncio.run(builder.build(BuildRequest(ticket=tk, branch="b", iteration=1), app, cfg)).ok)

# -- test_engineer.ensure_coverage: consumes the BuildArtifact as primary context ------------ #
ba = BuildArtifact(files_changed=["orchestrator/zzz.py"], diff_digest="DIGEST_TOKEN",
                   decisions=[], open_questions=["OQ_TOKEN"])
te_prompt = {}
async def fake_te_agent(prompt, options, tag=""):
    te_prompt["p"] = prompt
    return AgentRun(text="COVERAGE: lines 80→90", final="COVERAGE: lines 80→90",
                    cost_usd=0.1, num_turns=1, is_error=False, tools=[])
test_engineer.run_agent = fake_te_agent
store_te = PerTicketArtifactStore(); store_te.put(ba)
res_te = asyncio.run(test_engineer.ensure_coverage(tk, app, cfg, store=store_te, build_artifact=ba))
chk("ensure_coverage returns a TestEngineerResult", isinstance(res_te, TestEngineerResult))
chk("ensure_coverage leads with the builder's changed files", "orchestrator/zzz.py" in te_prompt.get("p", ""))
chk("ensure_coverage surfaces the builder's diff digest", "DIGEST_TOKEN" in te_prompt.get("p", ""))
chk("ensure_coverage (a consumer stage) publishes nothing — no review/build slot churn",
    store_te.review is None and store_te.build is ba)
# falls back to store.build when no explicit artifact is passed
te_prompt.clear()
asyncio.run(test_engineer.ensure_coverage(tk, app, cfg, store=store_te))
chk("ensure_coverage falls back to store.build when build_artifact arg omitted",
    "orchestrator/zzz.py" in te_prompt.get("p", ""))

# -- reviewer.review: consumes the BuildArtifact, publishes a typed ReviewVerdict ------------ #
r_prompt = {}
async def fake_review_agent(prompt, options, tag=""):
    r_prompt["p"] = prompt
    return AgentRun(text="", final='```json\n{"verdict":"PASS","spec_conformance":{"met":true,"gaps":[]},'
                    '"quality":{"issues":[]},"required_changes":[],"summary":"ok"}\n```',
                    cost_usd=0.1, num_turns=1, is_error=False, tools=[])
reviewer.run_agent = fake_review_agent
store_r = PerTicketArtifactStore(); store_r.put(ba)
res_r = asyncio.run(reviewer.review("a diff", tk, app, cfg, store=store_r, build_artifact=ba))
chk("review returns a ReviewResult", isinstance(res_r, ReviewResult) and res_r.verdict is Verdict.PASS)
chk("review leads with the builder's structured handoff", "BUILDER'S STRUCTURED HANDOFF" in r_prompt.get("p", ""))
chk("review surfaces the builder's changed files", "orchestrator/zzz.py" in r_prompt.get("p", ""))
chk("review publishes a typed ReviewVerdict into the store", isinstance(store_r.review, ReviewVerdict))
chk("the published ReviewVerdict uses the Verdict enum", store_r.review.verdict is Verdict.PASS)
chk("review with store=None stays back-compatible (no publish, no raise)",
    asyncio.run(reviewer.review("a diff", tk, app, cfg)).verdict is Verdict.PASS)


# ============================ C. end-to-end _attempt wiring ============================ #
import orchestrator.loop as loop

loop._notify = lambda c, t: None
loop._route_out_of_scope = lambda *a, **k: None
loop.run_gate = lambda app, changed=None: GateResult(passed=True, report="")
loop._land = lambda *a, **k: TicketReport("AUTO-72", Outcome.MERGED, 1, 0.0, "automatixy", "b")

class Audit:
    def __init__(s): s.ev = []
    def record(s, e, **k): s.ev.append({"event": e, **k})
class Backlog:
    def add_comment(s, *a): pass
    def set_status(s, *a): pass
class Git:
    def has_changes(s): return True
    def diff_against_base(s): return "diff --git a/x b/x\n+change"
    def changed_paths(s): return ["orchestrator/contracts.py"]

cap = {}
class WiringBuilder:
    @staticmethod
    def effort_plan(cfg, it, ticket): return ("low", "sized")
    @staticmethod
    async def build(req, app, cfg, audit=None, store=None, spec=None):
        cap["store"] = store
        cap["spec"] = spec
        if store is not None:
            store.put(BuildArtifact(files_changed=[], diff_digest="did the thing",
                                    decisions=["used a dataclass"], open_questions=["persist?"]))
        return BuildResult(ok=True, summary="did the thing", cost_usd=0.0, num_turns=1, raw="", tools=[])
class WiringTE:
    @staticmethod
    async def ensure_coverage(ticket, app, cfg, store=None, build_artifact=None):
        cap["te_artifact"] = build_artifact
        return TestEngineerResult(ok=True, coverage="lines 80→90")
class WiringReviewer:
    @staticmethod
    async def review(diff, ticket, app, cfg, iteration, store=None, build_artifact=None):
        cap["review_artifact"] = build_artifact
        if store is not None:
            store.put(ReviewVerdict(verdict=Verdict.PASS, blocking=[], notes=[]))
        return ReviewResult(verdict=Verdict.PASS, spec_met=True, cost_usd=0.0)

loop.builder_mod = WiringBuilder
loop.test_engineer_mod = WiringTE
loop.reviewer_mod = WiringReviewer

icfg = mkcfg(max_iterations=3, pm_enabled=False)
iapp = icfg.app("automatixy")
itk = Ticket(id="AUTO-72", key="AUTO-72", summary="structured handoffs", description="d",
             ephemeral=False, app="automatixy", acceptance_criteria=["the handoff is structured"])
git = Git()
rep = asyncio.run(loop._attempt(itk, iapp, icfg, git, Backlog(), Audit(), loop.Budget(0), "autodev/AUTO-72"))

chk("_attempt lands ship-ready work (MERGED)", rep.outcome == Outcome.MERGED, str(rep.outcome))
chk("_attempt instantiates ONE PerTicketArtifactStore and threads it to the builder",
    isinstance(cap.get("store"), PerTicketArtifactStore))
chk("_attempt derives a SpecArtifact from the ticket and hands it to the builder",
    isinstance(cap.get("spec"), SpecArtifact)
    and cap["spec"].acceptance == ["the handoff is structured"]
    and cap["spec"].scope == "structured handoffs")
chk("the store's spec slot is the SpecArtifact passed to the builder", cap["store"].spec is cap["spec"])
chk("the loop stamps the authoritative files_changed onto the published BuildArtifact",
    cap["store"].build is not None and cap["store"].build.files_changed == ["orchestrator/contracts.py"])
chk("the Test Engineer receives the builder's BuildArtifact as primary context",
    cap.get("te_artifact") is cap["store"].build)
chk("the Reviewer receives the builder's BuildArtifact as primary context",
    cap.get("review_artifact") is cap["store"].build)
chk("the Reviewer's ReviewVerdict lands back in the store",
    isinstance(cap["store"].review, ReviewVerdict) and cap["store"].review.verdict is Verdict.PASS)


# ============================ report ============================ #
print("\n============ EU-72 ARTIFACT WIRING QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed", "✅" if passed == len(results) else "❌")
sys.exit(0 if passed == len(results) else 1)
