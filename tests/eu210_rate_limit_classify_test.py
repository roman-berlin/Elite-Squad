#!/usr/bin/env python3
"""EU-210 — a transient rate-limit / 429 / overload must NOT be misclassified as a weekly cap.

The pre-EU-210 code kept the generic terms "limit reached" / "over limit" in `_CAP_PATTERNS`, so a
transient message like "rate limit reached" matched a cap pattern (via the substring "limit reached")
and took the immediate Opus-probe / weekly-cap path — the primary false-alarm defect from the audit.

This harness pins the fix on two levels:
  1. `_classify_plan_limit` — transient status/rate-limit language → "transient"; only EXPLICIT
     plan/weekly/usage/quota language → "cap".
  2. `run_agent_with_fallback` — a transient plan-limit flows through the backoff-retry loop (retry
     the SAME Sonnet model), never the Opus probe; a genuine cap still takes the Opus probe.

Written fail-first: checks 1a/1b (rate-limit-reached and the routing test) go RED against the
un-tightened patterns, because "rate limit reached" classified as "cap" and hit the Opus leg.
"""
import sys, types, asyncio

REPO = "."
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, REPO)

from orchestrator import agent as agent_mod
from orchestrator.agent import _classify_plan_limit, AgentRun

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))


# ── 1. _classify_plan_limit: transient language must classify as "transient", not "cap" ──────────
# These are the exact strings called out in the EU-210 iteration-2 review.
for msg in ("rate limit reached", "429 Too Many Requests", "overloaded",
            "Error: 529 overloaded_error", "rate_limit_error"):
    kind = _classify_plan_limit(msg)
    check(f"'{msg}' → transient (not cap)", kind == "transient", f"got {kind!r}")

# ── 2. genuine cap language still classifies as "cap" (no regression) ─────────────────────────────
for msg in ("Claude usage limit reached. Your limit will reset at 9am.",
            "You have hit your weekly limit",
            "plan limit exceeded",
            "quota exceeded for this billing cycle",
            "insufficient credit / balance"):
    kind = _classify_plan_limit(msg)
    check(f"'{msg[:32]}...' → cap", kind == "cap", f"got {kind!r}")

# a cap error that ALSO carries a 429 status must still win as "cap" (cap-first ordering preserved)
check("'429: usage limit reached' → cap (cap outranks the transient 429)",
      _classify_plan_limit("429: usage limit reached") == "cap")

# a non-limit error is neither
check("'connection refused' → '' (not a plan-limit error)",
      _classify_plan_limit("connection refused") == "")


# ── 3. run_agent_with_fallback routing: transient → backoff retry loop; cap → Opus probe ─────────
class _Opts:
    def __init__(self, model): self.model = model

_orig = agent_mod.run_agent
_orig_backoff = agent_mod._TRANSIENT_RETRY_BACKOFF_S
agent_mod._TRANSIENT_RETRY_BACKOFF_S = 0.0   # don't actually sleep in the test

def _make_fake(first_err, retry_ok=True):
    """Fake run_agent whose first Sonnet call surfaces the RAW error string `first_err`, classified
    through the real `_classify_plan_limit` — so the fallback's transient-vs-cap branch depends on
    the actual classifier under test (ties point-3 routing to the classification fix). Any later
    call (the backoff retry, still Sonnet, or the Opus probe) succeeds when retry_ok."""
    seen = {"n": 0}
    def fake(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
        async def _run():
            seen["n"] += 1
            model = getattr(options, "model", "")
            calls.append(model)
            if seen["n"] == 1:
                kind = _classify_plan_limit(first_err)
                return AgentRun(text="", final="", cost_usd=0.0, num_turns=0, is_error=True,
                                is_plan_limit=bool(kind), plan_limit_kind=kind)
            return AgentRun(text="ok", final="ok", cost_usd=0.1, num_turns=1,
                            is_error=not retry_ok)
        return _run()
    return fake

# (a) a transient "rate limit reached" → the second call is a RETRY on the SAME Sonnet model, never
# Opus. Against the un-tightened patterns this string classified as "cap" and hit the Opus leg.
calls: list[str] = []
agent_mod.run_agent = _make_fake("rate limit reached")
try:
    res = asyncio.run(agent_mod.run_agent_with_fallback("p", _Opts("claude-sonnet-5"), tag="builder"))
finally:
    agent_mod.run_agent = _orig
check("transient plan-limit → backoff-retry loop (2 Sonnet calls, no Opus)",
      len(calls) == 2 and all("sonnet" in m.lower() for m in calls), str(calls))
check("transient retry succeeded → returns the retry result (no weekly-cap surfaced)",
      res is not None and not res.is_plan_limit and res.final == "ok")

# (b) a genuine cap "usage limit reached" → the second call is the OPUS probe, not a Sonnet retry
calls = []
agent_mod.run_agent = _make_fake("Claude usage limit reached")
try:
    res2 = asyncio.run(agent_mod.run_agent_with_fallback("p", _Opts("claude-sonnet-5"), tag="builder"))
finally:
    agent_mod.run_agent = _orig
    agent_mod._TRANSIENT_RETRY_BACKOFF_S = _orig_backoff
check("cap plan-limit → Opus probe path (2nd call is Opus, not a Sonnet retry)",
      len(calls) == 2 and "opus" in calls[1].lower(), str(calls))


# 2026-07-24: the z.ai/GLM HARD weekly cap must classify as "cap", not "transient". The live error
# "[1310][Weekly/Monthly Limit Exhausted…]" contains "429" (transient) but the cap set had only
# "weekly limit" — and "weekly/monthly limit" is not a superstring of it — so a 3-day exhaustion was
# retried as a per-minute blip and the hybrid symmetric fallback never fired.
_GLM_EXHAUST = ("API Error: Request rejected (429) · [1310][Weekly/Monthly Limit Exhausted. "
                "Your limit will reset at 2026-07-27 19:44:02]")
check("GLM weekly/monthly exhaustion classifies as CAP (not transient)",
      _classify_plan_limit(_GLM_EXHAUST) == "cap", _classify_plan_limit(_GLM_EXHAUST))
check("a bare monthly-limit cap is recognised",
      _classify_plan_limit("monthly limit reached") == "cap")
check("'limit exhausted' is a cap phrase",
      _classify_plan_limit("your limit exhausted") == "cap")
check("a plain transient 429 is STILL transient (no false cap)",
      _classify_plan_limit("429: rate limit reached") == "transient")
check("'too many requests' is STILL transient",
      _classify_plan_limit("too many requests (429)") == "transient")

print("\n============ EU-210 RATE-LIMIT CLASSIFY QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
