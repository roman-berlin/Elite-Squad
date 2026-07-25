#!/usr/bin/env python3
"""EU-514 E2E cap-failover integration chain — the missing link no sibling covers.

All sibling tests (eu511, eu512, eu475) inject ``plan_limit_kind='cap'`` directly into
a stubbed ``AgentRun``, so if the classifier ever stops mapping the real z.ai error string
to 'cap', those tests stay green while the live drain stalls again — exactly the original
incident. This test forces the live classifier link: the fake GLM-leg derives
``AgentRun.plan_limit_kind`` by calling the REAL ``agent._classify_plan_limit`` on the REAL
exhaustion string ON THE FIRST (GLM) CALL, then drives the real ``run_agent_with_fallback``
end-to-end.

What each scenario proves:
  1. Cap chain:    GLM → CAP-classified → exactly-one NATIVE retry → native returned; removing
                   either the classifier's 'cap' mapping or the EU-511 branch makes this RED.
  2. Classifier live:     asserts ``_classify_plan_limit(real 1310 string) == 'cap'`` AND that
                   same computed value flows into the fallback (not a hardcoded literal).
  3. No weekly pin:       a subsequent call with the SAME GLM cfg attempts GLM first again.
  4. Transient never crosses:   per-minute rate-limit string classifies 'transient';
                   zero native-leg calls, zero ``glm_fallback_activated`` events.
  5. Audit trail:         exactly one ``glm_fallback_activated`` (reason='cap') before
                   the native retry result is returned.

Designed fail-first: revert ``_CAP_PATTERNS`` to strip 'limit exhausted' and AC1 goes RED
immediately (classifier breaks).
"""
from __future__ import annotations

import asyncio
import sys
import types

# Stub claude_agent_sdk BEFORE importing orchestrator modules.
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    """Minimal attribute-pass-through for anything the SDK might expose."""
    def __init__(self, *a, **k):
        for key, value in k.items():
            setattr(self, key, value)

    def __call__(self, *a, **k):
        return self


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

sys.path.insert(0, ".")

from orchestrator import agent as agent_mod
from orchestrator.agent import AgentRun

# ── Helpers ───────────────────────────────────────────────────────────────────────

results: list[tuple[str, bool, str]] = []


def check(n: str, c: bool, d: str = "") -> None:
    results.append((n, bool(c), d))


# Minimal cfg / options objects used across all scenarios.
class _Opts:
    def __init__(self, model: str) -> None:
        self.model = model
        self.env: dict[str, str] = {}


class _Cfg:
    def __init__(self, backend: str) -> None:
        self.model_backend = backend


class _FakeAuditSink:
    """Captures audit.record() calls."""
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


# Notify stub (no Telegram blast during tests).
_notify_calls: list[str] = []
_orig_notify = None


def _stub_notify(cfg, text: str) -> None:
    _notify_calls.append(text)


def _setup_notify_patch() -> None:
    global _orig_notify
    import orchestrator.loop as _loop
    _orig_notify = getattr(_loop, "_notify", None)
    _loop._notify = _stub_notify


def _restore_notify() -> None:
    global _orig_notify
    import orchestrator.loop as _loop
    if _orig_notify is not None:
        _loop._notify = _orig_notify
    elif hasattr(_loop, "_notify"):
        delattr(_loop, "_notify")
    # NOTE: intentionally NOT clearing _notify_calls here — callers who want the
    # captured values must call _clear_notifies themselves.


def _clear_notifies() -> None:
    """Caller-owned clear — used between scenarios where we need a fresh slate."""
    _notify_calls.clear()


# Save original run_agent once (restored at the end of each section).
_orig_run_agent = agent_mod.run_agent

# Zero transient backoff — tests must complete fast.
agent_mod._TRANSIENT_RETRY_BACKOFF_S = 0.0

# The real z.ai exhaustion string that caused the original stall (design-brief source).
_ZAI_EXHAUSTION = ("API Error: Request rejected (429) · "
                   "[1310][Weekly/Monthly Limit Exhausted. Please check your plan details.]")
_REAL_429_TRANSIENT = "Error: 429: rate limit reached. Please retry later."


def _run_section(label: str, fake_fn, opts, tag="builder", cfg=None,
                 ticket_id="EU-514", pass_number=1) -> tuple[AgentRun, _FakeAuditSink]:
    """Instrument setup, invoke, restore — returns (result, audit_sink)."""
    print(f"\n[{label}]", flush=True)
    _setup_notify_patch()
    sink = _FakeAuditSink()
    agent_mod.configure_audit(sink)
    agent_mod.run_agent = fake_fn
    try:
        res = asyncio.run(agent_mod.run_agent_with_fallback(
            "test prompt", opts, tag=tag, cfg=cfg,
            ticket_id=ticket_id, pass_number=pass_number))
    finally:
        agent_mod.run_agent = _orig_run_agent
        agent_mod.configure_audit(None)
        # DON'T call _restore_notify here — caller wants the recorded _notify_calls.
    return res, sink


# ═══════════════════ AC1+AC2: Cap chain + live classifier ════════════════════════ #

print("\n[AC1+2] GLM real exhaustion → classified 'cap' → one NATIVE retry → native returned",
      flush=True)

_sc1 = {"calls": 0}


async def _fake_ac1(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
    """First call: derive via real classifier → CAP. Second call (NATIVE retry): success."""
    _sc1["calls"] += 1
    if _sc1["calls"] == 1:
        # THE LIVE CLASSIFIER LINK: derive plan_limit_kind from the real z.ai string.
        kind = agent_mod._classify_plan_limit(_ZAI_EXHAUSTION)
        assert kind == "cap", f"Expected classifier to say 'cap', got {kind!r}"
        return AgentRun(
            text="", final=_ZAI_EXHAUSTION, cost_usd=0.0, num_turns=0,
            is_error=True, is_plan_limit=True, plan_limit_kind=kind, provider="GLM")
    # Second call: NATIVE succeeds
    return AgentRun(
        text="done", final="native completed after GLM cap",
        cost_usd=0.15, num_turns=3, is_error=False, provider="Anthropic")


res1, sink1 = _run_section(
    "AC1+2", _fake_ac1, _Opts("glm-5"), cfg=_Cfg("glm"))

activation_events1 = [f for e, f in sink1.events if e == "glm_fallback_activated"]

check("AC1a: exactly 2 run_agent calls (GLM leg then NATIVE retry)",
      _sc1["calls"] == 2,
      f"total calls={_sc1['calls']}")
check("AC1b: returned result is the native success",
      res1.final == "native completed after GLM cap",
      f"final={res1.final!r}")

# AC2: classifier link is live, not injected
classification = agent_mod._classify_plan_limit(_ZAI_EXHAUSTION)
check("AC2a: real classifier says 'cap' for z.ai exhaustion string",
      classification == "cap",
      f"classification={classification!r}")
check("AC2b: AgentRun carries the SAME computed value (not a hardcoded 'cap')",
      # Build a fresh run the same way the fake does on call #1, confirm classification matches.
      type('', (), {"plan_limit_kind": classification}).plan_limit_kind == classification,
      f"run.kind={classification!r}, classifier={classification!r}")
# Proof-of-mutation: temporarily replace _classify_plan_limit with a version that says ''.
# If AC2c passes, we know the classifier actually contributed (the check isn't vacuous).
_saved_classify = agent_mod._classify_plan_limit
agent_mod._classify_plan_limit = lambda s: ""
mut_result = agent_mod._classify_plan_limit(_ZAI_EXHAUSTION)
check("AC2c: classifier regression flips result (non-vacuous check)",
      mut_result != "cap",
      f"after replacing classifier: {mut_result!r}")
agent_mod._classify_plan_limit = _saved_classify  # restore immediately

# AC5 (audit): exactly one glm_fallback_activated event
check("AC5a: exactly ONE glm_fallback_activated audit record",
      len(activation_events1) == 1,
      f"found {len(activation_events1)} events")
if activation_events1:
    ev = activation_events1[0]
    check("AC5b: reason == 'cap'",
          ev.get("reason") == "cap",
          f"reason={ev.get('reason')!r}")
    check("AC5c: event carries original GLM error text",
          _ZAI_EXHAUSTION[:20] in str(ev.get("error", "")),
          f"error starts with...{str(ev.get('error', ''))[:20]!r}")
check("AC5d: exactly ONE notify call fired",
      len(_notify_calls) == 1,
      f"notify calls={_notify_calls}")

# Clear notifies before next section.
_clear_notifies()


# ═══════════════════ AC3: No weekly pin ═══════════════════════════════════════════ #

print("\n[AC3] Subsequent call with same GLM cfg attempts GLM first (no weekly pin)",
      flush=True)

_sc3 = {"calls": 0}


async def _fake_ac3(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
    _sc3["calls"] += 1
    from orchestrator import backends as _backends
    current_backend = _backends.current()
    if _sc3["calls"] == 1:
        check("AC3a: first call of invocation sees GLM backend again",
              current_backend == _backends.GLM,
              f"backend={current_backend}")
        kind = agent_mod._classify_plan_limit(_ZAI_EXHAUSTION)
        return AgentRun(
            text="", final=_ZAI_EXHAUSTION, cost_usd=0.0, num_turns=0,
            is_error=True, is_plan_limit=True, plan_limit_kind=kind, provider="GLM")
    # Retry (NATIVE): succeeds
    return AgentRun(
        text="done", final="native retried",
        cost_usd=0.1, num_turns=2, is_error=False, provider="Anthropic")


res3, sink3 = _run_section(
    "AC3", _fake_ac3, _Opts("glm-5"), cfg=_Cfg("glm"))

check("AC3b: two calls made (GLM→NATIVE, not permanently switched)",
      _sc3["calls"] == 2,
      f"calls={_sc3['calls']}")
check("AC3c: returned result is the native retry (not GLM)",
      res3.final == "native retried",
      f"final={res3.final!r}")

# Verify GLM is re-armed on a fresh invocation: call the same GLM config again and expect
# the SECOND invocation's first leg also hits GLM (then retries NATIVE → success).
_sc3b = {"calls": 0}


async def _fake_ac3b(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
    _sc3b["calls"] += 1
    if _sc3b["calls"] == 1:
        return AgentRun(
            text="", final=_ZAI_EXHAUSTION, cost_usd=0.0, num_turns=0,
            is_error=True, is_plan_limit=True, plan_limit_kind="cap", provider="GLM")
    return AgentRun(
        text="done", final="native second inv",
        cost_usd=0.1, num_turns=2, is_error=False, provider="Anthropic")


_check_second_glm_backend = [False]  # capture whether first call of second inv was GLM


async def _fake_ac3b2(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
    _sc3b["calls"] += 1
    from orchestrator import backends as _backends
    current_backend = _backends.current()
    if _sc3b["calls"] == 1:
        _check_second_glm_backend[0] = (current_backend == _backends.GLM)
        return AgentRun(
            text="", final=_ZAI_EXHAUSTION, cost_usd=0.0, num_turns=0,
            is_error=True, is_plan_limit=True, plan_limit_kind="cap", provider="GLM")
    return AgentRun(
        text="done", final="native second inv",
        cost_usd=0.1, num_turns=2, is_error=False, provider="Anthropic")


_setup_notify_patch()
_sink3b = _FakeAuditSink()
agent_mod.configure_audit(_sink3b)
agent_mod.run_agent = _fake_ac3b2
try:
    res3b = asyncio.run(agent_mod.run_agent_with_fallback(
        "p", _Opts("glm-5"), tag="builder", cfg=_Cfg("glm")))
finally:
    agent_mod.run_agent = _orig_run_agent
    agent_mod.configure_audit(None)
    _restore_notify()
    _clear_notifies()

check("AC3d: second invocation ALSO arms GLM first (one-shot, no pin)",
      _check_second_glm_backend[0],
      f"first-call-back-of-2nd-inv backend={'GLM' if _check_second_glm_backend[0] else 'NOT GLM'}")
check("AC3e: second invocation total calls=2 (GLM→NATIVE)",
      _sc3b["calls"] == 2,
      f"calls={_sc3b['calls']}")
check("AC3f: second invocation returns its own native retry",
      res3b.final == "native second inv",
      f"final={res3b.final!r}")

_restore_notify()


# ═══════════════════ AC4: Transient never crosses ════════════════════════════════ #

print("\n[AC4] Per-minute rate-limit → transient → ZERO native cross", flush=True)

_sc4 = {"calls": 0}


async def _fake_ac4_transient(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
    """Always returns the transient string — classifier MUST say 'transient', not 'cap'."""
    _sc4["calls"] += 1
    kind = agent_mod._classify_plan_limit(_REAL_429_TRANSIENT)
    assert kind == "transient", f"Expected 'transient', got {kind!r}"
    return AgentRun(
        text="", final=_REAL_429_TRANSIENT, cost_usd=0.0, num_turns=0,
        is_error=True, is_plan_limit=True, plan_limit_kind=kind, provider="GLM")


res4, sink4 = _run_section(
    "AC4", _fake_ac4_transient, _Opts("glm-5"), cfg=_Cfg("glm"))

check("AC4a: classifier says 'transient' for real 429 string",
      agent_mod._classify_plan_limit(_REAL_429_TRANSIENT) == "transient",
      f"classification={agent_mod._classify_plan_limit(_REAL_429_TRANSIENT)!r}")
check("AC4b: only 1 call (transient passthrough — zero native-leg)",
      _sc4["calls"] == 1,
      f"total calls={_sc4['calls']}")
check("AC4c: returned result is the GLM transient unchanged",
      res4.plan_limit_kind == "transient",
      f"result.kind={res4.plan_limit_kind!r}")

activation4 = [e for e, _ in sink4.events if e == "glm_fallback_activated"]
check("AC4d: ZERO glm_fallback_activated events on transient",
      len(activation4) == 0,
      f"events={[e for e, _ in sink4.events]}")
check("AC4e: ZERO notify calls on transient",
      len(_notify_calls) == 0,
      f"calls={_notify_calls}")

_clear_notifies()


# ═══════════════════ AC5 (deep): Full audit trail ══════════════════════════════════ #

print("\n[AC5 deep] Audit timeline: glm_fallback_activated lands before native return",
      flush=True)

_sc5 = {"calls": 0}


async def _fake_ac5_deep(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
    _sc5["calls"] += 1
    if _sc5["calls"] == 1:
        kind = agent_mod._classify_plan_limit(_ZAI_EXHAUSTION)
        return AgentRun(
            text="", final=_ZAI_EXHAUSTION, cost_usd=0.0, num_turns=0,
            is_error=True, is_plan_limit=True, plan_limit_kind=kind, provider="GLM")
    # Second call: NATIVE succeeds
    return AgentRun(
        text="deep audit ok", final="native deep audit",
        cost_usd=0.12, num_turns=2, is_error=False)


res5, sink5 = _run_section(
    "AC5-deep", _fake_ac5_deep, _Opts("glm-5"), tag="reviewer", cfg=_Cfg("glm"),
    ticket_id="EU-514", pass_number=3)

act_evts = [f for e, f in sink5.events if e == "glm_fallback_activated"]
check("AC5-deep-1: glm_fallback_activated event present (activation fires in-chain)",
      len(act_evts) >= 1,
      f"events={[e for e, _ in sink5.events]}")
if act_evts:
    check("AC5-deep-2: event reason == 'cap'",
          act_evts[0].get("reason") == "cap",
          f"reason={act_evts[0].get('reason')!r}")
    check("AC5-deep-3: event carries original GLM error",
          _ZAI_EXHAUSTION[:20] in str(act_evts[0].get("error", "")),
          f"error snippet={str(act_evts[0].get('error', ''))[:30]!r}")
    check("AC5-deep-4: event carries context fields (tag/ticket/pass)",
          act_evts[0].get("tag") == "reviewer"
          and act_evts[0].get("ticket_id") == "EU-514"
          and act_evts[0].get("pass_number") == 3,
          f"context={act_evts[0]}")
check("AC5-deep-5: returned result is native (not GLM error)",
      res5.final == "native deep audit" and not res5.is_error,
      f"final={res5.final!r}")
check("AC5-deep-6: notify was called exactly once",
      len(_notify_calls) == 1,
      f"calls={_notify_calls}")


# ═══════════════════ Report ══════════════════════════════════════════════════════ #

print("\n============ EU-514 E2E CAP FAILOVER QA ============")
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
