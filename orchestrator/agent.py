"""Shared Agent SDK helper.

Wraps `claude_agent_sdk.query()`, collects the assistant text + cost/turn metadata,
and (when a `tag` is given) streams a compact line per tool use so you can see the
officer working in real time.

EU-108 Sonnet-cap fallback:
  When a Sonnet call hits a plan-limit error (HTTP 429 / usage-limit), the fallback
  logic retries with Opus once to distinguish between:
    • Sonnet sub-limit hit → Opus succeeds → activate fallback, stay on Opus until reset
    • All-models cap hit → Opus also 429s → pause (existing EU-82 governor behavior)
"""
from __future__ import annotations

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
    # The error detection looks for patterns in the Agent SDK's message.error field:
    #   • "429" — HTTP status code for rate-limit/plan-limit errors
    #   • "limit reached" — common error message when plan quota is exhausted
    #   • "usage-limit" / "rate limit" / "plan limit" / "over limit" — alternative error formats
    # These patterns are based on observed Agent SDK error behavior; the SDK surfaces
    # Anthropic API errors (HTTP 429 with "rate limit" or "usage limit" details) via
    # the message.error field when a plan/session/weekly quota is exceeded.
    is_plan_limit: bool = False
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

    # EU-174: Hybrid routing - handle tier-based endpoint switching
    import os as _os
    _original_base_url = None
    _original_api_key = None
    _original_model = None

    if routing_tier:
        from . import routing as _routing
        tier = _routing.RoutingTier(routing_tier)

        # Save original environment values
        _original_base_url = _os.environ.get("ANTHROPIC_BASE_URL")
        _original_api_key = _os.environ.get("ANTHROPIC_API_KEY")
        _original_model = getattr(options, "model", None)

        # Set tier-specific values
        if tier == _routing.RoutingTier.LOCAL:
            # Route to local Ollama
            base_url = _routing.get_base_url_for_tier(tier)
            api_key = _routing.get_api_key_for_tier(tier)
            model = _routing.get_model_for_tier(tier)

            if base_url:
                _os.environ["ANTHROPIC_BASE_URL"] = base_url
            if api_key:
                _os.environ["ANTHROPIC_API_KEY"] = api_key
            # Update the model in options
            if hasattr(options, "model"):
                options.model = model
        else:
            # Route to cloud - use defaults or explicit cloud settings
            cloud_model = _routing.get_model_for_tier(tier)
            if cloud_model and hasattr(options, "model"):
                options.model = cloud_model

    chunks: list[str] = []
    tools: list[str] = []
    final = ""
    cost = 0.0
    turns = 0
    in_tok = 0
    out_tok = 0
    is_error = False
    is_plan_limit = False

    # EU-123: capture provider info from the model configuration
    from . import provider as _provider
    model = getattr(options, "model", "") or ""
    provider, model_version = _provider.get_provider_info(model)

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
                err = str(getattr(message, "error", "")).lower()
                # Check for plan-limit error patterns: 429 status, usage-limit mentions
                if any(pattern in err for pattern in ["429", "limit reached", "usage-limit", "rate limit", "plan limit", "over limit"]):
                    is_plan_limit = True
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

    # EU-174: Restore original environment variables after routing
    if routing_tier and _original_base_url is not None:
        if _original_base_url is not None:
            _os.environ["ANTHROPIC_BASE_URL"] = _original_base_url
        elif "ANTHROPIC_BASE_URL" in _os.environ:
            del _os.environ["ANTHROPIC_BASE_URL"]

        if _original_api_key is not None:
            _os.environ["ANTHROPIC_API_KEY"] = _original_api_key
        elif "ANTHROPIC_API_KEY" in _os.environ:
            del _os.environ["ANTHROPIC_API_KEY"]

        if _original_model is not None and hasattr(options, "model"):
            options.model = _original_model

    return AgentRun(text="\n".join(chunks), final=final, cost_usd=cost,
                    num_turns=turns, is_error=is_error, tools=tools,
                    input_tokens=in_tok, output_tokens=out_tok, is_plan_limit=is_plan_limit,
                    provider=provider, model_version=model_version, duration_s=duration_s)


# ── EU-108: Sonnet-cap fallback detection ────────────────────────────────────────────────────────────
# When Sonnet hits a plan-limit error, we retry with Opus to distinguish between:
#   • Sonnet sub-limit (Opus succeeds) → activate fallback
#   • All-models cap (Opus also fails) → pause (existing behavior)
# This is a thin wrapper around run_agent that adds the retry logic.

async def run_agent_with_fallback(prompt: str, options: ClaudeAgentOptions, tag: str = "",
                                   ticket_id: str | None = None, pass_number: int | None = None,
                                   cfg=None, routing_tier: str | None = None) -> AgentRun:
    """Run an agent with Sonnet→Opus fallback on plan-limit errors.

    When a Sonnet call hits a 429/usage-limit error AND the config flag is enabled:
      1. Retry once with Opus (same prompt, options)
      2. If Opus succeeds → activate Sonnet-cap fallback (stay on Opus until weekly reset)
      3. If Opus also fails → it's the All-models cap (existing pause behavior)

    Args:
        prompt: The agent prompt
        options: ClaudeAgentOptions with model, system_prompt, etc.
        tag: Tag for usage ledger (e.g. "builder", "reviewer")
        ticket_id: Optional ticket ID for per-pass tracking
        pass_number: Optional pass number for per-pass tracking
        cfg: Config object (needed to check opus_fallback_on_sonnet_cap flag)

    Returns:
        AgentRun with the result (either from Sonnet or Opus fallback)
    """
    from .models import SONNET, OPUS, activate_sonnet_fallback, _get_next_friday_0900_utc, \
                      sonnet_fallback_notification_sent, mark_sonnet_fallback_notified, \
                      fallback_reset_time_str

    # Check if fallback is enabled in config
    fallback_enabled = cfg and getattr(cfg, "opus_fallback_on_sonnet_cap", True)

    # Get the configured model
    model = getattr(options, "model", "") or ""

    # Only apply fallback logic for Sonnet with config enabled
    is_sonnet = model and "sonnet" in model.lower()

    if not is_sonnet or not fallback_enabled:
        # Not Sonnet or fallback disabled — run normally
        return await run_agent(prompt, options, tag=tag, ticket_id=ticket_id, pass_number=pass_number, routing_tier=routing_tier)

    # Try Sonnet first
    result = await run_agent(prompt, options, tag=tag, ticket_id=ticket_id, pass_number=pass_number, routing_tier=routing_tier)

    # If Sonnet succeeded or hit a non-plan-limit error, return as-is
    if not result.is_plan_limit:
        return result

    # Sonnet hit a plan-limit error — try Opus once to distinguish the limit type
    # Clone options and switch to Opus
    from claude_agent_sdk import ClaudeAgentOptions
    opus_options = ClaudeAgentOptions(
        model=OPUS,
        system_prompt=getattr(options, "system_prompt", ""),
        permission_mode=getattr(options, "permission_mode", None),
        allowed_tools=getattr(options, "allowed_tools", None),
        setting_sources=getattr(options, "setting_sources", None),
        max_turns=getattr(options, "max_turns", None),
        effort=getattr(options, "effort", None),
    )

    # Try Opus once
    opus_result = await run_agent(prompt, opus_options, tag=tag, ticket_id=ticket_id, pass_number=pass_number, routing_tier=routing_tier)

    if opus_result.is_plan_limit:
        # Opus also hit a limit — this is the All-models cap, not just Sonnet
        # Return the original Sonnet error (existing EU-82 pause behavior)
        return result

    # Opus succeeded — it was a Sonnet sub-limit hit!
    # Activate the fallback state (stay on Opus until weekly reset)
    reset_at = _get_next_friday_0900_utc()
    activate_sonnet_fallback(reset_at, cfg=cfg)

    # Send one-time notification if not already sent
    if not sonnet_fallback_notification_sent() and cfg:
        try:
            from . import notify
            reset_str = fallback_reset_time_str()
            message = f"⚠️ Sonnet weekly cap hit → Opus fallback active (resets {reset_str})"
            notify.send(message)
            mark_sonnet_fallback_notified(cfg=cfg)

            # Also log to cockpit
            print(f"  · {message}", flush=True)
        except Exception:  # noqa: BLE001 — notification must never break a run
            pass

    # Return Opus result (successful) with Opus provider info
    # The opus_result already has provider="Anthropic" and model_version from run_agent
    return opus_result
