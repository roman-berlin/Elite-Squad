"""Shared Agent SDK helper.

Wraps `claude_agent_sdk.query()`, collects the assistant text + cost/turn metadata,
and (when a `tag` is given) streams a compact line per tool use so you can see the
officer working in real time.
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
            if getattr(message, "error", None):
                is_error = True
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
                    input_tokens=in_tok, output_tokens=out_tok)
