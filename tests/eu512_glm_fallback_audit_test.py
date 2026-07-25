#!/usr/bin/env python3
"""EU-512: GLM-cap -> main-model failover *activation* instrumentation.

Mirrors the Sonnet-cap activation block (EU-212, lines ~772-782 of agent.py)
for the GLM path added in EU-511. Before the native retry fires on a GLM cap,
the code MUST:
  1. Record a `glm_fallback_activated` audit event (best-effort, never raises).
  2. Send a Telegram notify via loop._notify (best-effort, never raises).

Fired once per activation -- on the cap branch only -- never on the transient
passthrough or the clean-success path.

KEY DECISION: does NOT rename or move the existing `glm_cap_failover_activated`
success-path record from EU-511; the two events are distinct (activation vs retry
succeeded) and both stay.

Written fail-first: against unmodified agent.py (no activation instrumentation)
checks AC1a-AC1b go RED because no `glm_fallback_activated` events land.
"""
from __future__ import annotations

import asyncio
import sys
import types

# Stub the Agent SDK (imported transitively) so import never needs a real model / network.
sdk = types.ModuleType("claude_agent_sdk")


class _D:
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


# ═══════════════════ AC1: Cap --> exactly ONE glm_fallback_activated + ONE notify ══ #
print("\n[AC1] GLM cap-classified -> exactly one glm_fallback_activated + one notify", flush=True)


async def _fake_ac1(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
    """GLM capped -> native retry succeeds (regardless, activations fire)."""
    if not hasattr(_fake_ac1, "_n"):
        _fake_ac1._n = 0
    _fake_ac1._n += 1
    n = _fake_ac1._n

    if n == 1:
        return AgentRun(text="", final="Error: usage limit reached", cost_usd=0.0,
                        num_turns=0, is_error=True, is_plan_limit=True,
                        plan_limit_kind="cap", provider="GLM")
    # Native retry succeeds
    return AgentRun(text="done", final="native completed",
                    cost_usd=0.15, num_turns=3, is_error=False,
                    provider="Anthropic")


class _RaiseNotify:
    """Stub that records calls but can also raise."""
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.raise_flag = False

    def __call__(self, cfg, text: str) -> None:
        self.calls.append(text)
        if self.raise_flag:
            raise RuntimeError("notify broke")


_notify_stub = _RaiseNotify()

# Monkey-patch orchestrator.loop._notify
import orchestrator.loop as _loop
_orig_notify = getattr(_loop, "_notify", None)
_loop._notify = _notify_stub

agent_mod.run_agent = _fake_ac1

sink1 = _FakeAuditSink()
agent_mod.configure_audit(sink1)
_fake_ac1._n = 0
try:
    res1 = asyncio.run(agent_mod.run_agent_with_fallback(
        "test prompt", _Opts("claude-sonnet-4-1-20250620"),
        tag="builder", cfg=_Cfg("glm"), ticket_id="EU-512", pass_number=2))
finally:
    agent_mod.run_agent = _orig_run_agent
    agent_mod.configure_audit(None)
    if _orig_notify is not None:
        _loop._notify = _orig_notify
    elif hasattr(_loop, "_notify"):
        delattr(_loop, "_notify")

activation_events1 = [f for e, f in sink1.events if e == "glm_fallback_activated"]
check("AC1a: exactly ONE glm_fallback_activated audit event",
      len(activation_events1) == 1,
      f"found {len(activation_events1)} events: {[e for e, _ in sink1.events]}")
if activation_events1:
    ev = activation_events1[0]
    check("AC1b: event reason == 'cap' (original plan_limit_kind)",
          ev.get("reason") == "cap",
          f"reason={ev.get('reason')!r}")
    check("AC1c: event error == original GLM error text",
          "usage limit" in str(ev.get("error", "")),
          f"error={ev.get('error')!r}")
    check("AC1d: event carries tag/ticket/pass context",
          ev.get("tag") == "builder"
          and ev.get("ticket_id") == "EU-512"
          and ev.get("pass_number") == 2,
          f"fields={ev}")

check("AC1e: exactly ONE notify call fired",
      len(_notify_stub.calls) == 1,
      f"notify calls={len(_notify_stub.calls)}: {_notify_stub.calls}")

check("AC1f: returned result is the native retry (not GLM)",
      res1.final == "native completed" and not res1.is_error,
      f"final={res1.final!r}, is_error={res1.is_error}")


# ══════════════ AC2: Transient/GLM success --> ZERO activations + ZERO notifies ════ #
print("\n[AC2] GLM transient -> zero glm_fallback_activated + zero notify", flush=True)


async def _fake_ac2_transient(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
    """GLM returns plan-limit kind='transient' (or empty string)."""
    return AgentRun(text="", final="Error: rate limit exceeded temporarily", cost_usd=0.0,
                    num_turns=0, is_error=True, is_plan_limit=True,
                    plan_limit_kind="transient", provider="GLM")


async def _fake_ac2_success(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
    """GLM clean success."""
    return AgentRun(text="glm output", final="GLM succeeded fine",
                    cost_usd=0.05, num_turns=2, is_error=False,
                    provider="GLM")


for desc, fake_fn in [("transient", _fake_ac2_transient), ("clean success", _fake_ac2_success)]:
    _notify_ac2 = _RaiseNotify()
    _loop._notify = _notify_ac2
    sink2 = _FakeAuditSink()
    agent_mod.configure_audit(sink2)
    agent_mod.run_agent = fake_fn
    try:
        res2 = asyncio.run(agent_mod.run_agent_with_fallback(
            "p", _Opts("claude-sonnet-4-1-20250620"),
            tag="builder", cfg=_Cfg("glm")))
    finally:
        agent_mod.run_agent = _orig_run_agent
        agent_mod.configure_audit(None)
        if _orig_notify is not None:
            _loop._notify = _orig_notify
        elif hasattr(_loop, "_notify"):
            delattr(_loop, "_notify")

    act_events2 = [e for e, _ in sink2.events if e == "glm_fallback_activated"]
    check(f"AC2 ({desc}): ZERO glm_fallback_activated events",
          len(act_events2) == 0,
          f"events={[e for e, _ in sink2.events]}")
    check(f"AC2 ({desc}): ZERO notify calls",
          len(_notify_ac2.calls) == 0,
          f"calls={_notify_ac2.calls}")


# ═══════════════ AC3a: Sink.record raises --> no propagate, same returned run ════════ #
print("\n[AC3a] Broken _AUDIT_SINK.record -> swallowed, run unchanged", flush=True)


async def _fake_ac3(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
    """GLM capped -> native succeeds (same shape as AC1)."""
    if not hasattr(_fake_ac3, "_n"):
        _fake_ac3._n = 0
    _fake_ac3._n += 1
    if _fake_ac3._n == 1:
        return AgentRun(text="", final="Error: usage limit reached", cost_usd=0.0,
                        num_turns=0, is_error=True, is_plan_limit=True,
                        plan_limit_kind="cap", provider="GLM")
    return AgentRun(text="done", final="native ok after broken sink",
                    cost_usd=0.15, num_turns=3, is_error=False)


class _BreakingSink:
    def record(self, *a, **k):
        raise RuntimeError("audit sink broken")


_broken_sink = _BreakingSink()
_loop._notify = _RaiseNotify()  # notify must still work
agent_mod.configure_audit(_broken_sink)
agent_mod.run_agent = _fake_ac3
_fake_ac3._n = 0
raised_ac3a = False
res_ac3a = None
try:
    res_ac3a = asyncio.run(agent_mod.run_agent_with_fallback(
        "p", _Opts("claude-sonnet-4-1-20250620"),
        tag="builder", cfg=_Cfg("glm")))
except Exception:
    raised_ac3a = True
finally:
    agent_mod.run_agent = _orig_run_agent
    agent_mod.configure_audit(None)
    if _orig_notify is not None:
        _loop._notify = _orig_notify
    elif hasattr(_loop, "_notify"):
        delattr(_loop, "_notify")

check("AC3a: no raise propagates out (sink raised)",
      not raised_ac3a,
      "exception propagated!")
check("AC3a: returned result is still native (unaffected)",
      res_ac3a is not None and res_ac3a.final == "native ok after broken sink",
      f"final={res_ac3a.final if res_ac3a else 'None'}")


# ═══════════════ AC3b: _notify raises --> no propagate, same returned run ════════════ #
print("\n[AC3b] Breaking _notify -> swallowed, run unchanged", flush=True)


async def _fake_ac3b(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
    if not hasattr(_fake_ac3b, "_n"):
        _fake_ac3b._n = 0
    _fake_ac3b._n += 1
    if _fake_ac3b._n == 1:
        return AgentRun(text="", final="Error: usage limit reached", cost_usd=0.0,
                        num_turns=0, is_error=True, is_plan_limit=True,
                        plan_limit_kind="cap", provider="GLM")
    return AgentRun(text="done", final="native ok after broken notify",
                    cost_usd=0.15, num_turns=3, is_error=False)


_ok_sink = _FakeAuditSink()
_breaking_notify = _RaiseNotify()
_breaking_notify.raise_flag = True
_loop._notify = _breaking_notify
agent_mod.configure_audit(_ok_sink)
agent_mod.run_agent = _fake_ac3b
_fake_ac3b._n = 0
raised_ac3b = False
res_ac3b = None
try:
    res_ac3b = asyncio.run(agent_mod.run_agent_with_fallback(
        "p", _Opts("claude-sonnet-4-1-20250620"),
        tag="builder", cfg=_Cfg("glm")))
except Exception:
    raised_ac3b = True
finally:
    agent_mod.run_agent = _orig_run_agent
    agent_mod.configure_audit(None)
    if _orig_notify is not None:
        _loop._notify = _orig_notify
    elif hasattr(_loop, "_notify"):
        delattr(_loop, "_notify")

check("AC3b: no raise propagates out (notify raised)",
      not raised_ac3b,
      "exception propagated!")
check("AC3b: returned result is still native (unaffected)",
      res_ac3b is not None and res_ac3b.final == "native ok after broken notify",
      f"final={res_ac3b.final if res_ac3b else 'None'}")


# ═══════════════════ Report ══════════════════════════════════════════════════════ #
print("\n============ EU-512 GLM FALLBACK AUDIT QA ============")
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
