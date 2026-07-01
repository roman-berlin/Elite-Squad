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


def _tool_brief(name: str, inp) -> str:
    if isinstance(inp, dict):
        for k in ("file_path", "path", "command", "pattern", "url"):
            v = inp.get(k)
            if v:
                return f"{name} {str(v).splitlines()[0][:72]}"
    return name or "tool"


async def run_agent(prompt: str, options: ClaudeAgentOptions, tag: str = "",
                    ticket_id: str | None = None, pass_number: int | None = None) -> AgentRun:
    # EU-38: `ticket_id` + `pass_number` let a build/soldier pass tag its ledger line so per-pass
    # input tokens are sliceable by ticket (the real cost lever). Optional + keyword-defaulted, so
    # every existing caller (officers/chat that pass only `tag`) is unaffected.
    chunks: list[str] = []
    tools: list[str] = []
    final = ""
    cost = 0.0
    turns = 0
    in_tok = 0
    out_tok = 0
    is_error = False
    is_plan_limit = False

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

    # One choke-point for the token ledger: every officer/builder/soldier/chat call lands here.
    try:
        from . import usage as _usage
        _usage.record(getattr(options, "model", "") or "", in_tok, out_tok, cost, tag,
                      ticket_id=ticket_id, pass_number=pass_number)
    except Exception:  # noqa: BLE001 — metering must never break a run
        pass

    return AgentRun(text="\n".join(chunks), final=final, cost_usd=cost,
                    num_turns=turns, is_error=is_error, tools=tools,
                    input_tokens=in_tok, output_tokens=out_tok, is_plan_limit=is_plan_limit)


# ── EU-108: Sonnet-cap fallback detection ────────────────────────────────────────────────────────────
# When Sonnet hits a plan-limit error, we retry with Opus to distinguish between:
#   • Sonnet sub-limit (Opus succeeds) → activate fallback
#   • All-models cap (Opus also fails) → pause (existing behavior)
# This is a thin wrapper around run_agent that adds the retry logic.

async def run_agent_with_fallback(prompt: str, options: ClaudeAgentOptions, tag: str = "",
                                   ticket_id: str | None = None, pass_number: int | None = None,
                                   cfg=None) -> AgentRun:
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
        return await run_agent(prompt, options, tag=tag, ticket_id=ticket_id, pass_number=pass_number)

    # Try Sonnet first
    result = await run_agent(prompt, options, tag=tag, ticket_id=ticket_id, pass_number=pass_number)

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
    opus_result = await run_agent(prompt, opus_options, tag=tag, ticket_id=ticket_id, pass_number=pass_number)

    if opus_result.is_plan_limit:
        # Opus also hit a limit — this is the All-models cap, not just Sonnet
        # Return the original Sonnet error (existing EU-82 pause behavior)
        return result

    # Opus succeeded — it was a Sonnet sub-limit hit!
    # Activate the fallback state (stay on Opus until weekly reset)
    reset_at = _get_next_friday_0900_utc()
    activate_sonnet_fallback(reset_at)

    # Send one-time notification if not already sent
    if not sonnet_fallback_notification_sent() and cfg:
        try:
            from . import notify
            reset_str = fallback_reset_time_str()
            message = f"⚠️ Sonnet weekly cap hit → Opus fallback active (resets {reset_str})"
            notify.send(message)
            mark_sonnet_fallback_notified()

            # Also log to cockpit
            print(f"  · {message}", flush=True)
        except Exception:  # noqa: BLE001 — notification must never break a run
            pass

    # Return Opus result (successful)
    return opus_result
