"""Shared Agent SDK helper.

Wraps `claude_agent_sdk.query()`, collects the assistant text + cost/turn metadata,
and (when a `tag` is given) streams a compact line per tool use so you can see the
officer working in real time.

EU-108 Sonnet-cap fallback:
  When a Sonnet call hits a CAP-classified plan-limit error (the message names an exhausted
  usage/plan/weekly quota), the fallback logic retries with Opus once to distinguish between:
    • Sonnet sub-limit hit → Opus succeeds cleanly → activate fallback, stay on Opus until reset
    • All-models cap hit → Opus also 429s → pause (existing EU-82 governor behavior)
  A transient per-minute 429 / 529 overload instead gets one backoff retry on Sonnet and never
  arms the weekly fallback; a failed Opus probe (auth/network) never arms it either.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

try:  # tool-use block type name can vary across SDK versions
    from claude_agent_sdk import ToolUseBlock
except Exception:  # pragma: no cover
    ToolUseBlock = None

# Serialises the LOCAL-tier routing window: the Ollama base-URL/key swap is process-global env,
# so overlapping routed calls must not interleave their save/restore (2026-07-06 review). Held
# for the duration of one routed agent call; acquired via asyncio.to_thread so event loops in
# any thread queue without blocking. Retired when Phase-2 routing moves to per-call options.
_ROUTED_ENV_LOCK = threading.Lock()


@dataclass
class AgentRun:
    text: str            # concatenated assistant text across the run
    final: str           # text of the final message (the verdict / summary)
    cost_usd: float
    num_turns: int
    is_error: bool
    tools: list[str] = field(default_factory=list)   # tool calls made, for the transcript
    input_tokens: int = 0    # prompt + cache tokens this run (for the usage ledger)
    output_tokens: int = 0   # completion tokens this run
    # EU-118: true when this run hit a Claude plan limit (429 / usage-limit).
    # Detection matches _CAP_PATTERNS + _TRANSIENT_PATTERNS below against the Agent SDK's
    # message.error field; the SDK surfaces Anthropic API errors (HTTP 429 with "rate limit"
    # or "usage limit" details) there when a plan/session/weekly quota is exceeded.
    is_plan_limit: bool = False
    # EU-108 hardening (2026-07-05 fake cap alert): how the plan-limit error classified —
    #   "cap"       → the message names an exhausted usage/plan/weekly quota (fallback-eligible)
    #   "transient" → per-minute 429 / 529 overload (retry territory; must never arm the
    #                 weekly Opus fallback, which is a ~1.7x cost amplifier until Friday)
    #   ""          → not a plan-limit error. Additive field: is_plan_limit keeps its
    #                 EU-118 broad meaning for existing consumers.
    plan_limit_kind: str = ""
    # EU-123: provider information for this run — which provider + model was used
    provider: str = ""          # "Anthropic" or "GLM" (z.ai)
    model_version: str = ""     # Clean model identifier (e.g., "claude-opus-4-8", "glm-4")
    # QW4 (2026-07-05): wall-clock duration of this call — the forensic audit found NO duration
    # was recorded anywhere (0 duration fields across 2,555 audit events).
    duration_s: float = 0.0


# QW4: optional audit sink — when a process configures it (main/server startup, beside
# usage.configure), every agent call also lands a structured `agent_call` event in audit.jsonl
# {tag, model, provider, tokens in/out, cost, duration, turns, ticket, pass}. audit.jsonl stays the
# single source of truth for per-call economics; usage_ledger.jsonl remains the compact rollup the
# gauges read. Unconfigured (tests, ad-hoc imports) → no-op.
_AUDIT_SINK = None


def configure_audit(audit) -> None:
    """Point agent_call instrumentation at an AuditLog. Call once at process start."""
    global _AUDIT_SINK
    _AUDIT_SINK = audit


# EU-108 hardening: split the old catch-all plan-limit patterns into two classes. A genuine
# quota exhaustion names the cap ("Claude usage limit reached", "weekly limit", "plan limit");
# a transient per-minute 429 or 529 overload only carries status/rate-limit language. Cap
# patterns win when both match (a real cap error usually also carries a 429 status).
_CAP_PATTERNS = ("usage limit", "usage-limit", "plan limit", "weekly limit",
                 "limit reached", "over limit", "quota exceeded")
_TRANSIENT_PATTERNS = ("rate limit", "rate_limit", "too many requests",
                       "overloaded", "429", "529")


def _classify_plan_limit(err: str) -> str:
    """Classify an SDK error string: "cap", "transient", or "" (not a plan-limit error)."""
    e = err.lower()
    if any(p in e for p in _CAP_PATTERNS):
        return "cap"
    if any(p in e for p in _TRANSIENT_PATTERNS):
        return "transient"
    return ""


def _tool_brief(name: str, inp) -> str:
    if isinstance(inp, dict):
        for k in ("file_path", "path", "command", "pattern", "url"):
            v = inp.get(k)
            if v:
                return f"{name} {str(v).splitlines()[0][:72]}"
    return name or "tool"


async def run_agent(prompt: str, options: ClaudeAgentOptions, tag: str = "",
                    ticket_id: str | None = None, pass_number: int | None = None,
                    routing_tier: str | None = None) -> AgentRun:
    # EU-38: `ticket_id` + `pass_number` let a build/soldier pass tag its ledger line so per-pass
    # input tokens are sliceable by ticket (the real cost lever). Optional + keyword-defaulted, so
    # every existing caller (officers/chat that pass only `tag`) is unaffected.

    # EU-174: hybrid routing — tier-based endpoint switching. The mutations are process-global
    # (env base URL/key) plus options.model, so they are scoped with try/finally and restored
    # with unset-aware semantics. The 2026-07-05 audit (§6 defect 2) found the old restore was
    # skipped entirely when ANTHROPIC_BASE_URL was originally unset — one LOCAL-tier call
    # permanently pointed the whole process at Ollama — and no exception path restored anything.
    # EU-189: GLM backend is applied per-call via options.env inside _run_agent_unrouted and is
    # mutually exclusive with EU-174 tier routing (which mutates process-global env). When GLM is
    # the run's backend, bypass routing entirely and go straight to the single seam.
    from . import backends as _backends
    if _backends.current() == _backends.GLM:
        return await _run_agent_unrouted(prompt, options, tag=tag, ticket_id=ticket_id,
                                         pass_number=pass_number)

    if not routing_tier:
        return await _run_agent_unrouted(prompt, options, tag=tag, ticket_id=ticket_id,
                                         pass_number=pass_number)

    import asyncio as _asyncio
    import os as _os
    from . import routing as _routing
    tier = _routing.RoutingTier(routing_tier)

    if tier != _routing.RoutingTier.LOCAL:
        # Cloud tier touches only THIS call's options.model — no process-global state.
        _original_model = getattr(options, "model", None)
        cloud_model = _routing.get_model_for_tier(tier)
        if cloud_model and hasattr(options, "model"):
            options.model = cloud_model
        try:
            return await _run_agent_unrouted(prompt, options, tag=tag, ticket_id=ticket_id,
                                             pass_number=pass_number)
        finally:
            if hasattr(options, "model"):
                options.model = _original_model

    # LOCAL tier mutates process-global env (base URL/key → Ollama). 2026-07-06 review: two
    # overlapping routed calls could interleave save/restore — the later starter snapshots the
    # earlier one's Ollama URL as its "original" and restores it after the pop, re-stranding the
    # whole process (§6 defect 2 reopened via concurrency). The mutation window is therefore
    # serialised process-wide; the Phase-2 routing slice replaces env mutation with per-call
    # option routing, retiring this lock. An UNROUTED call overlapping a LOCAL window is still
    # served by the mutated env — a known limitation until that slice. The blocking acquire runs
    # off-loop (to_thread) so no event loop is ever stalled while queueing.
    await _asyncio.to_thread(_ROUTED_ENV_LOCK.acquire)
    try:
        _original_base_url = _os.environ.get("ANTHROPIC_BASE_URL")
        _original_api_key = _os.environ.get("ANTHROPIC_API_KEY")
        _original_model = getattr(options, "model", None)

        base_url = _routing.get_base_url_for_tier(tier)
        api_key = _routing.get_api_key_for_tier(tier)
        model = _routing.get_model_for_tier(tier)
        if base_url:
            _os.environ["ANTHROPIC_BASE_URL"] = base_url
        if api_key:
            _os.environ["ANTHROPIC_API_KEY"] = api_key
        if hasattr(options, "model"):
            options.model = model

        try:
            # The ledger/audit records inside see the ROUTED model — deliberate: the row must say
            # what was actually served. Restore runs after them, exception or not.
            return await _run_agent_unrouted(prompt, options, tag=tag, ticket_id=ticket_id,
                                             pass_number=pass_number)
        finally:
            if _original_base_url is not None:
                _os.environ["ANTHROPIC_BASE_URL"] = _original_base_url
            else:
                _os.environ.pop("ANTHROPIC_BASE_URL", None)
            if _original_api_key is not None:
                _os.environ["ANTHROPIC_API_KEY"] = _original_api_key
            else:
                _os.environ.pop("ANTHROPIC_API_KEY", None)
            if hasattr(options, "model"):
                options.model = _original_model
    finally:
        _ROUTED_ENV_LOCK.release()


async def _run_agent_unrouted(prompt: str, options: ClaudeAgentOptions, tag: str = "",
                              ticket_id: str | None = None,
                              pass_number: int | None = None) -> AgentRun:
    chunks: list[str] = []
    tools: list[str] = []
    final = ""
    cost = 0.0
    turns = 0
    in_tok = 0
    out_tok = 0
    is_error = False
    is_plan_limit = False
    plan_limit_kind = ""

    # EU-189: point THIS call at the run's chosen backend (Opus/GLM) via options.env just before
    # the SDK query. No-op for Opus; for GLM it writes the z.ai endpoint + bearer into options.env
    # (per-subprocess only — zero process-global mutation) and overrides options.model. Returns the
    # backend actually applied (fail-closed to Opus if GLM is unconfigured).
    from . import backends as _backends
    effective_backend = _backends.apply(options)
    # EU-123: capture provider info — label from the backend actually applied, not a global sniff.
    from . import provider as _provider
    model = getattr(options, "model", "") or ""
    provider, model_version = _provider.get_provider_info(model, backend=effective_backend)

    import time as _time
    _t0 = _time.monotonic()

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            parts: list[str] = []
            for b in message.content:
                if isinstance(b, TextBlock):
                    parts.append(b.text)
                elif ToolUseBlock is not None and isinstance(b, ToolUseBlock):
                    brief = _tool_brief(getattr(b, "name", ""), getattr(b, "input", None))
                    tools.append(brief)
                    if tag:
                        print(f"      · {tag}: {brief}", flush=True)
            text = "".join(parts)
            if text:
                chunks.append(text)
                final = text
            # EU-118: detect plan-limit errors in message errors
            if getattr(message, "error", None):
                is_error = True
                kind = _classify_plan_limit(str(getattr(message, "error", "")))
                if kind:
                    is_plan_limit = True
                    if plan_limit_kind != "cap":  # a cap sighting outranks a transient one
                        plan_limit_kind = kind
        elif isinstance(message, ResultMessage):
            cost = message.total_cost_usd or 0.0
            turns = message.num_turns
            is_error = is_error or message.is_error
            if message.result:
                final = message.result
            u = getattr(message, "usage", None)
            if isinstance(u, dict):
                in_tok = (int(u.get("input_tokens", 0) or 0)
                          + int(u.get("cache_read_input_tokens", 0) or 0)
                          + int(u.get("cache_creation_input_tokens", 0) or 0))
                out_tok = int(u.get("output_tokens", 0) or 0)

    duration_s = round(_time.monotonic() - _t0, 2)

    # One choke-point for the token ledger: every officer/builder/soldier/chat call lands here.
    try:
        from . import usage as _usage
        _usage.record(getattr(options, "model", "") or "", in_tok, out_tok, cost, tag,
                      ticket_id=ticket_id, pass_number=pass_number, provider=provider,
                      duration_s=duration_s)
    except Exception:  # noqa: BLE001 — metering must never break a run
        pass

    # QW4: per-call instrumentation into audit.jsonl — model, tokens in/out, duration (the three
    # fields the 2026-07-05 forensics found missing from every one of the 2,555 audit events).
    if _AUDIT_SINK is not None:
        try:
            extra = {}
            if ticket_id:
                extra["ticket_id"] = str(ticket_id)
            if pass_number is not None:
                extra["pass_number"] = pass_number
            _AUDIT_SINK.record("agent_call", tag=tag or "",
                               model=getattr(options, "model", "") or "",
                               provider=provider, model_version=model_version,
                               input_tokens=in_tok, output_tokens=out_tok,
                               cost_usd=round(cost, 6), duration_s=duration_s,
                               turns=turns, **extra)
        except Exception:  # noqa: BLE001 — instrumentation must never break a run
            pass

    return AgentRun(text="\n".join(chunks), final=final, cost_usd=cost,
                    num_turns=turns, is_error=is_error, tools=tools,
                    input_tokens=in_tok, output_tokens=out_tok, is_plan_limit=is_plan_limit,
                    plan_limit_kind=plan_limit_kind,
                    provider=provider, model_version=model_version, duration_s=duration_s)


# ── Sonnet-cap → one-shot Opus retry ─────────────────────────────────────────────────────────────────
# When Sonnet hits a CAP-classified plan-limit error, we retry the CURRENT work once with Opus to
# distinguish between:
#   • Sonnet sub-limit (Opus succeeds cleanly) → return the Opus result, persist nothing
#   • All-models cap (Opus also fails) → pause (return the is_plan_limit result; EU-82)
# Transient rate limits (per-minute 429, 529 overload) get one backoff retry on Sonnet instead.
# This is a thin wrapper around run_agent that adds the retry logic. The EU-108 weekly-Opus PIN was
# deleted (Phase-2 Task 2): a Sonnet cap must never pin a week of the most expensive tier.

# Backoff before the single Sonnet retry on a transient rate limit. Module-level so tests
# (and an operator in a pinch) can zero it out.
_TRANSIENT_RETRY_BACKOFF_S = 5.0


async def run_agent_with_fallback(prompt: str, options: ClaudeAgentOptions, tag: str = "",
                                   ticket_id: str | None = None, pass_number: int | None = None,
                                   cfg=None, routing_tier: str | None = None) -> AgentRun:
    """Run a Sonnet agent with a per-call, one-shot Opus fallback on a Sonnet weekly cap.

    When a Sonnet call hits a plan-limit:
      1. Transient rate limit (per-minute 429 / 529 overload) → back off once and retry
         Sonnet; never probe Opus.
      2. Cap-classified error ("usage limit reached" etc.) → retry once with Opus so the
         CURRENT unit of work can finish if Opus still has headroom.
      3. If Opus succeeds cleanly (is_error False) → return the Opus result, persisting NOTHING.
         The next call starts cheap on Sonnet again. (The EU-108 weekly-Opus pin was deleted in
         Phase-2 Task 2 — a Sonnet cap must never pin a week of the most expensive tier.)
      4. If Opus also hits a limit → it's the All-models cap: return the original is_plan_limit
         result so autopilot's proactive poll pauses (EU-82). If the Opus probe fails for any other
         reason (auth/network), that proves nothing about the cap — return the Sonnet error.

    Args:
        prompt: The agent prompt
        options: ClaudeAgentOptions with model, system_prompt, etc.
        tag: Tag for usage ledger (e.g. "builder", "reviewer")
        ticket_id: Optional ticket ID for per-pass tracking
        pass_number: Optional pass number for per-pass tracking
        cfg: Config object (passed through for ledger/audit context)

    Returns:
        AgentRun with the result (either from Sonnet or the one-shot Opus retry)
    """
    from .models import OPUS
    from . import backends as _backends

    # EU-189: under a GLM run there is no Sonnet-cap → native-Opus fallback (that semantics is
    # Anthropic-only and would burn the Max subscription the operator chose GLM to avoid). Read the
    # run's backend from cfg first (robust — cfg is passed by builder/reviewer) then the contextvar;
    # if GLM, run straight through with no Opus probe. If cfg says GLM but the run-scoped contextvar
    # wasn't pinned (a caller outside loop.run, or across a context-dropping boundary), pin it for
    # this call so the seam in _run_agent_unrouted actually applies GLM instead of silently running
    # Opus — keeps the cfg-view and the contextvar-view of the backend consistent (security review).
    if _backends.is_glm(cfg):
        _tok = None if _backends.current() == _backends.GLM else _backends.set_backend(_backends.GLM)
        try:
            return await run_agent(prompt, options, tag=tag, ticket_id=ticket_id,
                                   pass_number=pass_number, routing_tier=routing_tier)
        finally:
            if _tok is not None:
                _backends.reset_backend(_tok)

    # Get the configured model
    model = getattr(options, "model", "") or ""

    # The Sonnet-cap → one-shot-Opus fallback only applies to Sonnet calls.
    is_sonnet = model and "sonnet" in model.lower()

    if not is_sonnet:
        # Not Sonnet — nothing to fall back from; run normally.
        return await run_agent(prompt, options, tag=tag, ticket_id=ticket_id, pass_number=pass_number, routing_tier=routing_tier)

    # Try Sonnet first
    result = await run_agent(prompt, options, tag=tag, ticket_id=ticket_id, pass_number=pass_number, routing_tier=routing_tier)

    # If Sonnet succeeded or hit a non-plan-limit error, return as-is
    if not result.is_plan_limit:
        return result

    # Transient rate limit (per-minute 429, 529 overload) — not a quota exhaustion. Back off
    # and retry Sonnet once; probing Opus over a blip would burn the expensive tier needlessly.
    if result.plan_limit_kind != "cap":
        if _TRANSIENT_RETRY_BACKOFF_S > 0:
            import asyncio
            print(f"  · transient rate-limit on {model} — retrying once after "
                  f"{_TRANSIENT_RETRY_BACKOFF_S:.0f}s (no weekly fallback)", flush=True)
            await asyncio.sleep(_TRANSIENT_RETRY_BACKOFF_S)
        return await run_agent(prompt, options, tag=tag, ticket_id=ticket_id,
                               pass_number=pass_number, routing_tier=routing_tier)

    # Sonnet hit a cap-classified plan-limit error — try Opus once to distinguish the limit type.
    # 2026-07-05 audit §6 defect 1: the old manual rebuild here dropped cwd, hooks (the guard
    # denylist) and disallowed_tools — the Opus probe ran in the orchestrator's own CWD with no
    # guard under the inherited bypassPermissions, and its output was used as the build result.
    # A shallow copy preserves every field by construction; the model is the only difference.
    import copy
    opus_options = copy.copy(options)
    opus_options.model = OPUS

    # Try Opus once. Deliberately NOT passing routing_tier: the retry's whole point is to run on
    # Anthropic Opus — routing it (CLOUD would rewrite the model to glm-5.2) would re-run the routed
    # model instead of the Opus retry we intend (2026-07-06 review).
    opus_result = await run_agent(prompt, opus_options, tag=tag, ticket_id=ticket_id, pass_number=pass_number)

    if opus_result.is_plan_limit:
        # Opus also hit a limit — this is the All-models cap, not just Sonnet.
        # Return the original Sonnet error so autopilot's proactive poll pauses (EU-82).
        return result

    if opus_result.is_error:
        # The retry itself failed (auth, network, …) — that proves nothing about the Sonnet
        # cap. Surface the original Sonnet plan-limit error so upstream handling (EU-82 pause)
        # still sees it.
        return result

    # Opus succeeded where Sonnet was capped — a Sonnet-tier sub-limit, not an All-models cap.
    # Return the Opus result so THIS unit of work completes. Persist NOTHING: the EU-108 weekly-Opus
    # pin was deleted (Phase-2 Task 2) because pinning the most expensive tier for a week is
    # anti-economics. The next call starts cheap on Sonnet again; if Sonnet is still capped it
    # independently retries Opus per-call, and an All-models cap surfaces above as is_plan_limit →
    # EU-82 pause. Never a week of pinned Opus.
    print(f"  · Sonnet cap on {tag or model} — this pass ran on Opus (no weekly pin)", flush=True)
    return opus_result
