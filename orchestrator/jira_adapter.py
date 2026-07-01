"""Jira ticket comment summarizer using cheap LLMs.

Posts concise, high-value summaries of critical gate outcomes so the Commander
sees what actually happened without drowning in noise.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .config import Config


@dataclass
class GateComment:
    """Structured comment object for Jira gate events."""
    gate: str
    status: str
    summary: str
    timestamp: str


class TicketCommenter:
    """LLM-powered ticket commenter for gate events."""

    # Rate limit: max 5 comments per ticket per gate cycle
    MAX_COMMENTS_PER_CYCLE = 5

    def __init__(self, cfg: Config, dry_run: bool = False, no_comment: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.no_comment = no_comment
        self.comment_counts: dict[str, int] = {}  # ticket_id -> count in current cycle

    def _get_api_key(self) -> str | None:
        """Get Anthropic API key from environment."""
        return os.environ.get("ANTHROPIC_API_KEY")

    def _call_haiku(self, prompt: str, system: str = "") -> str | None:
        """Call Claude Haiku for summarization.

        Uses the Agent SDK directly via HTTP API for minimal overhead.
        Returns the summary text or None on failure.
        """
        api_key = self._get_api_key()
        if not api_key:
            print("  · ticket commenter: ANTHROPIC_API_KEY not set — skipping LLM call", flush=True)
            return None

        try:
            import httpx

            # Haiku model ID
            model = "claude-3-haiku-20240307"
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }

            payload = {
                "model": model,
                "max_tokens": 300,
                "temperature": 0.3,
                "system": system,
                "messages": [{"role": "user", "content": prompt}]
            }

            # 5-second timeout for summarization (don't block the pipeline)
            with httpx.Client(timeout=5.0) as client:
                response = client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers=headers,
                    json=payload
                )
                response.raise_for_status()
                result = response.json()
                return result.get("content", [{}])[0].get("text", "").strip()

        except Exception as exc:
            print(f"  · ticket commenter: LLM call failed ({exc})", flush=True)
            return None

    def _should_post_comment(self, ticket_id: str) -> bool:
        """Check if we should post a comment (rate limit + flags)."""
        if self.no_comment:
            return False

        # Reset counter for new ticket cycle
        if ticket_id not in self.comment_counts:
            self.comment_counts[ticket_id] = 0

        # Enforce rate limit
        if self.comment_counts[ticket_id] >= self.MAX_COMMENTS_PER_CYCLE:
            print(f"  · ticket commenter: rate limit reached for {ticket_id} (max {self.MAX_COMMENTS_PER_CYCLE} per cycle)",
                  flush=True)
            return False

        return True

    def _increment_counter(self, ticket_id: str) -> None:
        """Increment the comment counter for a ticket."""
        self.comment_counts[ticket_id] = self.comment_counts.get(ticket_id, 0) + 1

    def summarize_gate_event(self, gate: str, status: str, details: str,
                            ticket_id: str) -> GateComment | None:
        """Summarize a gate event using LLM.

        Args:
            gate: Gate name (e.g., "Build", "Review", "Security")
            status: Gate status (e.g., "FAILED", "PASSED", "BLOCKED")
            details: Raw gate output/details to summarize
            ticket_id: Ticket ID for rate limiting

        Returns:
            GateComment object if summarization succeeded, None otherwise
        """
        if not self._should_post_comment(ticket_id):
            return None

        # Skip LLM call for trivial cases (save money)
        if status == "PASSED" and len(details) < 200:
            summary = f"{gate} passed successfully"
        elif status == "PASSED":
            summary = self._summarize_with_llm(gate, status, details)
            if not summary:
                summary = f"{gate} passed"
        else:
            summary = self._summarize_with_llm(gate, status, details)
            if not summary:
                # Fallback to simple template
                summary = f"{gate} {status.lower()}: {(details or '')[:200]}"

        comment = GateComment(
            gate=gate,
            status=status,
            summary=summary,
            timestamp=datetime.now().isoformat()
        )

        self._increment_counter(ticket_id)
        return comment

    def _summarize_with_llm(self, gate: str, status: str, details: str) -> str | None:
        """Use LLM to extract high-signal events from gate output."""
        system_prompt = (
            "You are a concise engineering summarizer. Extract ONLY the most critical "
            "information from gate events. Focus on:\n"
            "- What failed and why\n"
            "- What passed (only if notable)\n"
            "- What needs to be done\n"
            "\n"
            "Output format: ONE line, max 80 characters, no preamble. "
            "Be specific but brief."
        )

        user_prompt = f"Gate: {gate}\nStatus: {status}\n\nOutput:\n{details[:1500]}\n\nSummarize in ONE line (max 80 chars)."

        summary = self._call_haiku(user_prompt, system=system_prompt)
        if summary:
            # Truncate to 80 chars
            summary = summary[:80]
            # Remove common LLM prefixes
            for prefix in ["Summary:", "Result:", "Output:", "- "]:
                if summary.startswith(prefix):
                    summary = summary[len(prefix):].strip()

        return summary

    def format_comment(self, comment: GateComment) -> str:
        """Format a GateComment for Jira posting."""
        emoji = {
            "PASSED": "✅",
            "FAILED": "❌",
            "BLOCKED": "🛑",
            "ESCALATED": "🔥",
            "RETRY": "🔄",
        }.get(comment.status.upper(), "•")

        return f"{emoji} {comment.gate}: {comment.summary}"

    def post_comment(self, backlog, ticket_id: str, comment: GateComment) -> bool:
        """Post a summarized comment to Jira.

        Args:
            backlog: BacklogAdapter instance
            ticket_id: Ticket ID/key
            comment: GateComment to post

        Returns:
            True if comment was posted (or skipped in dry-run), False on error
        """
        if not self._should_post_comment(ticket_id):
            return False

        formatted = self.format_comment(comment)

        if self.dry_run:
            print(f"  · ticket commenter (dry-run): would post to {ticket_id}: {formatted}", flush=True)
            return True

        try:
            # Post to Jira via backlog adapter
            backlog.add_comment(
                type("Ticket", (), {"key": ticket_id})(),
                formatted
            )
            print(f"  · ticket commenter: posted to {ticket_id}: {formatted}", flush=True)
            return True
        except Exception as exc:
            print(f"  · ticket commenter: failed to post to {ticket_id}: {exc}", flush=True)
            return False

    def reset_cycle(self, ticket_id: str) -> None:
        """Reset comment counter for a new gate cycle."""
        if ticket_id in self.comment_counts:
            self.comment_counts[ticket_id] = 0
