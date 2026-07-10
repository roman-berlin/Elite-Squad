"""The "Claude Code returned an error result: success" crash class (2026-07-09).

SDK quirk (claude_agent_sdk 0.2.x): when the CLI hits a terminal provider-side error, it emits a
final result JSON with is_error=true but subtype "success" — the REAL error text rides in the
`result` field, and the `errors` array is empty. The SDK then raises
"Claude Code returned an error result: success" (interpolating the useless subtype), DISCARDING the
real error and skipping metering/audit/transcript. 9 runs died that way since 2026-06-21 (EU-136 +
AUTO-73 on 07-09 alone), each stranding its ticket with a nonsense ticket_exception.

Pins on agent._run_agent_unrouted:
  1. post-result SDK raise degrades to a normal AgentRun(is_error=True) whose final is the REAL
     provider error text — the run ends cleanly instead of a ticket_exception.
  2. a GLM/z.ai quota text in ResultMessage.result sets is_plan_limit (the EU-202 blind spot:
     the classifier only ran on AssistantMessage.error, never on the result text).
  3. a genuine crash BEFORE any result still raises (no swallowing of real failures).
  4. the healthy path is untouched (success result → final, no error).
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

# Distinct classes so isinstance() discriminates inside the stream loop.
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

def _query_for(events, raise_after=None):
    async def fake_query(prompt=None, options=None):
        for e in events:
            yield e
        if raise_after is not None:
            raise raise_after
    return fake_query

def _opts():
    return types.SimpleNamespace(model="claude-opus-4-8", cwd=None, env={})

REAL_ERR = "API Error (z.ai): 429 insufficient quota — your GLM coding plan usage limit was reached"

# ---- 1) the crash class: post-result raise degrades to a clean error return ---- #
agent.query = _query_for(
    [FakeAssistant("working…"), FakeResult(is_error=True, result=REAL_ERR)],
    raise_after=Exception("Claude Code returned an error result: success"))
run = asyncio.run(agent._run_agent_unrouted("p", _opts(), tag="builder"))
chk("post-result SDK raise returns a normal AgentRun (no exception)", run is not None)
chk("is_error is set", run.is_error)
chk("final carries the REAL provider error, not 'success'",
    "insufficient quota" in (run.final or ""), run.final)
chk("metering fields survived (cost/turns from the consumed result)",
    run.cost_usd == 0.01 and run.num_turns == 3, (run.cost_usd, run.num_turns))

# ---- 2) GLM quota text in the result classifies as a plan limit ---- #
chk("z.ai quota text in ResultMessage.result sets is_plan_limit (EU-202 blind spot closed)",
    run.is_plan_limit, run.plan_limit_kind)

# ---- 2b) a non-quota provider error does NOT classify as a plan limit ---- #
agent.query = _query_for(
    [FakeResult(is_error=True, result="TypeError: fetch failed — ECONNRESET mid-stream")],
    raise_after=Exception("Claude Code returned an error result: success"))
run2 = asyncio.run(agent._run_agent_unrouted("p", _opts(), tag="builder"))
chk("plain provider error → is_error without is_plan_limit",
    run2.is_error and not run2.is_plan_limit, (run2.is_error, run2.is_plan_limit))

# ---- 3) a genuine pre-result crash still raises ---- #
agent.query = _query_for([FakeAssistant("hi")], raise_after=RuntimeError("socket exploded"))
try:
    asyncio.run(agent._run_agent_unrouted("p", _opts(), tag="builder"))
    chk("pre-result crash re-raises", False, "no exception raised")
except RuntimeError:
    chk("pre-result crash re-raises", True)
# and the SDK-shaped message WITHOUT a consumed result also re-raises
agent.query = _query_for([], raise_after=Exception("Claude Code returned an error result: success"))
try:
    asyncio.run(agent._run_agent_unrouted("p", _opts(), tag="builder"))
    chk("SDK-shaped raise with NO result seen still re-raises", False, "no exception raised")
except Exception as e:
    chk("SDK-shaped raise with NO result seen still re-raises", "error result" in str(e))

# ---- 4) healthy path untouched ---- #
agent.query = _query_for([FakeAssistant("done."), FakeResult(is_error=False, result="all good")])
run3 = asyncio.run(agent._run_agent_unrouted("p", _opts(), tag="builder"))
chk("healthy run: final = result, no error flags",
    run3.final == "all good" and not run3.is_error and not run3.is_plan_limit,
    (run3.final, run3.is_error))

print("\n========== ERROR-RESULT DEGRADATION QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
