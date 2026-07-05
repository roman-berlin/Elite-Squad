"""Hybrid LLM Routing — Tier 1 (Local Ollama) / Tier 2 (Cloud).

EU-174: Route requests between local and cloud models to optimize costs.
  • Tier 1 (Local): Ollama with glm-4.7-flash:q4_K_M for routine tasks
  • Tier 2 (Cloud): GLM-5.2 for complex architectural work

The routing layer decides which tier to use based on task characteristics:
  • Local Tier 1: data parsing, routine tests, script execution, boilerplate
  • Cloud Tier 2: multi-tenant logic, deep architectural changes, complex debugging
"""
from __future__ import annotations

import os
from enum import Enum
from typing import Literal, Optional


class RoutingTier(str, Enum):
    """Routing tiers for LLM requests."""
    LOCAL = "local"      # Tier 1: Local Ollama
    CLOUD = "cloud"      # Tier 2: Cloud proxy


# Task complexity indicators for routing decisions
_COMPLEXITY_KEYWORDS = {
    # Routes to CLOUD (Tier 2) - complex architectural work
    "tier2": [
        "multi-tenant",
        "tenant isolation",
        "tenant separation",
        "architectural",
        "architecture",
        "deep refactor",
        "database schema",
        "migration",
        "complex debugging",
        "production",
        "security review",
        "rls",
        "row level security",
        "supabase policy",
        "authentication",
        "authorization",
        "api redesign",
        "performance",
        "scalability",
        "critical bug",
        "investigation",
    ],
    # Routes to LOCAL (Tier 1) - routine work
    "tier1": [
        "data parsing",
        "parse",
        "format",
        "boilerplate",
        "test",
        "unit test",
        "script",
        "utility",
        "helper",
        "simple fix",
        "typo",
        "documentation",
        "comment",
        "rename",
        "move file",
        "update config",
    ],
}


def classify_task(
    ticket_description: str = "",
    task_type: str = "",
    effort: str = "",
    size: str = "",
) -> RoutingTier:
    """Classify a task to determine routing tier.

    Args:
        ticket_description: The ticket description text
        task_type: Type of task (e.g., "build", "review", "test")
        effort: Effort level (e.g., "low", "medium", "high")
        size: Ticket size (e.g., "XS", "S", "M", "L", "XL")

    Returns:
        RoutingTier.LOCAL for routine work, RoutingTier.CLOUD for complex work
    """
    # Start with all text to analyze
    text = f"{ticket_description} {task_type} {effort} {size}".lower()

    # Check for Tier 2 (cloud) indicators first - they take precedence
    for keyword in _COMPLEXITY_KEYWORDS["tier2"]:
        if keyword.lower() in text:
            return RoutingTier.CLOUD

    # Check for Tier 1 (local) indicators
    for keyword in _COMPLEXITY_KEYWORDS["tier1"]:
        if keyword.lower() in text:
            return RoutingTier.LOCAL

    # Default: use size/effort heuristics
    # Large/XL tickets or high/max effort → cloud
    if size.upper() in ("L", "XL") or effort.lower() in ("high", "max", "xhigh"):
        return RoutingTier.CLOUD

    # Small/medium tickets → local
    return RoutingTier.LOCAL


def get_base_url_for_tier(tier: RoutingTier) -> str:
    """Get the base URL for a given routing tier.

    Reads from environment variables:
      • ANTHROPIC_BASE_URL (primary/cloud endpoint)
      • OLLAMA_BASE_URL (local Ollama endpoint, optional)

    Args:
        tier: The routing tier (LOCAL or CLOUD)

    Returns:
        The base URL for the requested tier, or empty string if not configured
    """
    if tier == RoutingTier.LOCAL:
        # Local Ollama endpoint
        return os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    else:
        # Cloud endpoint (default Anthropic or GLM proxy)
        return os.environ.get("ANTHROPIC_BASE_URL", "")


def get_api_key_for_tier(tier: RoutingTier) -> str:
    """Get the API key for a given routing tier.

    Args:
        tier: The routing tier (LOCAL or CLOUD)

    Returns:
        The API key for the requested tier, or empty string if not configured
    """
    if tier == RoutingTier.LOCAL:
        # Ollama typically doesn't require an API key
        return os.environ.get("OLLAMA_API_KEY", "")
    else:
        # Cloud endpoint key
        return os.environ.get("ANTHROPIC_API_KEY", "")


def get_model_for_tier(tier: RoutingTier) -> str:
    """Get the model name for a given routing tier.

    Args:
        tier: The routing tier (LOCAL or CLOUD)

    Returns:
        The model identifier for the requested tier
    """
    if tier == RoutingTier.LOCAL:
        # Local Ollama model
        return os.environ.get("OLLAMA_MODEL", "glm-4.7-flash:q4_K_M")
    else:
        # Cloud model
        return os.environ.get("ANTHROPIC_MODEL", "glm-5.2")


def routing_tier_from_env() -> RoutingTier:
    """Get the default routing tier from environment variable.

    Reads ROUTING_TIER env var (default: "cloud").

    Returns:
        The configured routing tier
    """
    tier_str = os.environ.get("ROUTING_TIER", "cloud").lower()
    if tier_str == "local":
        return RoutingTier.LOCAL
    return RoutingTier.CLOUD


def is_routing_enabled() -> bool:
    """Check if hybrid routing is enabled.

    Returns:
        True if ROUTING_ENABLED is "true" or "1"
    """
    return os.environ.get("ROUTING_ENABLED", "false").lower() in ("true", "1", "yes")


def should_route_to_local(
    ticket_description: str = "",
    task_type: str = "",
    effort: str = "",
    size: str = "",
) -> bool:
    """Convenience function to check if a task should route to local Tier 1.

    Args:
        ticket_description: The ticket description text
        task_type: Type of task
        effort: Effort level
        size: Ticket size

    Returns:
        True if task should route to local, False for cloud
    """
    if not is_routing_enabled():
        return False

    tier = classify_task(ticket_description, task_type, effort, size)
    return tier == RoutingTier.LOCAL
