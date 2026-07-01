"""EU-152: KPI hint line removal test.

Verifies that the hint line (third line of KPI cards) has been removed from:
1. Regular KPI cards (with value/label only)
2. KPI cards with gauge bars
3. KPI cards with sparklines
4. Security blocks card (interactive details view)

This is a regression test: the hint field should not appear in the rendered HTML
even if it's present in the card data structure (for backward compatibility).
"""
import sys
import types

# Minimal stubs so warroom can be imported without the full dependency tree.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

sys.path.insert(0, ".")
from orchestrator import warroom

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# ---- Test 1: Regular KPI card (no gauge, no sparkline) ----
print("\n==== Testing Regular KPI Card ====")

regular_card = {
    "label": "Merged total",
    "value": "42",
    "hint": "all time",  # Hint field present but should not render
    "tone": "ok",
}
html = warroom._kpi_html([regular_card])

chk("Regular card: contains value", "42" in html)
chk("Regular card: contains label", "Merged total" in html)
chk("Regular card: does NOT contain hint text", "all time" not in html)
chk("Regular card: does NOT contain hint HTML class", 'class="kh"' not in html and 'class=kh' not in html)

# ---- Test 2: KPI card with gauge bar ----
print("\n==== Testing KPI Card with Gauge ====")

gauge_card = {
    "label": "Tokens",
    "value": "5k · 50%",
    "hint": "cap 10k",  # Hint field present but should not render
    "gauge": 0.5,
    "tone": "ok",
}
html = warroom._kpi_html([gauge_card])

chk("Gauge card: contains value", "5k" in html or "50%" in html)
chk("Gauge card: contains label", "Tokens" in html)
chk("Gauge card: contains gauge bar HTML", "height:3px" in html)
chk("Gauge card: does NOT contain hint text", "cap 10k" not in html)
chk("Gauge card: does NOT contain hint HTML class", 'class="kh"' not in html and 'class=kh' not in html)

# ---- Test 3: KPI card with sparkline ----
print("\n==== Testing KPI Card with Sparkline ====")

sparkline_card = {
    "label": "Pass rate",
    "value": "92%",
    "hint": "last 7 runs",  # Hint field present but should not render
    "sparkline": [1, 2, 1, 0, 1, 0, 1],
    "tone": "ok",
}
html = warroom._kpi_html([sparkline_card])

chk("Sparkline card: contains value", "92%" in html)
chk("Sparkline card: contains label", "Pass rate" in html)
chk("Sparkline card: contains SVG element", "<svg" in html)
chk("Sparkline card: does NOT contain hint text", "last 7 runs" not in html)
chk("Sparkline card: does NOT contain hint HTML class", 'class="kh"' not in html and 'class=kh' not in html)

# ---- Test 4: Security blocks card (interactive) ----
print("\n==== Testing Security Blocks Card ====")

security_card = {
    "label": "Security blocks",
    "value": "2",
    "hint": "Security Engineer gate (all time)",  # Hint should not render in summary
    "tone": "bad",
    "href": "/forensics?cat=security_block",
    "security_block_findings": [
        {"ticket_id": "AUTO-01", "reason": "SQL injection", "iteration": 1},
        {"ticket_id": "AUTO-02", "reason": "Hardcoded secret", "iteration": 2},
    ],
}
html = warroom._kpi_html([security_card])

chk("Security card: renders as <details>", "<details" in html)
chk("Security card: contains value", "2" in html)
chk("Security card: contains label", "Security blocks" in html)
chk("Security card: contains findings", "AUTO-01" in html or "AUTO-02" in html)
chk("Security card: does NOT contain hint text in summary",
    "Security Engineer gate" not in html.split("</summary>")[0])
chk("Security card: does NOT contain hint HTML class", 'class="kh"' not in html and 'class=kh' not in html)

# ---- Test 5: Empty security card ----
print("\n==== Testing Empty Security Card ====")

empty_security_card = {
    "label": "Security blocks",
    "value": "0",
    "hint": "No blocks recorded",  # Hint should not render
    "tone": None,
    "security_block_findings": [],
}
html = warroom._kpi_html([empty_security_card])

chk("Empty security card: renders as <details>", "<details" in html)
chk("Empty security card: contains empty message", "No security blocks" in html)
chk("Empty security card: does NOT contain hint HTML class", 'class="kh"' not in html and 'class=kh' not in html)

# ---- Test 6: Multiple cards mixed ----
print("\n==== Testing Multiple Cards Mixed ====")

mixed_cards = [
    {"label": "Card A", "value": "1", "hint": "hint A"},
    {"label": "Card B", "value": "2", "hint": "hint B", "gauge": 0.3},
    {"label": "Card C", "value": "3", "hint": "hint C", "sparkline": [1, 2, 3]},
]
html = warroom._kpi_html(mixed_cards)

# Count how many hint divs appear
hint_count = html.count('class="kh"') + html.count('class=kh')
chk("Mixed cards: NO hint divs in any card", hint_count == 0, f"Found {hint_count} hint divs")

# ---- Summary ----
print("\n==== EU-152 Test Summary ====")
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
for name, ok, detail in results:
    status = "✓" if ok else "✗"
    print(f"{status} {name}")
    if detail and not ok:
        print(f"   {detail}")

print(f"\nPassed: {passed}/{total}")
sys.exit(0 if passed == total else 1)
