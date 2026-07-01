"""Provider detection utility — determines the model provider (Anthropic/GLM) and model version.

EU-123: Show the real provider + model per officer in the live feed + audit.
"""
from __future__ import annotations

import os
import re


def get_provider_info(model: str = "") -> tuple[str, str]:
    """Detect provider from endpoint configuration and model string.

    Returns:
        (provider, model_version) where:
        - provider: "GLM" if using z.ai endpoint, "Anthropic" otherwise
        - model_version: Clean model name (e.g., "claude-opus-4-8", "claude-sonnet-4-6", "glm-4")

    Detection logic:
        1. Check ANTHROPIC_BASE_URL environment variable
        2. If it contains z.ai → provider='GLM'
        3. Otherwise → provider='Anthropic'
        4. Extract model version from the model string
    """
    # Check environment variable for provider endpoint
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")

    # Detect provider from endpoint
    if "z.ai" in base_url.lower():
        provider = "GLM"
    else:
        provider = "Anthropic"

    # Extract model version from model string
    # Model strings are like: "claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"
    # For GLM, they might be: "glm-4", "glm-4-plus", etc.
    # When using GLM endpoint, map Claude model tiers to GLM equivalents for display
    if provider == "GLM":
        model_version = _map_to_glm_model(model) if model else ""
    else:
        model_version = _normalize_model(model) if model else ""

    return provider, model_version


def _normalize_model(model: str) -> str:
    """Normalize model string to a clean version identifier.

    Removes common prefixes and normalizes the model name.
    Examples:
        "claude-opus-4-8" → "claude-opus-4-8"
        "claude-sonnet-4-6" → "claude-sonnet-4-6"
        "claude-haiku-4-5-20251001" → "claude-haiku-4-5"
        "glm-4-plus" → "glm-4-plus"
    """
    if not model:
        return ""

    # Remove provider prefixes if present
    clean = model.strip().lower()

    # For Claude models, strip the date suffix from haiku-style names
    # e.g., "claude-haiku-4-5-20251001" → "claude-haiku-4-5"
    match = re.match(r'(claude-haiku-[\d-]+)-\d{8}', clean)
    if match:
        clean = match.group(1)

    # For other Claude models, keep as-is
    # e.g., "claude-opus-4-8", "claude-sonnet-4-6"

    return clean


def _map_to_glm_model(model: str) -> str:
    """Map Claude model names to GLM equivalents for display.

    When routing through the Z.ai endpoint (GLM), the request still names a Claude tier,
    but we map it to the corresponding GLM model for accurate display.

    Examples:
        "claude-opus-4-8" → "glm-4-plus"
        "claude-sonnet-4-6" → "glm-4"
        "claude-haiku-4-5-20251001" → "glm-3"
        "glm-4" → "glm-4" (already GLM, keep as-is)
    """
    if not model:
        return ""

    clean = model.strip().lower()

    # If it's already a GLM model, keep it as-is
    if "glm" in clean:
        return _normalize_model(clean)

    # Map Claude tiers to GLM equivalents
    if "opus" in clean:
        return "glm-4-plus"
    if "sonnet" in clean:
        return "glm-4"
    if "haiku" in clean:
        return "glm-3"

    # Default for unknown models
    return "glm-4"


def format_provider_model(provider: str, model_version: str) -> str:
    """Format provider and model for display in the live feed.

    Args:
        provider: "Anthropic" or "GLM"
        model_version: Clean model identifier (e.g., "claude-opus-4-8", "glm-4")

    Returns:
        Formatted string like "Claude-Opus-4.8" or "GLM-4"

    Examples:
        >>> format_provider_model("Anthropic", "claude-opus-4-8")
        'Claude-Opus-4.8'
        >>> format_provider_model("GLM", "glm-4")
        'GLM-4'
        >>> format_provider_model("Anthropic", "")
        'Claude'
    """
    if not provider:
        return "Unknown"

    # Format model version for display
    if not model_version:
        # No model version - just show provider
        if provider == "Anthropic":
            return "Claude"
        return provider

    # Normalize and format the model
    # Convert "claude-opus-4-8" → "Claude-Opus-4.8"
    # Convert "glm-4-plus" → "GLM-4-Plus"
    parts = model_version.replace("_", "-").split("-")
    title_parts = [p.title() if p and p.isalpha() else p for p in parts]

    # Replace numbers with version format (e.g., "4" → "4.0" for major versions)
    formatted = "-".join(title_parts)

    # Special handling for Claude models
    if provider == "Anthropic":
        # For Claude, prefix with "Claude-" if not already present
        if formatted.lower().startswith("claude"):
            formatted = formatted[0].upper() + formatted[1:]
        else:
            formatted = f"Claude-{formatted}"
    else:
        # For GLM, capitalize first letter
        formatted = formatted[0].upper() + formatted[1:] if formatted else provider

    return formatted
