#!/usr/bin/env python3
"""EU-511: cap-only one-shot GLM → main-model retry in run_agent_with_fallback.

When a GLM run hits a HARD plan-limit cap (plan_limit_kind == "cap"), retry
that SAME unit of work exactly once on the native/main backend — mirroring
the existing Sonnet→Opus one-shot pattern already in this function.

Covered scenarios (all stubbing run_agent, no network):
  1. Cap→success:    GLM capped, native retry succeeds → return retry result (call #2);
                     the audit sink MUST record glm_cap_failover_activated (reason="cap",
                     model bound) — verified, not just wired (EU-511 review)
  1b. Empty tag:     same success path with tag="" — no UnboundLocalError on the print/audit
                     path (regression guard for the hoisted `model` binding)
  2. Transient:      GLM plan-limit kind != "cap" ("" or "transient") → ORIGINAL result,
                     no native retry (call count == 1)
  3a. Clean success: GLM non-cap success → ORIGINAL result (no-op regression)
  3b. Plain error:   GLM plain error → ORIGINAL result (no-op regression)
  4. All-capped:     GLM capped, native retry ALSO plan-limited → ORIGINAL GLM result
  5. Broken retry:   GLM capped, native retry fails (non-cap error) → ORIGINAL GLM result
  6. Stateless + suite: second invocation still starts on GLM, run_all.py exits 0
"""
from __future__ import annotations

import asyncio
import sys
import types

# Stub the Agent SDK (imported transitively) so import never needs a real model / network.
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        for key, value in k.items():
            setattr(self, key, value)
    def __call__(s, *a, **k):
        return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import agent as agent_mod
from orchestrator.agent import AgentRun

results: list[tuple[str, bool, str]] = []


def check(n: str, c: bool, d: str = "") -> None:
    results.append((n, bool(c), d))


# ── Stubs ───────────────────────────────────────────────────────────────────────


class _Opts:
    def __init__(self, model: str) -> None:
        self.model = model
        self.env: dict[str, str] = {}


class _Cfg:
    """Minimal cfg with model_backend attribute."""
    def __init__(self, backend: str) -> None:
        self.model_backend = backend


class _FakeAuditSink:
    """Captures audit.record() calls for inspection."""
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


# Save originals to restore after each test section
_orig_run_agent = agent_mod.run_agent


# ═══════════════════════════════ Scenario 1: GLM cap → native succeeds ══════════════ #
print("\n[SCENARIO 1] GLM cap → native retry succeeds", flush=True)

scenarios = {"calls": 0, "glm_call": 0, "native_call": 0}


async def _fake_scenario1(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
    scenarios["calls"] += 1
    n = scenarios["calls"]

    if n == 1:
        # Call 1: GLM run with cap exhaustion
        # Verify backend was set to GLM inside run_agent_with_fallback
        from orchestrator import backends as _backends
        check("scenario1 call #1: backend is GLM",
              _backends.current() == _backends.GLM,
              f"backend={_backends.current()}")
        return AgentRun(text="", final="Error: usage limit reached", cost_usd=0.0,
                        num_turns=0, is_error=True, is_plan_limit=True,
                        plan_limit_kind="cap", provider="GLM", model_version="glm-4")

    # Call 2: Native retry — should succeed
    from orchestrator import backends as _backends
    check("scenario1 call #2: backend is NATIVE",
          _backends.current() == _backends.NATIVE,
          f"backend={_backends.current()}")
    check("scenario1 call #2: options.env NOT z.ai (pre-GLM snapshot)",
          "ZAI_BASE_URL" not in str(options.env),
          f"env keys={list(options.env.keys())}")
    check("scenario1 call #2: original model preserved ('claude-sonnet-4-1-20250620')",
          getattr(options, "model", "") == "claude-sonnet-4-1-20250620",
          f"model={getattr(options, 'model', '')}")
    return AgentRun(text="done", final="native model completed the task",
                    cost_usd=0.15, num_turns=3, is_error=False,
                    provider="Anthropic", model_version="opus-4-1-20250620")


sink1 = _FakeAuditSink()
agent_mod.configure_audit(sink1)
agent_mod.run_agent = _fake_scenario1

try:
    res1 = asyncio.run(agent_mod.run_agent_with_fallback(
        "test prompt", _Opts("claude-sonnet-4-1-20250620"), tag="builder",
        cfg=_Cfg("glm"), ticket_id="EU-511", pass_number=1))
finally:
    agent_mod.run_agent = _orig_run_agent
    agent_mod.configure_audit(None)

check("scenario1: exactly 2 calls made", scenarios["calls"] == 2,
      f"total calls={scenarios['calls']}")
check("scenario1: returns retry result (not GLM result)",
      res1.final == "native model completed the task",
      f"final='{res1.final}'")
check("scenario1: retry result is NOT plan-limited",
      not res1.is_plan_limit,
      f"is_plan_limit={res1.is_plan_limit}")

# EU-511 review: the instrumentation must be VERIFIED, not just wired. Before the `model`
# binding was hoisted above the GLM branch, record() raised UnboundLocalError on `model=model`
# — silently swallowed by the best-effort except — so this event NEVER landed in the sink.
# Assert it lands now, with reason="cap" and the configured model bound (the hoist fix).
_failover1 = [f for e, f in sink1.events if e == "glm_cap_failover_activated"]
check("scenario1: audit sink recorded exactly one glm_cap_failover_activated",
      len(_failover1) == 1,
      f"events={[e for e, _ in sink1.events]}")
check("scenario1: failover event reason='cap' and model bound (hoist fix)",
      bool(_failover1)
      and _failover1[0].get("reason") == "cap"
      and _failover1[0].get("model") == "claude-sonnet-4-1-20250620",
      f"fields={_failover1[0] if _failover1 else 'none'}")
check("scenario1: failover event carries tag/ticket/pass context + original GLM error",
      bool(_failover1)
      and _failover1[0].get("tag") == "builder"
      and _failover1[0].get("ticket_id") == "EU-511"
      and _failover1[0].get("pass_number") == 1
      and _failover1[0].get("error") == "Error: usage limit reached",
      f"fields={_failover1[0] if _failover1 else 'none'}")


# ═══════════════════ Scenario 1b: cap → native succeeds, EMPTY tag ═════════════════ #
# The success-path print interpolates `{tag or model}` — with the `model` binding hoisted
# below the GLM block (pre-fix), an empty-tag caller hit UnboundLocalError right after the
# retry succeeded. The hoist must make this path clean, and the audit record (tag="") must
# still land with the model bound.
print("\n[SCENARIO 1b] GLM cap → native succeeds with EMPTY tag (no UnboundLocalError)", flush=True)

_sc1b = {"calls": 0}


async def _fake_sc1b(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
    _sc1b["calls"] += 1
    if _sc1b["calls"] == 1:
        return AgentRun(text="", final="Error: usage limit reached", cost_usd=0.0,
                        num_turns=0, is_error=True, is_plan_limit=True,
                        plan_limit_kind="cap", provider="GLM", model_version="glm-4")
    return AgentRun(text="done", final="native completed (empty-tag caller)",
                    cost_usd=0.1, num_turns=1, is_error=False,
                    provider="Anthropic", model_version="opus-4-1-20250620")


sink1b = _FakeAuditSink()
agent_mod.configure_audit(sink1b)
agent_mod.run_agent = _fake_sc1b
_exc1b: Exception | None = None
res1b = None
try:
    res1b = asyncio.run(agent_mod.run_agent_with_fallback(
        "p", _Opts("claude-sonnet-4-1-20250620"), tag="",
        cfg=_Cfg("glm")))
except Exception as exc:  # noqa: BLE001 — pre-fix this path raised UnboundLocalError after
    _exc1b = exc          # the retry had already succeeded; record it as a clean FAIL.
finally:
    agent_mod.run_agent = _orig_run_agent
    agent_mod.configure_audit(None)

check("scenario1b: empty-tag success path does NOT raise (pre-fix: UnboundLocalError)",
      _exc1b is None,
      f"raised={_exc1b!r}")
check("scenario1b: empty-tag success path returns retry result",
      res1b is not None and res1b.final == "native completed (empty-tag caller)",
      f"final={getattr(res1b, 'final', None)!r}")
_failover1b = [f for e, f in sink1b.events if e == "glm_cap_failover_activated"]
check("scenario1b: audit event still lands with model bound (print path safe)",
      len(_failover1b) == 1
      and _failover1b[0].get("model") == "claude-sonnet-4-1-20250620"
      and _failover1b[0].get("tag") == "",
      f"events={sink1b.events}")


# ═══════════════════════════════ Scenario 2: Transient → NO native retry ══════════ #
print("\n[SCENARIO 2] GLM transient → no native retry", flush=True)

for kind_name, kind_value in [("", "empty string"), ("transient", "'transient'")]:
    print(f"  ── kind={kind_value!r} ──", flush=True)
    _sc2 = {"calls": 0}

    async def _fake_sc2(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
        _sc2["calls"] += 1
        return AgentRun(text="", final=f"Error: {kind_value}", cost_usd=0.0,
                        num_turns=0, is_error=True, is_plan_limit=True,
                        plan_limit_kind=kind_value, provider="GLM")

    agent_mod.run_agent = _fake_sc2
    try:
        res2 = asyncio.run(agent_mod.run_agent_with_fallback(
            "p", _Opts("claude-sonnet-4-1-20250620"), tag="builder",
            cfg=_Cfg("glm")))
    finally:
        agent_mod.run_agent = _orig_run_agent

    check(f"scenario2 ({kind_value}): exactly 1 call (no native retry)",
          _sc2["calls"] == 1,
          f"calls={_sc2['calls']}")
    check(f"scenario2 ({kind_value}): returns original GLM result",
          res2.plan_limit_kind == kind_value,
          f"result.plan_limit_kind={res2.plan_limit_kind}")


# ═══════════════ Scenario 3a: GLM clean success → no-op ════════════════════════════ #
print("\n[SCENARIO 3a] GLM clean success → no-op (byte-identical)", flush=True)

_sc3a = {"calls": 0}


async def _fake_sc3a(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
    _sc3a["calls"] += 1
    return AgentRun(text="glm output", final="GLM did the job fine",
                    cost_usd=0.05, num_turns=2, is_error=False,
                    provider="GLM", model_version="glm-4")


agent_mod.run_agent = _fake_sc3a
try:
    res3a = asyncio.run(agent_mod.run_agent_with_fallback(
        "p", _Opts("claude-sonnet-4-1-20250620"), tag="builder",
        cfg=_Cfg("glm")))
finally:
    agent_mod.run_agent = _orig_run_agent

check("scenario3a: exactly 1 call", _sc3a["calls"] == 1,
      f"calls={_sc3a['calls']}")
check("scenario3a: returns original GLM success",
      res3a.final == "GLM did the job fine",
      f"final='{res3a.final}'")


# ═══════════════ Scenario 3b: GLM plain error → no-op ══════════════════════════════ #
print("\n[SCENARIO 3b] GLM plain error → no-op (byte-identical)", flush=True)

_sc3b = {"calls": 0}


async def _fake_sc3b(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
    _sc3b["calls"] += 1
    return AgentRun(text="", final="network timeout", cost_usd=0.0,
                    num_turns=0, is_error=True, is_plan_limit=False,
                    provider="GLM", model_version="glm-4")


agent_mod.run_agent = _fake_sc3b
try:
    res3b = asyncio.run(agent_mod.run_agent_with_fallback(
        "p", _Opts("claude-sonnet-4-1-20250620"), tag="builder",
        cfg=_Cfg("glm")))
finally:
    agent_mod.run_agent = _orig_run_agent

check("scenario3b: exactly 1 call", _sc3b["calls"] == 1,
      f"calls={_sc3b['calls']}")
check("scenario3b: returns original GLM error",
      res3b.final == "network timeout",
      f"final='{res3b.final}'")


# ═══════════════════════════ Scenario 4: All-models capped ══════════════════════════ #
print("\n[SCENARIO 4] Both GLM and native capped → return ORIGINAL GLM result", flush=True)

_sc4 = {"calls": 0}
_glm_result_4 = {}


async def _fake_sc4(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
    _sc4["calls"] += 1
    if _sc4["calls"] == 1:
        # Store the GLM result for identity comparison
        _glm_result_4["obj"] = AgentRun(text="", final="Error: usage limit reached", cost_usd=0.0,
                                        num_turns=0, is_error=True, is_plan_limit=True,
                                        plan_limit_kind="cap", provider="GLM")
        return _glm_result_4["obj"]
    # Native retry also capped — DIFFERENT object
    return AgentRun(text="", final="Error: Insufficient credits", cost_usd=0.0,
                    num_turns=0, is_error=True, is_plan_limit=True,
                    plan_limit_kind="cap", provider="Anthropic")


agent_mod.run_agent = _fake_sc4
try:
    res4 = asyncio.run(agent_mod.run_agent_with_fallback(
        "p", _Opts("claude-sonnet-4-1-20250620"), tag="builder",
        cfg=_Cfg("glm")))
finally:
    agent_mod.run_agent = _orig_run_agent

check("scenario4: exactly 2 calls made (GLM + native retry)",
      _sc4["calls"] == 2,
      f"calls={_sc4['calls']}")
check("scenario4: returns ORIGINAL GLM result (identity same)",
      res4 is _glm_result_4.get("obj"),
      f"id(result)={id(res4)}, id(glm_obj)={id(_glm_result_4.get('obj'))}")
check("scenario4: result carries original GLM cap signal",
      res4.plan_limit_kind == "cap" and res4.provider == "GLM",
      f"plan_limit_kind={res4.plan_limit_kind}, provider={res4.provider}")


# ═══════════════════════════ Scenario 5: Broken native retry ════════════════════════ #
print("\n[SCENARIO 5] GLM capped, native broken → return ORIGINAL GLM result", flush=True)

_sc5 = {"calls": 0}
_glm_result_5 = {}


async def _fake_sc5(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
    _sc5["calls"] += 1
    if _sc5["calls"] == 1:
        _glm_result_5["obj"] = AgentRun(text="", final="Error: usage limit reached", cost_usd=0.0,
                                        num_turns=0, is_error=True, is_plan_limit=True,
                                        plan_limit_kind="cap", provider="GLM")
        return _glm_result_5["obj"]
    # Native retry fails for a non-plan-limit reason (auth/network)
    return AgentRun(text="", final="auth: bearer token rejected", cost_usd=0.0,
                    num_turns=0, is_error=True, is_plan_limit=False,
                    provider="Anthropic")


agent_mod.run_agent = _fake_sc5
try:
    res5 = asyncio.run(agent_mod.run_agent_with_fallback(
        "p", _Opts("claude-sonnet-4-1-20250620"), tag="builder",
        cfg=_Cfg("glm")))
finally:
    agent_mod.run_agent = _orig_run_agent

check("scenario5: exactly 2 calls made (GLM + native retry)",
      _sc5["calls"] == 2,
      f"calls={_sc5['calls']}")
check("scenario5: returns ORIGINAL GLM result (identity same)",
      res5 is _glm_result_5.get("obj"),
      f"id(result)={id(res5)}, id(glm_obj)={id(_glm_result_5.get('obj'))}")
check("scenario5: result carries original GLM cap signal",
      res5.plan_limit_kind == "cap" and res5.provider == "GLM",
      f"plan_limit_kind={res5.plan_limit_kind}, provider={res5.provider}")


# ═══════════════════════════ Scenario 6: Statelessness ═════════════════════════════ #
print("\n[SCENARIO 6] Stateless: second cap-failover call starts on GLM again", flush=True)

# Scenario 6: Two full invocations; each goes GLM(cap)→NATIVE(success).
# Verify statelessness: second invocation still sees GLM as its first-call backend.
_sc6 = {"calls": 0}


async def _fake_sc6(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
    _sc6["calls"] += 1
    from orchestrator import backends as _backends
    backend = _backends.current()
    n = _sc6["calls"]

    # Calls 1 & 3: GLM calls (first invocation #1, second invocation #1)
    if n in (1, 3):
        if n == 1:
            check("scenario6 call #1 (first invocation): backend is GLM",
                  backend == _backends.GLM,
                  f"backend={backend}, call #{n}")
        else:
            check("scenario6 call #3 (second invocation): backend is GLM again (stateless)",
                  backend == _backends.GLM,
                  f"backend={backend}, call #{n}")
        return AgentRun(text="", final="Error: usage limit reached", cost_usd=0.0,
                        num_turns=0, is_error=True, is_plan_limit=True,
                        plan_limit_kind="cap", provider="GLM")

    # Calls 2 & 4: NATIVE retries (both succeed)
    if n == 2:
        check("scenario6 call #2 (first invocation retry): backend is NATIVE",
              backend == _backends.NATIVE,
              f"backend={backend}, call #{n}")
    elif n == 4:
        check("scenario6 call #4 (second invocation retry): backend is NATIVE",
              backend == _backends.NATIVE,
              f"backend={backend}, call #{n}")
    return AgentRun(text="native ok", final="completed on main model",
                    cost_usd=0.1, num_turns=1, is_error=False)


agent_mod.run_agent = _fake_sc6
try:
    res6_first = asyncio.run(agent_mod.run_agent_with_fallback(
        "p", _Opts("claude-sonnet-4-1-20250620"), tag="builder",
        cfg=_Cfg("glm")))
    res6_second = asyncio.run(agent_mod.run_agent_with_fallback(
        "p", _Opts("claude-sonnet-4-1-20250620"), tag="builder",
        cfg=_Cfg("glm")))
finally:
    agent_mod.run_agent = _orig_run_agent

check("scenario6: total of 4 calls across 2 invocations",
      _sc6["calls"] == 4,
      f"total calls={_sc6['calls']}")
check("scenario6: first invocation returned native retry result",
      res6_first.final == "completed on main model",
      f"final='{res6_first.final}'")
check("scenario6: second invocation returns its own native retry result",
      res6_second.final == "completed on main model",
      f"final='{res6_second.final}'")


# ═══════════════════════════ Report ════════════════════════════════════════════════ #
print("\n============ EU-511 GLM CAP FAILOVER QA ============")
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{total} passed")
if passed < total:
    print(f"  RESULT: {total - passed} FAIL ❌")
    sys.exit(1)
else:
    print("  RESULT: ALL GREEN ✅")
    sys.exit(0)
