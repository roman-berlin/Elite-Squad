"""Officer names — the single source of truth for human-facing display names.

Internal keys (``provost``, ``quartermaster``, ``scout`` …) are STABLE identifiers used
throughout the codebase (module names, role lookups, agent ids); they never change. Only the
human-facing *display* name attached to each key changes. The EU-17 / EU-23 rename retired the
army metaphor for plain software-team titles — this map is the one place those titles live, so
nothing else in the unit hard-codes an officer name. Everything that shows an officer to a human
imports ``OFFICER_NAMES`` (or calls :func:`display`) from here.

Rename map (old army name -> new display name) applied below:
    The General        -> CTO
    Field Engineer     -> Dev Team Lead
    Inspector General  -> Code Reviewer
    Adjutant           -> Engineering Manager
    Provost Marshal    -> Security Engineer
    Quartermaster      -> Release Manager
    Sentinel           -> SRE
    Drillmaster        -> Engineering Coach
    Scout              -> QA Engineer
    Scribe             -> Technical Writer
    soldiers           -> engineers
    Product Manager / DevOps / Test Engineer -> unchanged
"""
from __future__ import annotations

# internal key -> human-facing display name. Keys are immutable; only the values are renamed.
OFFICER_NAMES: dict[str, str] = {
    "general": "CTO",
    "adjutant": "Engineering Manager",
    "pm": "Product Manager",
    "senior_pm": "Senior PM",
    "field_engineer": "Dev Team Lead",
    "inspector": "Code Reviewer",
    "test_engineer": "Test Engineer",
    "scout": "QA Engineer",
    "provost": "Security Engineer",
    "quartermaster": "Release Manager",
    "sentinel": "SRE",
    "drillmaster": "Engineering Coach",
    "scribe": "Technical Writer",
    "devops": "DevOps",
    "soldiers": "engineers",   # the squad members a Dev Team Lead fields (plural / collective)
    "liaison": "Mayor",        # EU-66 inter-unit ambassador (friendly outward voice, no task execution)
}


def display(key: str) -> str:
    """Return the human-facing display name for an internal officer key.

    Falls back to the key itself (unknown keys shouldn't reach here, but a caller must never
    crash over a label lookup)."""
    return OFFICER_NAMES.get(key, key)
