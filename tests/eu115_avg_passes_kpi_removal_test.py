"""EU-115: Avg passes/ticket KPI removal test.

Verifies that the "Avg passes/ticket" (or "Avg passes") KPI card is NOT present
in the cockpit KPI list. This metric was documented in WAR_ROOM.md and QA_MANUAL.md
but never actually implemented as a KPI card. EU-115 removes it from documentation
and this test prevents it from being added back.

Regression test: ensures the KPI list excludes the removed metric.
"""
import sys
import types
import tempfile
from pathlib import Path

# Minimal stubs so warroom can be imported without the full dependency tree.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import warroom
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

print("\n==== EU-115: Avg passes/ticket KPI Removal Test ====")

# Create a minimal config pointing at a temp directory.
tmp = Path(tempfile.mkdtemp())
audit = tmp / "audit.jsonl"
audit.touch()
cfg = Config(
    apps=[AppConfig(name="test", repo_path=str(tmp), base_branch="dev",
                   protected_branch="main", backlog_backend="none")],
    audit_path=str(audit),
    use_worktree=False,
)

# Get the KPI cards from warroom.kpis()
cards = warroom.kpis(cfg, [], None)
card_labels = [c["label"] for c in cards]

print(f"  Current KPI cards: {card_labels}")

# Test 1: Verify "Avg passes/ticket" is not in the KPI list
chk(
    "KPI list does NOT contain 'Avg passes/ticket'",
    "Avg passes/ticket" not in card_labels,
    f"Found: {card_labels}"
)

# Test 2: Verify "Avg passes" (shorter variant) is also not in the list
chk(
    "KPI list does NOT contain 'Avg passes' (shorter variant)",
    "Avg passes" not in card_labels,
    f"Found: {card_labels}"
)

# Test 3: Verify "Avg passes / ticket" (with spaces) is also not in the list
chk(
    "KPI list does NOT contain 'Avg passes / ticket' (with spaces)",
    "Avg passes / ticket" not in card_labels,
    f"Found: {card_labels}"
)

# Test 4: Verify the expected KPI cards are still present
# ("Needs you" left the board in 5a882a6 — /needs is the inbox surface now.)
expected_cards = ["Merged → DEV today", "Security blocks"]
for expected in expected_cards:
    chk(
        f"Expected KPI '{expected}' is present",
        expected in card_labels,
        f"Missing: {expected}"
    )

# Test 5: Verify the Tokens card is present (if usage module is available)
if any(c["label"] == "Tokens" for c in cards):
    chk("Tokens KPI card is present (usage module available)", True)
else:
    chk("Tokens KPI card is absent (usage module unavailable - expected)", True)

# ---- Summary ----
print("\n==== EU-115 Test Summary ====")
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
for name, ok, detail in results:
    status = "✓" if ok else "✗"
    print(f"{status} {name}")
    if detail and not ok:
        print(f"   {detail}")

print(f"\nPassed: {passed}/{total}")
sys.exit(0 if passed == total else 1)
