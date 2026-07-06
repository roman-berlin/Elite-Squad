"""Routed-env scoping QA (2026-07-05 audit §6 defect 2 + the 2026-07-06 review's race).

The EU-174 LOCAL tier routes a call to Ollama by mutating process-global env
(ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY) plus options.model. Pinned here:

  1. unset-restore — when ANTHROPIC_BASE_URL was originally UNSET, one LOCAL call must leave it
     unset afterwards (the original bug pointed the whole process at Ollama forever);
  2. exception path — a query() crash mid-call still restores everything (try/finally);
  3. overlap serialisation — two overlapping LOCAL calls may NOT interleave save/restore: the
     later one would snapshot the earlier one's Ollama URL as its "original" and re-strand the
     process after the first one's pop (the review's concurrency reopening of the same defect);
  4. CLOUD tier — never touches env at all, and options.model is restored;
  5. the EU-108 Opus cap-probe runs UNROUTED (routing_tier must not reach the probe call —
     CLOUD would rewrite the probe's model to the routed one and arm fallback off a GLM result).

All offline — the SDK is stubbed and agent.query is monkeypatched; no network, no models.
"""
from __future__ import annotations

import asyncio
import os
import sys
import types

sdk = types.ModuleType("claude_agent_sdk")


class _Options:
    def __init__(s, **kw):
        s.__dict__.update(kw)


class _D:
    def __init__(s, *a, **k):
        pass

    def __call__(s, *a, **k):
        return s


sdk.ClaudeAgentOptions = _Options
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.agent as agent_mod                 # noqa: E402
from orchestrator.agent import AgentRun, run_agent     # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


OLLAMA = "http://localhost:11434"

# Known-clean env slate for every scenario; restored at the end.
_SAVED = {k: os.environ.get(k) for k in
          ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "OLLAMA_BASE_URL", "OLLAMA_API_KEY")}
os.environ.pop("ANTHROPIC_BASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["OLLAMA_BASE_URL"] = OLLAMA
os.environ["OLLAMA_API_KEY"] = "ollama-test-key"


def _empty_query(**kw):
    async def _gen():
        return
        yield  # pragma: no cover — makes this an async generator
    return _gen()


# ── 1. LOCAL: env originally unset → set during the call → unset after ──────────────────────────
seen_inside: dict = {}


def _spy_query(**kw):
    async def _gen():
        seen_inside["base_url"] = os.environ.get("ANTHROPIC_BASE_URL")
        seen_inside["api_key"] = os.environ.get("ANTHROPIC_API_KEY")
        return
        yield
    return _gen()


agent_mod.query = _spy_query
opts = _Options(model="claude-sonnet-4-6")
asyncio.run(run_agent("p", opts, routing_tier="local"))
chk("LOCAL: base URL points at Ollama DURING the call", seen_inside.get("base_url") == OLLAMA,
    str(seen_inside))
chk("LOCAL: originally-unset base URL is UNSET after the call (not stranded at Ollama)",
    "ANTHROPIC_BASE_URL" not in os.environ, str(os.environ.get("ANTHROPIC_BASE_URL")))
chk("LOCAL: originally-unset API key is UNSET after the call",
    "ANTHROPIC_API_KEY" not in os.environ, str(os.environ.get("ANTHROPIC_API_KEY")))
chk("LOCAL: options.model restored", opts.model == "claude-sonnet-4-6", str(opts.model))

# Originally-set values are restored, not popped.
os.environ["ANTHROPIC_BASE_URL"] = "https://api.anthropic.com"
asyncio.run(run_agent("p", _Options(model="claude-sonnet-4-6"), routing_tier="local"))
chk("LOCAL: originally-set base URL restored to its original value",
    os.environ.get("ANTHROPIC_BASE_URL") == "https://api.anthropic.com",
    str(os.environ.get("ANTHROPIC_BASE_URL")))
os.environ.pop("ANTHROPIC_BASE_URL", None)


# ── 2. exception mid-call still restores ─────────────────────────────────────────────────────────
def _boom_query(**kw):
    async def _gen():
        raise RuntimeError("model down")
        yield
    return _gen()


agent_mod.query = _boom_query
try:
    asyncio.run(run_agent("p", _Options(model="claude-sonnet-4-6"), routing_tier="local"))
except RuntimeError:
    pass
chk("LOCAL: exception mid-call → env still restored (unset)",
    "ANTHROPIC_BASE_URL" not in os.environ and "ANTHROPIC_API_KEY" not in os.environ,
    f"{os.environ.get('ANTHROPIC_BASE_URL')},{os.environ.get('ANTHROPIC_API_KEY')}")


# ── 3. overlapping LOCAL calls serialise — no interleaved save/restore ──────────────────────────
order: list[str] = []
gate = asyncio.Event()


def _overlap_query(**kw):
    async def _gen():
        order.append(f"enter:{os.environ.get('ANTHROPIC_BASE_URL')}")
        if len([e for e in order if e.startswith("enter")]) == 1:
            await gate.wait()          # first call holds its window open until released
        order.append("exit")
        return
        yield
    return _gen()


async def _overlap():
    agent_mod.query = _overlap_query
    a = asyncio.create_task(run_agent("A", _Options(model="claude-sonnet-4-6"), routing_tier="local"))
    await asyncio.sleep(0.15)          # A is inside its window, holding the lock
    b = asyncio.create_task(run_agent("B", _Options(model="claude-sonnet-4-6"), routing_tier="local"))
    await asyncio.sleep(0.15)          # B must be QUEUED on the lock, not inside
    entered_while_a_held = len([e for e in order if e.startswith("enter")])
    gate.set()
    await asyncio.gather(a, b)
    return entered_while_a_held


entered = asyncio.run(_overlap())
chk("overlap: second LOCAL call queues on the lock while the first is in flight",
    entered == 1, f"{entered} enters during A's window, order={order}")
chk("overlap: both calls saw Ollama inside their own (serialised) window",
    [e for e in order if e.startswith("enter")] == [f"enter:{OLLAMA}"] * 2, str(order))
chk("overlap: after both finish the env is UNSET — no re-stranding via interleaved restore",
    "ANTHROPIC_BASE_URL" not in os.environ, str(os.environ.get("ANTHROPIC_BASE_URL")))


# ── 4. CLOUD tier: no env mutation, options.model swapped then restored ─────────────────────────
agent_mod.query = _spy_query
seen_inside.clear()
opts_c = _Options(model="claude-sonnet-4-6")
asyncio.run(run_agent("p", opts_c, routing_tier="cloud"))
chk("CLOUD: env untouched during the call", seen_inside.get("base_url") is None, str(seen_inside))
chk("CLOUD: env untouched after the call", "ANTHROPIC_BASE_URL" not in os.environ)
chk("CLOUD: options.model restored after the call", opts_c.model == "claude-sonnet-4-6",
    str(opts_c.model))


# ── 5. EU-108 Opus cap-probe runs UNROUTED ───────────────────────────────────────────────────────
probe_calls: list[tuple[str, object]] = []


async def _fake_run_agent(prompt, options, tag="", ticket_id=None, pass_number=None,
                          routing_tier=None):
    model = getattr(options, "model", "")
    probe_calls.append((model, routing_tier))
    if "sonnet" in model.lower():
        return AgentRun(text="", final="", cost_usd=0.0, num_turns=0, is_error=True,
                        is_plan_limit=True, plan_limit_kind="cap")
    return AgentRun(text="ok", final="ok", cost_usd=0.1, num_turns=1, is_error=False)


class _Cfg:
    opus_fallback_on_sonnet_cap = True


_orig_run_agent = agent_mod.run_agent
agent_mod.run_agent = _fake_run_agent
try:
    # The successful probe ARMS the weekly fallback and persists it beside cfg.audit_path.
    # That path must be an ISOLATED directory — a bare mkstemp lands the state file in the
    # shared $TMPDIR, where every other mkstemp-configured harness (eu91 etc.) reads it and
    # goes Opus-pinned — and the state must be reset afterwards regardless.
    import tempfile
    from pathlib import Path as _P
    from orchestrator import models as _models
    from orchestrator import notify as _notify
    _cfg = _Cfg()
    _cfg.audit_path = str(_P(tempfile.mkdtemp()) / "audit.jsonl")
    _orig_send = _notify.send
    _notify.send = lambda *a, **k: True
    try:
        asyncio.run(agent_mod.run_agent_with_fallback(
            "p", _Options(model="claude-sonnet-4-6"), tag="builder", cfg=_cfg,
            routing_tier="cloud"))
    finally:
        _notify.send = _orig_send
        _models.reset_sonnet_fallback(_cfg)
        _models.reset_sonnet_fallback_notification(_cfg)
finally:
    agent_mod.run_agent = _orig_run_agent

chk("probe: Sonnet leg carries the caller's routing_tier",
    probe_calls and probe_calls[0][1] == "cloud", str(probe_calls))
chk("probe: Opus leg runs UNROUTED (routing_tier=None — really tests Anthropic Opus)",
    len(probe_calls) == 2 and "opus" in str(probe_calls[1][0]).lower()
    and probe_calls[1][1] is None, str(probe_calls))

# ── restore the process env slate ────────────────────────────────────────────────────────────────
for k, v in _SAVED.items():
    if v is None:
        os.environ.pop(k, None)
    else:
        os.environ[k] = v

print("\n========== ROUTED-ENV SCOPE QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
