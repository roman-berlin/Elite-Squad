"""EU-221: wall-clock timeout on officer calls (a stalled provider stream pins a drain).

Officer calls had turn caps (max_turns) but no wall-clock bound. z.ai/GLM streams have been
observed silently stalling (AUTO-73's 4.5-min stall on 07-09; the AUTO-108 planner's GLM
subprocess ran 30+ min with zero output on 07-10; EU-228's planner ran ~20 min then crashed on
07-14) while the whole drain waited. A 3-turn planner call can hang just as long as a 60-turn
builder call — max_turns bounds the number of turns, not the CLOCK.

Pins on agent._run_agent_unrouted / agent._timeout_for_tag / agent.configure_timeouts:
  1. a stream that yields NOTHING for the configured budget is terminated and returns a clean
     AgentRun(is_error=True, final="wall-clock timeout after <budget>s") — no exception escapes
     the call (the outer asyncio.wait_for below is a test-harness safety net only, not part of
     the mechanism under test: it exists so a regression fails fast instead of hanging the whole
     `tests/run_all.py` suite).
  2. the timeout is classified as a clean error, NOT a plan/quota limit (is_plan_limit stays
     False) — the existing fail-safe paths (planner->BUILD, reviewer->retry, builder->ERRORED)
     already handle a plain is_error return; it must not be misread as a Sonnet/GLM cap and
     trigger the Opus-fallback / autopilot-pause machinery.
  3. budgets are configurable per role tag: "builder" gets the (larger) builder budget, every
     other tag gets the (smaller) officer budget, and configure_timeouts(cfg) is what sets both
     from the run config.
  4. a healthy, fast-completing stream is unaffected by the guard (no false-positive timeout).
"""
import sys, asyncio, types

_sdk = types.ModuleType("claude_agent_sdk")
class _Dummy:
    def __init__(self, *a, **k):
        for key, value in k.items():
            setattr(self, key, value)
    def __call__(self, *a, **k): return self
_sdk.__getattr__ = lambda n: _Dummy
_sdk.ClaudeAgentOptions = _Dummy
_sdk.AssistantMessage = _Dummy
_sdk.ToolUseBlock = _Dummy
_sdk.HookMatcher = _Dummy
_sdk.ResultMessage = _Dummy
_sdk.TextBlock = _Dummy
_sdk.query = _Dummy()
sys.modules["claude_agent_sdk"] = _sdk
sys.path.insert(0, ".")

from orchestrator import agent

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

class FakeAssistant:
    def __init__(s, text="", error=None): s.content = [FakeText(text)] if text else []; s.error = error
class FakeText:
    def __init__(s, t): s.text = t
class FakeResult:
    def __init__(s, *, is_error=False, result="", cost=0.01, turns=3):
        s.is_error, s.result = is_error, result
        s.total_cost_usd, s.num_turns = cost, turns
        s.usage = {"input_tokens": 10, "output_tokens": 5}

agent.AssistantMessage = FakeAssistant
agent.TextBlock = FakeText
agent.ResultMessage = FakeResult
agent.ToolUseBlock = None   # the loop guards `ToolUseBlock is not None`

def _opts():
    return types.SimpleNamespace(model="claude-opus-4-8", cwd=None, env={})

# A "never-yielding" fake stream: the SDK's async generator is suspended awaiting the NEXT
# message from the CLI subprocess when it stalls, so it never even reaches its first `yield` —
# model that exactly: block on an await, forever, before any `yield` executes.
def _never_yield():
    async def fake_query(prompt=None, options=None):
        await asyncio.sleep(3600)
        if False:            # pragma: no cover — keeps this an async *generator* function
            yield
    return fake_query

def _instant(events):
    async def fake_query(prompt=None, options=None):
        for e in events:
            yield e
    return fake_query

# Keep the whole harness fast: shrink both budgets to well under a second.
agent._OFFICER_TIMEOUT_S = 0.05
agent._BUILDER_TIMEOUT_S = 0.08

# ---- 1) a stalled officer-tagged stream is terminated cleanly, no exception ---- #
agent.query = _never_yield()
run = asyncio.run(asyncio.wait_for(
    agent._run_agent_unrouted("p", _opts(), tag="planner"), timeout=5))
chk("stalled stream returns (no exception escapes)", run is not None)
chk("is_error is set", run.is_error)
chk("final names the wall-clock timeout",
    run.final == "wall-clock timeout after 0.05s", run.final)

# ---- 2) NOT classified as a plan/quota limit — a plain transient/infra error ---- #
chk("timeout is NOT a plan-limit (would wrongly probe Opus / pause autopilot)",
    not run.is_plan_limit and run.plan_limit_kind == "", (run.is_plan_limit, run.plan_limit_kind))
chk("timeout is NOT a turn-limit either (distinct failure class)", not run.is_turn_limit)

# ---- 3) budgets are configurable per role tag ---- #
chk("planner/reviewer/pm/etc. use the officer budget",
    agent._timeout_for_tag("planner") == agent._OFFICER_TIMEOUT_S)
chk("builder uses its own (larger) budget",
    agent._timeout_for_tag("builder") == agent._BUILDER_TIMEOUT_S)
chk("officer and builder budgets are independently configurable",
    agent._OFFICER_TIMEOUT_S != agent._BUILDER_TIMEOUT_S)

# The builder tag gets its own (larger, still-shrunk-for-the-test) budget end to end.
agent.query = _never_yield()
run_builder = asyncio.run(asyncio.wait_for(
    agent._run_agent_unrouted("p", _opts(), tag="builder"), timeout=5))
chk("a stalled BUILDER stream is bounded by builder_timeout_s, not officer_timeout_s",
    run_builder.final == "wall-clock timeout after 0.08s", run_builder.final)

# configure_timeouts(cfg) is the wiring point (main.py / server.py call it once at startup).
_fake_cfg = types.SimpleNamespace(officer_timeout_s=111, builder_timeout_s=222)
agent.configure_timeouts(_fake_cfg)
chk("configure_timeouts sets the officer budget from cfg", agent._OFFICER_TIMEOUT_S == 111)
chk("configure_timeouts sets the builder budget from cfg", agent._BUILDER_TIMEOUT_S == 222)
# restore small budgets for the remaining checks
agent._OFFICER_TIMEOUT_S = 0.05
agent._BUILDER_TIMEOUT_S = 0.08

# ---- 4) a healthy, fast-completing stream is unaffected (no false-positive timeout) ---- #
agent.query = _instant([FakeAssistant("done."), FakeResult(is_error=False, result="all good")])
run_ok = asyncio.run(asyncio.wait_for(
    agent._run_agent_unrouted("p", _opts(), tag="planner"), timeout=5))
chk("healthy run completes normally under the budget",
    run_ok.final == "all good" and not run_ok.is_error, (run_ok.final, run_ok.is_error))

print("\n========== EU-221 WALL-CLOCK TIMEOUT QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
