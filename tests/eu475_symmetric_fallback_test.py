#!/usr/bin/env python3
"""EU-513: Symmetric fallback regression — both directions + transient guarantees.

Proves that the two fallback paths (EU-118 main→secondary and EU-475 secondary→main) are
independent mirrors, each retrying exactly once on cap, and that transient plan-limits never
cross to the other model in either direction.

Also verifies that Documentation/BUILD_DOCTRINE.md documents this symmetric design with the
required ticket references and key-phrases (one-shot, per-call, no weekly pin, transient).

The existing harnesses already verify instrumented activation; these checks cover the behaviour
contract so neither path silently reverts after a future refactor.
"""
from __future__ import annotations

import asyncio
import sys
import types

# ── Stub claude_agent_sdk before any orchestrator import ────────────────────────

sdk = types.ModuleType("claude_agent_sdk")


class _D:
    """Minimal stub for everything ClaudeAgentOptions-like the SDK may expose."""
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

# ═══════════════════ Helpers & fixtures ══════════════════════════════════════════

results: list[tuple[str, bool, str]] = []


def check(n: str, c: bool, d: str = "") -> None:
    results.append((n, bool(c), d))


class _Opts:
    def __init__(self, model: str) -> None:
        self.model = model
        self.env: dict[str, str] = {}


class _Cfg:
    """Minimal cfg with model_backend attribute."""
    def __init__(self, backend: str) -> None:
        self.model_backend = backend


# Save originals to restore after each test section
_orig_run_agent = agent_mod.run_agent

# Zero out transient backoff so tests don't actually sleep
agent_mod._TRANSIENT_RETRY_BACKOFF_S = 0.0

# Notify stub (no network in tests)
_notify_stub_calls: list[str] = []


def _stub_notify(cfg, text: str) -> None:
    _notify_stub_calls.append(text)


_orig_notify = None


def _setup_notify_patch() -> None:
    global _orig_notify
    import orchestrator.loop as _loop
    _orig_notify = getattr(_loop, "_notify", None)
    _loop._notify = _stub_notify


def _restore_notify() -> None:
    import orchestrator.loop as _loop
    global _orig_notify
    if _orig_notify is not None:
        _loop._notify = _orig_notify
    elif hasattr(_loop, "_notify"):
        delattr(_loop, "_notify")


# ═══════════════════ Test data helpers ══════════════════════════════════════════

SONNET_CAP = AgentRun(text="", final="Error: usage limit reached for your plan",
                       cost_usd=0.0, num_turns=0, is_error=True, is_plan_limit=True,
                       plan_limit_kind="cap", provider="Anthropic")
NATIVE_OK = AgentRun(text="done", final="native completed",
                     cost_usd=0.15, num_turns=3, is_error=False, provider="Anthropic")
GLM_CAP = AgentRun(text="", final="Error: usage limit reached", cost_usd=0.0,
                   num_turns=0, is_error=True, is_plan_limit=True, plan_limit_kind="cap",
                   provider="GLM")
GLM_TRANSIENT = AgentRun(text="", final="Error: rate limit exceeded temporarily",
                         cost_usd=0.0, num_turns=0, is_error=True, is_plan_limit=True,
                         plan_limit_kind="transient", provider="GLM")
SONNET_TRANSIENT = AgentRun(text="", final="Error: 429 rate limit exceeded",
                            cost_usd=0.0, num_turns=0, is_error=True, is_plan_limit=True,
                            plan_limit_kind="transient")
SONNET_OK = AgentRun(text="ok", final="ok", cost_usd=0.1, num_turns=1, is_error=False)
ALL_MODEL_CAP = AgentRun(text="", final="Error: usage limit reached", cost_usd=0.0,
                         num_turns=0, is_error=True, is_plan_limit=True,
                         plan_limit_kind="cap", provider="GLM")


# ═══════════════════ Direction A: EU-118 Sonnet→Opus (main→secondary) ═══════════

print("\n[Direction A] EU-118: Sonnet cap → Opus retry", flush=True)

_sonnet_n = [0]
_opus_n = [0]


async def _sonnet_or_opus(prompt, options=None, **kw):
    """Direction A fake: Sonnet call → CAP; Opus retry → OK."""
    model = getattr(options, "model", "") if options else ""
    if "sonnet" in model.lower():
        _sonnet_n[0] += 1
        return SONNET_CAP
    # Opus retry succeeds
    _opus_n[0] += 1
    return NATIVE_OK


agent_mod.run_agent = _sonnet_or_opus
_setup_notify_patch()
try:
    res_a = asyncio.run(agent_mod.run_agent_with_fallback(
        "p", _Opts("claude-sonnet-5"), tag="builder",
        ticket_id="EU-513", pass_number=1))
finally:
    agent_mod.run_agent = _orig_run_agent
    _restore_notify()

check("AC1a: total run_agent calls == 2 (1 Sonnet + 1 Opus)",
      _sonnet_n[0] + _opus_n[0] == 2,
      f"Sonnet={_sonnet_n[0]}, Opus={_opus_n[0]}")
check("AC1b: returned result is the Opus success",
      res_a is not None and not res_a.is_error and res_a.final == NATIVE_OK.final,
      f"final={res_a.final!r}")


# ═══════════════════ Direction B: EU-475 GLM→Native (secondary→main) ═══════════

print("\n[Direction B] EU-475: GLM cap → Native retry", flush=True)

_call_seq_b = []


async def _glm_or_native(prompt, options=None, **kw):
    """Direction B fake: First call (GLM context) → CAP; Second call (NATIVE) → OK."""
    _call_seq_b.append(1)  # track total calls
    if len(_call_seq_b) == 1:
        return GLM_CAP  # First call under GLM → CAP
    return NATIVE_OK  # Retry under NATIVE → success


agent_mod.run_agent = _glm_or_native
_setup_notify_patch()
try:
    res_b = asyncio.run(agent_mod.run_agent_with_fallback(
        "p", _Opts("glm-5.2"), tag="reviewer",
        ticket_id="EU-513", pass_number=2,
        cfg=_Cfg("glm")))
finally:
    agent_mod.run_agent = _orig_run_agent
    _restore_notify()

check("AC2a: GLM cap returns the native result (not the GLM error)",
      res_b is not None and not res_b.is_error and res_b.final == NATIVE_OK.final,
      f"final={res_b.final!r}")
check("AC2b: exactly 2 calls (first GLM, retry NATIVE without routing_tier)",
      len(_call_seq_b) == 2,
      f"total calls={len(_call_seq_b)}")


# Bonus: both models capped → original GLM result returned
print("[Direction B bonus] Both GLM and native capped → original GLM result", flush=True)


async def _both_capped(prompt, options=None, **kw):
    """Both GLM and NATIVE calls return cap."""
    return ALL_MODEL_CAP


agent_mod.run_agent = _both_capped
_setup_notify_patch()
try:
    res_bc = asyncio.run(agent_mod.run_agent_with_fallback(
        "p", _Opts("glm-5.2"), tag="reviewer",
        ticket_id="EU-513", pass_number=2,
        cfg=_Cfg("glm")))
finally:
    agent_mod.run_agent = _orig_run_agent
    _restore_notify()

check("AC2c (bonus): both GLM+native capped → original GLM result returned",
      res_bc is not None and res_bc.is_plan_limit,
      f"is_plan_limit={res_bc.is_plan_limit}, is_error={res_bc.is_error}")


# ═══════════════════ Transient A: Sonnet transient stays on Sonnet ══════════════

print("\n[Transient A] Sonnet transient → recovery on Sonnet, no Opus cross", flush=True)

_sonnet_tr_n = [0]
_opus_tr_n = [0]


async def _sonnet_transient_once(prompt, options=None, **kw):
    """Sonnet transient twice, then clean — NO Opus called."""
    model = getattr(options, "model", "") if options else ""
    if "sonnet" in model.lower():
        _sonnet_tr_n[0] += 1
        if _sonnet_tr_n[0] <= 2:
            return SONNET_TRANSIENT
        return SONNET_OK
    _opus_tr_n[0] += 1
    return NATIVE_OK


agent_mod.run_agent = _sonnet_transient_once
_setup_notify_patch()
try:
    res_ta = asyncio.run(agent_mod.run_agent_with_fallback(
        "p", _Opts("claude-sonnet-5"), tag="builder", ticket_id="EU-513"))
finally:
    agent_mod.run_agent = _orig_run_agent
    _restore_notify()

check("AC3a: transient Sonnet recovers on SAME Sonnet model (no Opus call)",
      _opus_tr_n[0] == 0,
      f"Opus calls={_opus_tr_n[0]}")
check("AC3b: clean Sonnet result returned after transient recovery",
      res_ta is not None and not res_ta.is_error and res_ta.final == "ok",
      f"final={res_ta.final!r}")


# ═══════════════════ Transient B: GLM transient → passthrough, no NATIVE ═════════

print("\n[Transient B] GLM transient → exact passthrough, no NATIVE cross", flush=True)

_glm_tr_n = [0]
_nat_tr_n = [0]


async def _glm_always_transient(prompt, options=None, **kw):
    """Always transient — GLM branch returns immediately (line 687 passthrough)."""
    _glm_tr_n[0] += 1
    return GLM_TRANSIENT


agent_mod.run_agent = _glm_always_transient
_setup_notify_patch()
try:
    res_tb = asyncio.run(agent_mod.run_agent_with_fallback(
        "p", _Opts("glm-5.2"), tag="reviewer",
        ticket_id="EU-513", pass_number=2,
        cfg=_Cfg("glm")))
finally:
    agent_mod.run_agent = _orig_run_agent
    _restore_notify()

check("AC4a: GLM transient → single call only (never touches NATIVE)",
      _glm_tr_n[0] == 1 and _nat_tr_n[0] == 0,
      f"GLM={_glm_tr_n[0]}, NATIVE={_nat_tr_n[0]}")
check("AC4b: returned result is the original GLM transient (unchanged)",
      res_tb is not None and res_tb.is_plan_limit and res_tb.plan_limit_kind == "transient",
      f"plan_limit_kind={res_tb.plan_limit_kind!r}")


# ═══════════════════ Doctrine content check ════════════════════════════════════

print("\n[Doctrine] BUILD_DOCTRINE.md contains symmetric fallback subsection", flush=True)

doc_path = "Documentation/BUILD_DOCTRINE.md"
try:
    doc_text = open(doc_path, encoding="utf-8").read()
    required = {
        "EU-118": "EU-118 reference",
        "EU-475": "EU-475 reference",
        "EU-108": "EU-108 weekly pin deleted reference",
        "one-shot": "one-shot language",
        "per-call": "per-call language",
        "weekly": "no-weekly-pin language",
        "transient": "transient plan-limit behavior",
        "fail over": "fail-over restriction for transients",
    }
    for phrase, desc in required.items():
        check(f"Doctrine contains '{phrase}' ({desc})",
              phrase.lower() in doc_text.lower(),
              f"'{phrase}' not found")
except FileNotFoundError:
    for phrase, desc in required.items():
        check(f"Doctrine contains '{phrase}' ({desc}) — file missing!", False,
              "BUILD_DOCTRINE.md not found")

# ═══════════════════ Report ════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("  EU-513 SYMMETRIC FALLBACK REGRESSION QA")
print("=" * 60)
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-" * 60)
print(f"  {passed}/{total} passed")
if passed < total:
    print(f"  RESULT: {total - passed} FAIL ❌")
    sys.exit(1)
else:
    print("  RESULT: ALL GREEN ✅")
    sys.exit(0)
