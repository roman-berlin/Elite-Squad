"""EU-57: officer role names are unified — one documented old->new map, locked to the SOT.

This harness guards the audit deliverable from drifting:
  1. The human-readable map in ``Documentation/OFFICER_NAMING.md`` covers EVERY officer and its NEW
     canonical name EXACTLY matches ``orchestrator.officers.OFFICER_NAMES`` (the one source of truth).
     If anyone renames an officer in the SOT without updating the doc (or vice-versa), this goes red.
  2. The doc names every RETIRED army display name (so the old->new mapping is actually documented,
     not just the new names), including the ticket's examples (Sentinel, Drillmaster).
  3. The doc states the policy for what intentionally KEEPS the old vocabulary (internal keys + the
     army-themed charter files), so future agents don't "fix" deliberate design.
  4. The living unit memory's log heading uses the canonical "Technical Writer-maintained" (matching
     the code SOT ``orchestrator.memory._LOG_HEADING``), not the retired "Scribe-maintained".

Pure filesystem + string-map checks — no agent, no network. The Agent SDK is stubbed (as every other
harness does) so importing the orchestrator works in a fresh checkout / CI without the SDK installed.
"""
import sys
import types as _types
from pathlib import Path

sys.path.insert(0, ".")

_sdk = _types.ModuleType("claude_agent_sdk")
class _SDKStub:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
_sdk.__getattr__ = lambda n: _SDKStub
sys.modules["claude_agent_sdk"] = _sdk

from orchestrator.officers import OFFICER_NAMES
from orchestrator import memory

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "Documentation" / "OFFICER_NAMING.md"
UNIT = ROOT / "memory" / "UNIT.md"

results: list[tuple[str, bool, str]] = []
def chk(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))

# The retired army display name for each internal key (the EU-17/23 rename, from officers.py's
# docstring). "unchanged" officers had no army->dev rename. The doc must document this whole map.
OLD_DISPLAY = {
    "general": "The General",
    "field_engineer": "Field Engineer",
    "inspector": "Inspector General",
    "adjutant": "Adjutant",
    "provost": "Provost Marshal",
    "quartermaster": "Quartermaster",
    "sentinel": "Sentinel",
    "drillmaster": "Drillmaster",
    "scout": "Scout",
    "scribe": "Scribe",
    "soldiers": "soldiers",
}

doc = DOC.read_text(encoding="utf-8") if DOC.exists() else ""
chk("Documentation/OFFICER_NAMING.md exists (the documented mapping)", DOC.exists())

# --- 1) every officer's NEW canonical name from the SOT appears in the doc -------------------------
for key, new in OFFICER_NAMES.items():
    chk(f"doc documents new canonical name for {key!r}: {new!r}", new in doc, "missing from doc")

# --- 1b) every officer's INTERNAL KEY is shown in the doc (keys are the stable identifiers) --------
for key in OFFICER_NAMES:
    chk(f"doc shows the stable internal key {key!r}", f"`{key}`" in doc, "key not documented")

# --- 2) every RETIRED army display name is documented (old->new, not just new) --------------------
for key, old in OLD_DISPLAY.items():
    chk(f"doc documents the retired name for {key!r}: {old!r}", old in doc, "old name not in doc")
# the ticket called these two out by name explicitly
for example in ("Sentinel", "Drillmaster"):
    chk(f"doc names the ticket's example deprecated name: {example!r}", example in doc)

# --- 3) the policy for intentional-keeps is stated (so deliberate design isn't 'fixed' away) ------
chk("doc states internal keys stay stable", "internal key" in doc.lower())
chk("doc states charter files keep the army metaphor by design",
    "officers/*.md" in doc and "Army role" in doc)

# --- 4) living unit memory log heading is the canonical, code-SOT heading -------------------------
unit = UNIT.read_text(encoding="utf-8") if UNIT.exists() else ""
chk("memory/UNIT.md exists", UNIT.exists())
chk("unit memory log heading is canonical (matches orchestrator.memory._LOG_HEADING)",
    memory._LOG_HEADING in unit, "stale/non-canonical heading")
chk("unit memory log heading no longer says 'Scribe-maintained'",
    "Scribe-maintained" not in unit, "retired heading still present")

# --------------------------------------------------------------------------------------------- #
print("\n=============== EU-57 OFFICER NAMING UNIFIED ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-" * 60)
print(f"  {passed}/{len(results)} passed", "✅" if passed == len(results) else "❌")
sys.exit(0 if passed == len(results) else 1)
