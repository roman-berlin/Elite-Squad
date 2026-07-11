"""EU-212: audit event on Sonnet-cap -> Opus fallback activation.

The 5-day Opus weekly pin was already deleted (Phase-2 Task 2, see eu108_pin_removed_test.py) —
run_agent_with_fallback is already per-call: every call starts on Sonnet, only the single capped
call retries on Opus, and nothing is persisted. The only unbuilt slice from EU-212 is the audit
event: when a Sonnet cap-classified plan-limit triggers the Opus retry, a configured _AUDIT_SINK
must receive a `sonnet_fallback_activated` record with the classification reason, the original
Sonnet error text, and the tag — so the cockpit can show WHY fallback is active. Emitted ONLY on
the cap-classified branch, never on the transient-rate-limit backoff-and-retry-Sonnet branch, and
must be a silent no-op when _AUDIT_SINK is unconfigured. No sonnet_fallback_state.json (or any
weekly-pin state) is ever written — the fallback stays per-call (auto-exit, no Friday wait).

Written fail-first: against the un-instrumented code (bare print(), no _AUDIT_SINK.record call on
the activation branch) checks 1 and 2 go RED.
"""
import sys, types, asyncio, tempfile
from pathlib import Path

REPO = "."
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, REPO)

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))

from orchestrator import agent as agent_mod
from orchestrator.agent import AgentRun

# Zero out the transient backoff so test 2 doesn't actually sleep.
agent_mod._TRANSIENT_RETRY_BACKOFF_S = 0.0


class _FakeSink:
    def __init__(self):
        self.events = []

    def record(self, event, **fields):
        self.events.append((event, fields))


class _Opts:
    def __init__(self, model): self.model = model


def _make_fake(opus_also_caps: bool):
    async def _fake(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
        model = getattr(options, "model", "")
        if "sonnet" in model.lower():
            return AgentRun(text="", final="Error: usage limit reached for your plan", cost_usd=0.0,
                            num_turns=0, is_error=True, is_plan_limit=True, plan_limit_kind="cap")
        if opus_also_caps:
            return AgentRun(text="", final="Error: usage limit reached", cost_usd=0.0, num_turns=0,
                            is_error=True, is_plan_limit=True, plan_limit_kind="cap")
        return AgentRun(text="ok", final="ok", cost_usd=0.1, num_turns=1, is_error=False)
    return _fake


def _make_fake_transient():
    calls = {"n": 0}
    async def _fake2(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return AgentRun(text="", final="Error: 429 rate limit exceeded", cost_usd=0.0, num_turns=0,
                            is_error=True, is_plan_limit=True, plan_limit_kind="transient")
        return AgentRun(text="ok", final="ok", cost_usd=0.1, num_turns=1, is_error=False)
    return _fake2


_orig = agent_mod.run_agent

# ── 1. Sonnet cap → Opus fallback activates → configured sink receives sonnet_fallback_activated ──
sink = _FakeSink()
agent_mod.configure_audit(sink)
agent_mod.run_agent = _make_fake(opus_also_caps=False)
try:
    res = asyncio.run(agent_mod.run_agent_with_fallback(
        "p", _Opts("claude-sonnet-5"), tag="builder", ticket_id="EU-212", pass_number=1))
finally:
    agent_mod.run_agent = _orig

activation_events = [f for e, f in sink.events if e == "sonnet_fallback_activated"]
check("cap-classified Sonnet limit → exactly one sonnet_fallback_activated event",
      len(activation_events) == 1, str(sink.events))
if activation_events:
    ev = activation_events[0]
    check("event carries the classification reason ('cap')",
          ev.get("reason") == "cap" or ev.get("kind") == "cap", str(ev))
    check("event carries the original Sonnet error text",
          "usage limit" in str(ev.get("error", "") or ev.get("original_error", "")), str(ev))
    check("event carries the tag", ev.get("tag") == "builder", str(ev))
check("Opus still completes the pass (fallback result returned)",
      res is not None and not res.is_error and res.final == "ok")

# ── 2. Transient rate-limit (not cap) → NO sonnet_fallback_activated event ─────────────────────────
sink2 = _FakeSink()
agent_mod.configure_audit(sink2)
agent_mod.run_agent = _make_fake_transient()
try:
    res_t = asyncio.run(agent_mod.run_agent_with_fallback(
        "p", _Opts("claude-sonnet-5"), tag="builder"))
finally:
    agent_mod.run_agent = _orig

check("transient rate-limit path emits NO sonnet_fallback_activated event",
      not any(e == "sonnet_fallback_activated" for e, _ in sink2.events), str(sink2.events))
check("transient path still recovers on Sonnet retry", res_t is not None and not res_t.is_error)

# ── 3. _AUDIT_SINK unconfigured (None) → cap fallback still works, no raise ────────────────────────
agent_mod.configure_audit(None)
agent_mod.run_agent = _make_fake(opus_also_caps=False)
raised = False
try:
    res_none = asyncio.run(agent_mod.run_agent_with_fallback(
        "p", _Opts("claude-sonnet-5"), tag="builder"))
except Exception:
    raised = True
finally:
    agent_mod.run_agent = _orig
check("unconfigured _AUDIT_SINK → no raise, Opus result still returned",
      not raised and res_none is not None and not res_none.is_error and res_none.final == "ok")

# ── 4. no sonnet_fallback_state.json (or any weekly-pin state) written on a Sonnet cap ─────────────
tmpdir = Path(tempfile.mkdtemp())
sink3 = _FakeSink()
agent_mod.configure_audit(sink3)
agent_mod.run_agent = _make_fake(opus_also_caps=False)
try:
    asyncio.run(agent_mod.run_agent_with_fallback(
        "p", _Opts("claude-sonnet-5"), tag="builder"))
finally:
    agent_mod.run_agent = _orig
    agent_mod.configure_audit(None)
check("no sonnet_fallback_state.json written anywhere (per-call fallback, auto-exit)",
      not any(tmpdir.rglob("sonnet_fallback_state.json")) and
      not Path("sonnet_fallback_state.json").exists())

print("\n============ EU-212 FALLBACK AUDIT QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
