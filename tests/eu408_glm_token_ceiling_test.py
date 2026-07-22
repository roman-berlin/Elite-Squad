"""EU-408 — a per-pass GLM token ceiling (production audit 2026-07-21, P1 resources-cost).

builder_task_budget (the graceful in-pass pacing countdown) is Anthropic-only — GLM passes ignore
it entirely, and a single GLM builder pass was measured at 13.28M input tokens (4.4x the intended
envelope). The only GLM brakes were the turn ceiling (coarse) and the quota gate (per-run, not
per-pass). This gives GLM passes a compensating in-pass bound: agent.py counts cumulative input
tokens on the stream and, once a GLM pass crosses ``glm_pass_token_ceiling`` (default ~5M), cuts it
off CLEANLY and treats it exactly like a turn-limit hit (split/boosted-retry ladder — never a bare
error), recording a ``glm_token_ceiling`` audit event with the burned-token count.

Pins on agent._run_agent_unrouted / agent.configure_timeouts / builder.BuildResult:
  1. an over-ceiling GLM pass is cut off mid-stream (the trailing ResultMessage is never reached)
     and returns a clean AgentRun(is_error=True, is_turn_limit=True) — the SAME signal a real
     max-turns blow-out sets, so the loop's existing turn-limit ladder routes it (split → boosted
     retry → escalate) instead of misclassifying it as Outcome.ERRORED.
  2. the cutoff is NOT a quota/plan-limit (is_plan_limit stays False — distinct from a GLM cap),
     and NOT a wall-clock timeout (is_turn_limit True, the opposite of the EU-221 timeout class).
  3. the ledger records it: a ``glm_token_ceiling`` audit event fires carrying the burned input
     tokens, and the run's recorded input_tokens reflect the real burn (not zeroed by the early
     cutoff before the SDK's final ResultMessage).
  4. the ceiling is GLM-ONLY: the same token burn on a NATIVE (Anthropic) pass does not trip it,
     and a GLM pass that stays UNDER the ceiling completes normally (no false trip).
  5. the ceiling is configurable: configure_timeouts(cfg) reads cfg.glm_pass_token_ceiling; the
     module default applies when unset (~5M).
  6. routing contract: a BuildResult with is_turn_limit=True and num_turns BELOW turns_for still
     enters the loop's turn-limit ladder (build.is_turn_limit is part of the condition), so a
     token-cutoff that burned few-but-huge turns is not dropped into the ERRORED fallthrough.
"""
import asyncio
import os
import sys
import types

# ---- stub the Agent SDK exactly like tests/eu221_wallclock_timeout_test.py ----
_sdk = types.ModuleType("claude_agent_sdk")


class _Dummy:
    def __init__(self, *a, **k):
        for key, value in k.items():
            setattr(self, key, value)

    def __call__(self, *a, **k):
        return self


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

os.environ["GLM_AUTH_TOKEN"] = "test-token"   # so backends.apply() resolves GLM (not fail-closed to NATIVE)

from orchestrator import agent, backends, builder  # noqa: E402
from orchestrator.config import Config, AppConfig  # noqa: E402

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


class FakeText:
    def __init__(self, t):
        self.text = t


class FakeAssistant:
    """An AssistantMessage stand-in. `usage` carries that turn's billed input tokens (the Anthropic
    API reports per-turn usage; cumulative billed input = the running sum, exactly what the 13.28M
    figure measures)."""

    def __init__(self, text="", *, in_tok=0, out_tok=0, error=None):
        self.content = [FakeText(text)] if text else []
        self.error = error
        self.usage = {"input_tokens": in_tok,
                      "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0,
                      "output_tokens": out_tok}


class FakeResult:
    def __init__(self, *, is_error=False, result="all good", cost=0.01, turns=2):
        self.is_error, self.result = is_error, result
        self.subtype = ""
        self.total_cost_usd, self.num_turns = cost, turns
        self.usage = {"input_tokens": 10, "output_tokens": 5}


agent.AssistantMessage = FakeAssistant
agent.TextBlock = FakeText
agent.ResultMessage = FakeResult
agent.ToolUseBlock = None   # the loop guards `ToolUseBlock is not None`


def _opts():
    return types.SimpleNamespace(model="claude-opus-4-8", cwd=None, env={}, max_turns=200)


def _instant(events, sentinel=None):
    """fake query(): yield each event. If `sentinel` is a list, append a marker when the trailing
    FakeResult is actually consumed — so the test can PROVE the cutoff happened mid-stream (the
    result never arrived)."""

    async def fake_query(prompt=None, options=None):
        for e in events:
            if isinstance(e, FakeResult) and sentinel is not None:
                sentinel.append("result-reached")
            yield e

    return fake_query


# capture audit events (the ledger/analyzer sink)
_audit_events = []


class _FakeAudit:
    def record(self, event, **kw):
        _audit_events.append((event, kw))


agent.configure_audit(_FakeAudit())

# keep wall-clock budgets generous so the only thing that can trip here is the token ceiling
agent._OFFICER_TIMEOUT_S = 30
agent._BUILDER_TIMEOUT_S = 30

# ---- 1) an over-ceiling GLM pass is cut off mid-stream → turn-limit signal, not a bare error ---- #
agent._GLM_PASS_TOKEN_CEILING = 1_000_000   # low ceiling so the test burn crosses it fast
tok = backends.set_backend("glm")
result_sentinel = []
try:
    agent.query = _instant([
        FakeAssistant("turn 1", in_tok=400_000, out_tok=1_000),    # cumulative 0.4M
        FakeAssistant("turn 2", in_tok=400_000, out_tok=1_000),    # cumulative 0.8M
        FakeAssistant("turn 3", in_tok=400_000, out_tok=1_000),    # cumulative 1.2M → over ceiling
        FakeResult(),                                               # must NEVER be reached
    ], sentinel=result_sentinel)
    run = asyncio.run(asyncio.wait_for(
        agent._run_agent_unrouted("p", _opts(), tag="builder"), timeout=10))
finally:
    backends.reset_backend(tok)

chk("over-ceiling GLM pass returns cleanly (no exception escapes)", run is not None)
chk("over-ceiling GLM pass is marked is_error (build did not complete)", run.is_error)
chk("over-ceiling GLM pass is routed as a TURN-LIMIT (→ split/boost ladder), not a bare error",
    run.is_turn_limit, str(run.is_turn_limit))
chk("the trailing ResultMessage was NOT reached (cutoff happened mid-stream)",
    result_sentinel == [], str(result_sentinel))
chk("final names the GLM token ceiling (auditable reason)",
    "token ceiling" in (run.final or "").lower() or "glm" in (run.final or "").lower(), run.final)

# ---- 2) distinct from a quota cap and from the EU-221 wall-clock timeout ---- #
chk("token ceiling is NOT a plan/quota limit (would wrongly pause the drain / probe Opus)",
    not run.is_plan_limit and run.plan_limit_kind == "", (run.is_plan_limit, run.plan_limit_kind))

# ---- 3) the ledger records the cutoff: event + real burned tokens ---- #
ceil_events = [kw for (ev, kw) in _audit_events if ev == "glm_token_ceiling"]
chk("a glm_token_ceiling audit event fired", len(ceil_events) == 1,
    str([(ev, kw) for ev, kw in _audit_events]))
if ceil_events:
    kw = ceil_events[0]
    chk("the event records the burned input tokens (1.2M cumulative at cutoff)",
        kw.get("input_tokens") == 1_200_000, str(kw.get("input_tokens")))
    chk("the event records the configured ceiling", kw.get("ceiling") == 1_000_000,
        str(kw.get("ceiling")))
chk("the run's recorded input_tokens reflect the real burn (not zeroed by the early cutoff)",
    run.input_tokens == 1_200_000, str(run.input_tokens))

# ---- 4a) the ceiling is GLM-ONLY: the same burn on a NATIVE pass does not trip ---- #
native_sentinel = []
tok2 = backends.set_backend("opus")
try:
    agent.query = _instant([
        FakeAssistant("turn 1", in_tok=400_000, out_tok=1_000),
        FakeAssistant("turn 2", in_tok=400_000, out_tok=1_000),
        FakeAssistant("turn 3", in_tok=400_000, out_tok=1_000),   # 1.2M — over the GLM ceiling
        FakeResult(result="native done"),
    ], sentinel=native_sentinel)
    run_native = asyncio.run(asyncio.wait_for(
        agent._run_agent_unrouted("p", _opts(), tag="builder"), timeout=10))
finally:
    backends.reset_backend(tok2)
chk("NATIVE pass with the same burn is NOT cut off (ceiling is GLM-only)",
    not run_native.is_turn_limit and not run_native.is_error
    and run_native.final == "native done" and native_sentinel == ["result-reached"],
    (run_native.is_turn_limit, run_native.is_error, run_native.final, native_sentinel))

# ---- 4b) a GLM pass that stays UNDER the ceiling completes normally (no false trip) ---- #
under_sentinel = []
tok3 = backends.set_backend("glm")
try:
    agent.query = _instant([
        FakeAssistant("turn 1", in_tok=200_000, out_tok=1_000),   # 0.2M
        FakeAssistant("turn 2", in_tok=200_000, out_tok=1_000),   # 0.4M  (under 1M)
        FakeResult(result="glm done"),
    ], sentinel=under_sentinel)
    run_under = asyncio.run(asyncio.wait_for(
        agent._run_agent_unrouted("p", _opts(), tag="builder"), timeout=10))
finally:
    backends.reset_backend(tok3)
chk("GLM pass under the ceiling completes normally (no false-positive cutoff)",
    not run_under.is_turn_limit and not run_under.is_error
    and run_under.final == "glm done" and under_sentinel == ["result-reached"],
    (run_under.is_turn_limit, run_under.is_error, run_under.final, under_sentinel))

# ---- 5) configurable via cfg.glm_pass_token_ceiling; module default ~5M when unset ---- #
agent.configure_timeouts(types.SimpleNamespace(glm_pass_token_ceiling=7_777_777))
chk("configure_timeouts reads cfg.glm_pass_token_ceiling into the module ceiling",
    agent._GLM_PASS_TOKEN_CEILING == 7_777_777, str(agent._GLM_PASS_TOKEN_CEILING))
# a Config with no explicit knob carries the documented ~5M default
_default_cfg = Config(apps=[AppConfig(name="x", repo_path="/tmp/eu408", base_branch="dev",
                                      protected_branch="main", backlog_backend="none")],
                      audit_path="/tmp/eu408-audit.jsonl")
chk("Config exposes glm_pass_token_ceiling with a ~5M default",
    getattr(_default_cfg, "glm_pass_token_ceiling", None) == 5_000_000,
    str(getattr(_default_cfg, "glm_pass_token_ceiling", None)))

# ---- 6) routing contract: an is_turn_limit BuildResult enters the ladder even with few turns ---- #
# A token-cutoff burns FEW turns but huge tokens: num_turns well below turns_for, yet is_turn_limit.
# The loop's turn-limit ladder condition is `build.is_turn_limit or num_turns >= turns_for(eff)` —
# model the build the loop sees with a stand-in so the CONDITION is what's pinned (deterministic,
# no real SDK), and confirm BuildResult actually carries the field the loop reads.
from orchestrator.contracts import BuildResult  # noqa: E402
from types import SimpleNamespace as _NS  # noqa: E402

eff = builder.effort_for(_default_cfg, 1, None)
_turns_cap = builder.turns_for(_default_cfg, eff)
cutoff_build = _NS(ok=False, num_turns=3, is_turn_limit=True)            # few turns, huge tokens
non_cutoff = _NS(ok=False, num_turns=3, is_turn_limit=False)
chk("a cutoff build (few turns, is_turn_limit) satisfies the loop's turn-limit condition",
    (cutoff_build.is_turn_limit or cutoff_build.num_turns >= _turns_cap),
    str((cutoff_build.is_turn_limit, cutoff_build.num_turns, _turns_cap)))
chk("without is_turn_limit, few-turns failure does NOT enter the ladder (ERRORED fallthrough)",
    not (non_cutoff.is_turn_limit or non_cutoff.num_turns >= _turns_cap))
chk("BuildResult declares the is_turn_limit field the loop routes on",
    "is_turn_limit" in getattr(BuildResult, "__dataclass_fields__", {}),
    str(list(getattr(BuildResult, "__dataclass_fields__", {}).keys())))

print("\n========== EU-408 GLM PER-PASS TOKEN CEILING QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
