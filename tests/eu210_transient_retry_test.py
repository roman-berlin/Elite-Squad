"""EU-210: transient 429 must not be misclassified as a weekly Sonnet cap.

Before this ticket, a Sonnet transient rate-limit (429/529 overload) got exactly ONE backoff
retry (5s) and, if that retry also came back transient, `run_agent_with_fallback` just returned
the still-transient result as-is — never distinguishing "still just a blip" from "actually a
cap", and never giving the operator the Opus-probe signal EU-82/EU-108 rely on to decide
pause-vs-continue.

EU-210 tightens this:
  1. Retry a transient Sonnet 429 up to `_TRANSIENT_RETRY_ATTEMPTS` (2) times, spaced
     `_TRANSIENT_RETRY_BACKOFF_S` (~2s) apart — NOT the old single 5s retry.
  2. The first retry that comes back clean (not a plan-limit) is returned immediately: no Opus
     probe, no cap classification, no fallback state.
  3. If EVERY backoff retry (initial call + all retries) still comes back transient, that is no
     longer treated as "just a blip" — escalate into the existing cap path (probe Opus once) so
     a persistently-failing transient error still gets EU-82/EU-108 semantics instead of being
     silently swallowed.
  4. `_classify_plan_limit` keeps requiring explicit plan/weekly/usage/quota language for "cap" —
     this ticket does NOT loosen that pattern match.

Written fail-first: against the pre-EU-210 code, the constants don't exist yet (attempt count),
the transient-exhausted case never reaches the Opus probe, and the retry loop only fires once.
"""
from __future__ import annotations

import sys
import types

# Stub the Agent SDK (imported transitively) so import never needs a real model / network.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import asyncio

from orchestrator import agent as agent_mod
from orchestrator.agent import AgentRun

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


class _Opts:
    def __init__(self, model):
        self.model = model


def _transient_run(model_hint="sonnet"):
    return AgentRun(text="", final="", cost_usd=0.0, num_turns=0, is_error=True,
                     is_plan_limit=True, plan_limit_kind="transient")


def _clean_run():
    return AgentRun(text="ok", final="ok", cost_usd=0.1, num_turns=1, is_error=False)


def _cap_run():
    return AgentRun(text="", final="", cost_usd=0.0, num_turns=0, is_error=True,
                     is_plan_limit=True, plan_limit_kind="cap")


# ============ AC4: retry-attempt/backoff constants ============ #
def test_retry_constants():
    chk("_TRANSIENT_RETRY_ATTEMPTS == 2", getattr(agent_mod, "_TRANSIENT_RETRY_ATTEMPTS", None) == 2,
        getattr(agent_mod, "_TRANSIENT_RETRY_ATTEMPTS", "MISSING"))
    backoff = getattr(agent_mod, "_TRANSIENT_RETRY_BACKOFF_S", None)
    chk("_TRANSIENT_RETRY_BACKOFF_S ~= 2.0", backoff is not None and 1.5 <= backoff <= 2.5, backoff)


# ============ AC1: transient then clean retry → clean result, no Opus probe ============ #
def test_transient_then_clean_retry_no_opus_probe():
    calls = []
    sleeps = []

    async def fake_run_agent(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
        calls.append(getattr(options, "model", ""))
        if len(calls) == 1:
            return _transient_run()
        return _clean_run()

    async def fake_sleep(s):
        sleeps.append(s)

    orig_run_agent = agent_mod.run_agent
    orig_sleep = asyncio.sleep
    agent_mod.run_agent = fake_run_agent
    asyncio.sleep = fake_sleep
    try:
        res = asyncio.run(agent_mod.run_agent_with_fallback(
            "p", _Opts("claude-sonnet-5"), tag="builder"))
    finally:
        agent_mod.run_agent = orig_run_agent
        asyncio.sleep = orig_sleep

    chk("clean retry returned (is_plan_limit False)", res is not None and not res.is_plan_limit, res)
    chk("clean retry result surfaced (final == 'ok')", res is not None and res.final == "ok", res and res.final)
    chk("no error on clean retry", res is not None and not res.is_error)
    chk("only 2 calls made (initial + 1 retry) — no Opus probe", len(calls) == 2, calls)
    chk("every call used Sonnet (no Opus probe model swap)",
        all("sonnet" in m.lower() for m in calls), calls)
    chk("exactly one backoff sleep before the successful retry", len(sleeps) == 1, sleeps)


# ============ AC2: explicit cap language takes the Opus-probe path, not transient-retry ============ #
def test_explicit_cap_language_classifies_as_cap():
    for err in ("Claude usage limit reached", "weekly limit exceeded", "quota exceeded"):
        kind = agent_mod._classify_plan_limit(err)
        chk(f"'{err}' classifies as cap", kind == "cap", kind)


def test_cap_result_takes_opus_probe_path_not_transient_retry():
    calls = []
    sleeps = []

    async def fake_run_agent(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
        model = getattr(options, "model", "")
        calls.append(model)
        if "sonnet" in model.lower():
            return _cap_run()
        return _clean_run()

    async def fake_sleep(s):
        sleeps.append(s)

    orig_run_agent = agent_mod.run_agent
    orig_sleep = asyncio.sleep
    agent_mod.run_agent = fake_run_agent
    asyncio.sleep = fake_sleep
    try:
        res = asyncio.run(agent_mod.run_agent_with_fallback(
            "p", _Opts("claude-sonnet-5"), tag="builder"))
    finally:
        agent_mod.run_agent = orig_run_agent
        asyncio.sleep = orig_sleep

    chk("cap path probes Opus (2 calls: sonnet cap + opus)", len(calls) == 2, calls)
    chk("second call used Opus, not another Sonnet retry",
        len(calls) == 2 and "opus" in calls[1].lower(), calls)
    chk("no transient backoff sleep on the cap path", len(sleeps) == 0, sleeps)
    chk("cap path returns the Opus result (headroom found)",
        res is not None and not res.is_error and res.final == "ok")


# ============ AC3: transient on every attempt → escalate to cap handling ============ #
def test_transient_exhausted_escalates_to_opus_probe_then_all_models_cap():
    calls = []
    sleeps = []

    async def fake_run_agent(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
        model = getattr(options, "model", "")
        calls.append(model)
        # Sonnet always transient; Opus probe also plan-limited (All-models cap).
        if "sonnet" in model.lower():
            return _transient_run()
        return AgentRun(text="", final="", cost_usd=0.0, num_turns=0, is_error=True,
                        is_plan_limit=True, plan_limit_kind="cap")

    async def fake_sleep(s):
        sleeps.append(s)

    orig_run_agent = agent_mod.run_agent
    orig_sleep = asyncio.sleep
    agent_mod.run_agent = fake_run_agent
    asyncio.sleep = fake_sleep
    try:
        res = asyncio.run(agent_mod.run_agent_with_fallback(
            "p", _Opts("claude-sonnet-5"), tag="builder"))
    finally:
        agent_mod.run_agent = orig_run_agent
        asyncio.sleep = orig_sleep

    sonnet_calls = [c for c in calls if "sonnet" in c.lower()]
    opus_calls = [c for c in calls if "opus" in c.lower()]

    chk("all backoff retries exhausted before escalating (initial + 2 retries = 3 Sonnet calls)",
        len(sonnet_calls) == 3, calls)
    chk("2 backoff sleeps between the 3 Sonnet attempts", len(sleeps) == 2, sleeps)
    chk("escalates into the cap path: Opus probe invoked exactly once", len(opus_calls) == 1, calls)
    chk("Opus also limited → surfaces is_plan_limit True (EU-82 pause)",
        res is not None and res.is_plan_limit, res)


def main():
    print("=" * 70)
    print("EU-210: transient 429 vs weekly-cap misclassification")
    print("=" * 70)

    test_retry_constants()
    test_transient_then_clean_retry_no_opus_probe()
    test_explicit_cap_language_classifies_as_cap()
    test_cap_result_takes_opus_probe_path_not_transient_retry()
    test_transient_exhausted_escalates_to_opus_probe_then_all_models_cap()

    print()
    passed = sum(1 for _, ok, _ in results if ok)
    for n, ok, det in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
    print("-" * 70)
    print(f"  {passed}/{len(results)} passed")
    print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
