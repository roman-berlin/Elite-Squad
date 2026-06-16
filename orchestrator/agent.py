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


def _tool_brief(name: str, inp) -> str:
    if isinstance(inp, dict):
        for k in ("file_path", "path", "command", "pattern", "url"):
            v = inp.get(k)
            if v:
                return f"{name} {str(v).splitlines()[0][:72]}"
    return name or "tool"


async def run_agent(prompt: str, options: ClaudeAgentOptions, tag: str = "") -> AgentRun:
    chunks: list[str] = []
    tools: list[str] = []
    final = ""
    cost = 0.0
    turns = 0
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

    return AgentRun(text="\n".join(chunks), final=final, cost_usd=cost,
                    num_turns=turns, is_error=is_error, tools=tools)
