"""OFFICER_NAMES single source of truth (EU-40 slice): the internal-key -> display-name map
applies the EU-17 / EU-23 rename, keeps internal keys stable, and is importable from config."""
import sys

sys.path.insert(0, ".")
from orchestrator.officers import OFFICER_NAMES, display
from orchestrator.config import OFFICER_NAMES as CFG_NAMES, officer_display

print("=== config re-export is the same object ===")
assert CFG_NAMES is OFFICER_NAMES, "config must re-export the one map, not a copy"
assert officer_display is display

print("=== rename map applied (old army name -> new display) ===")
EXPECTED = {
    "general": "CTO",
    "field_engineer": "Dev Team Lead",
    "inspector": "Code Reviewer",
    "adjutant": "Engineering Manager",
    "provost": "Security Engineer",
    "quartermaster": "Release Manager",
    "sentinel": "SRE",
    "drillmaster": "Engineering Coach",
    "scout": "QA Engineer",
    "scribe": "Technical Writer",
    "soldiers": "engineers",
    # unchanged
    "pm": "Product Manager",
    "devops": "DevOps",
    "test_engineer": "Test Engineer",
}
for key, name in EXPECTED.items():
    assert OFFICER_NAMES.get(key) == name, f"{key} -> {OFFICER_NAMES.get(key)} (expected {name})"
    assert display(key) == name

print("=== no old army names leaked into any display value ===")
OLD = ("General", "Field Engineer", "Inspector", "Adjutant", "Provost",
       "Quartermaster", "Sentinel", "Drillmaster", "Scout", "Scribe", "soldier")
for v in OFFICER_NAMES.values():
    assert not any(o in v for o in OLD), f"old army name leaked in display value: {v!r}"

print("=== unknown key falls back to itself (never crashes) ===")
assert display("does_not_exist") == "does_not_exist"

print("ALL OK", len(OFFICER_NAMES), "officers")
