"""EU-108 Opus-week pin REMOVED (Phase-2 Task 2, 2026-07-07) — a Sonnet weekly cap must NEVER pin a
week of Opus.

run_agent_with_fallback keeps the per-call one-shot Opus retry (so the CURRENT unit of work finishes
when Sonnet is capped but Opus still has headroom) but persists NOTHING: the next call starts cheap on
Sonnet again, and if Opus ALSO caps the wrapper returns is_plan_limit so autopilot's proactive poll
pauses (EU-82). The old weekly-Opus state machine — activate_sonnet_fallback / sonnet_fallback_active /
the persisted sonnet_fallback_state.json / the opus_fallback_on_sonnet_cap config knob — is deleted, and
for_builder / for_reviewer no longer have a "force Opus while the pin is armed" branch.

This harness pins that new contract. Written fail-first: against the un-deleted (pinning) code the
symbol-gone and no-persistence checks go RED.
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

from orchestrator import agent as agent_mod, models
from orchestrator.agent import AgentRun
from orchestrator.config import Config

# ── 1. the weekly-pin state machine is GONE from models ──────────────────────────────────────────
for sym in ("activate_sonnet_fallback", "sonnet_fallback_active", "reset_sonnet_fallback",
            "reset_sonnet_fallback_notification", "_get_next_friday_0900_utc",
            "fallback_reset_time_str", "mark_sonnet_fallback_notified",
            "sonnet_fallback_notification_sent", "_save_state", "_load_state", "_state_file"):
    check(f"models.{sym} deleted", not hasattr(models, sym))

# ── 2. the config knob that gated the pin is GONE ────────────────────────────────────────────────
check("config field opus_fallback_on_sonnet_cap deleted",
      "opus_fallback_on_sonnet_cap" not in Config.__dataclass_fields__)

# ── 3. for_builder / for_reviewer never force Opus via a pin branch (cheap-first, pass 1) ─────────
class _Ticket:
    id = "EU-108"; key = "EU-108"; summary = "x"; description = "d"; acceptance_criteria = []
cfg = Config(apps=[])
bmodel, _ = models.for_builder(cfg, _Ticket(), "high", 1)
check("for_builder pass-1 high-effort stays cheap-first (Sonnet, not a pinned Opus)",
      "sonnet" in bmodel.lower(), bmodel)
rmodel, _ = models.for_reviewer(cfg, diff="small diff", iteration=1)
check("for_reviewer pass-1 small diff stays cheap-first (Sonnet, not a pinned Opus)",
      "sonnet" in rmodel.lower(), rmodel)

# ── 4. behaviour of run_agent_with_fallback on a Sonnet cap ───────────────────────────────────────
calls: list[tuple[str, object]] = []

def _make_fake(opus_also_caps: bool):
    async def _fake(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
        model = getattr(options, "model", "")
        calls.append((model, routing_tier))
        if "sonnet" in model.lower():
            return AgentRun(text="", final="", cost_usd=0.0, num_turns=0, is_error=True,
                            is_plan_limit=True, plan_limit_kind="cap")
        # Opus leg
        if opus_also_caps:
            return AgentRun(text="", final="", cost_usd=0.0, num_turns=0, is_error=True,
                            is_plan_limit=True, plan_limit_kind="cap")
        return AgentRun(text="ok", final="ok", cost_usd=0.1, num_turns=1, is_error=False)
    return _fake

class _Opts:
    def __init__(self, model): self.model = model

class _Cfg:
    pass

_orig = agent_mod.run_agent

# (a) Sonnet cap + Opus success → the CURRENT work completes on Opus (Opus result returned) ...
calls.clear()
_cfg = _Cfg()
_cfg.audit_path = str(Path(tempfile.mkdtemp()) / "audit.jsonl")
agent_mod.run_agent = _make_fake(opus_also_caps=False)
try:
    res = asyncio.run(agent_mod.run_agent_with_fallback(
        "p", _Opts("claude-sonnet-5"), tag="builder", cfg=_cfg, routing_tier="cloud"))
finally:
    agent_mod.run_agent = _orig
check("Sonnet cap + Opus headroom → returns the Opus result (current pass completes)",
      res is not None and not res.is_error and res.final == "ok")
check("the Opus retry leg runs UNROUTED (routing_tier=None — really tests Anthropic Opus)",
      len(calls) == 2 and calls[1][1] is None, str(calls))
# ... and NOTHING is persisted: no weekly-pin state file is written beside audit_path
check("no sonnet_fallback_state.json persisted (no weekly pin armed)",
      not (Path(_cfg.audit_path).with_name("sonnet_fallback_state.json")).exists())

# (b) Sonnet cap + Opus ALSO caps → is_plan_limit propagates (→ EU-82 pause), no Opus pin
calls.clear()
agent_mod.run_agent = _make_fake(opus_also_caps=True)
try:
    res2 = asyncio.run(agent_mod.run_agent_with_fallback(
        "p", _Opts("claude-sonnet-5"), tag="builder", cfg=_cfg, routing_tier="cloud"))
finally:
    agent_mod.run_agent = _orig
check("Sonnet cap + Opus also cap → returns is_plan_limit (All-models cap → EU-82 pause)",
      res2 is not None and res2.is_plan_limit)

print("\n============ EU-108 PIN-REMOVED QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
