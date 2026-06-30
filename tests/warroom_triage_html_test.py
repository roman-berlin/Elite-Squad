"""Test warroom.py _run_html() triage state rendering (EU-130).

Verifies that _run_html() renders the triage phase UI correctly when triage_phase
is set, and that the card never shows stale 'Working · Land' text for tickets that
are in triage or already merged.
"""
import sys
import types

sys.path.insert(0, ".")

# Minimal stubs so warroom can be imported without the full dependency tree.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

from orchestrator import warroom

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# ---- Test data ----
phases = list(warroom.PHASES)

def make_run_with_triage(triage_phase: str, triage_verdict: str,
                          triage_reason: str = None,
                          triage_new_tickets: list = None,
                          live: bool = True) -> dict:
    """Helper to create a run dict with triage state."""
    run = {
        "live": live,
        "ticket": "AUTO-130",
        "app": "automatixy",
        "passes": 0,
        "verdict": "",
        "outcome": None,  # no terminal outcome - in triage
        "branch": "feat/test",
        "phases": phases,
        "reached": 0,
        "failed_phase": None,
        "sparkline": [],
        "triage_phase": triage_phase,
        "triage_verdict": triage_verdict,
    }
    if triage_reason:
        run["triage_reason"] = triage_reason
    if triage_new_tickets:
        run["triage_new_tickets"] = triage_new_tickets
    return run

# ---- (a) CLOSED triage rendering ----
print("\n==== Testing CLOSED Triage Rendering ====")

run = make_run_with_triage("closed", "CLOSE", "invalid")
html = warroom._run_html(run, mode="live", elapsed="2m 15s", manual=False)

chk("Triage HTML contains triage bar", "triage-bar" in html)
chk("Shows 'Closed' status", "Closed" in html)
chk("Shows close reason", "invalid" in html)
chk("Shows verdict CLOSE", "CLOSE" in html)
chk("No 'Working · Land' appears", "Working · Land" not in html)
chk("No phase bar (triage replaces it)", '<div class="phasebar idle">' not in html)
chk("No pass metadata shown", '<span class=meta>pass' not in html)

# ---- (b) ANSWER triage rendering ----
print("\n==== Testing ANSWER Triage Rendering ====")

run = make_run_with_triage("closed", "ANSWER", "answered")
html = warroom._run_html(run, mode="live", elapsed="1m 30s", manual=False)

chk("Triage HTML shows 'Closed' for ANSWER", "Closed" in html)
chk("Shows 'answered' reason", "answered" in html)
chk("Verdict ANSWER appears", "ANSWER" in html)
chk("No stale pipeline info", "Working · Land" not in html)

# ---- (c) REFILE triage rendering ----
print("\n==== Testing REFILE Triage Rendering ====")

run = make_run_with_triage(
    "refiled", "REFILE",
    triage_new_tickets=[
        {"key": "AUTO-133", "title": "Fix auth bug"},
        {"key": "AUTO-134", "title": "Add tests"},
    ]
)
html = warroom._run_html(run, mode="live", elapsed="45s", manual=False)

chk("Triage HTML shows 'Refiled'", "Refiled" in html)
chk("Shows new ticket AUTO-133", "AUTO-133" in html)
chk("Shows new ticket AUTO-134", "AUTO-134" in html)
chk("No 'Working · Land' text", "Working · Land" not in html)

# ---- (d) In-triage (still triaging) ----
print("\n==== Testing In-Triage (Still Triaging) ====")

run = make_run_with_triage("triaging", "ANSWER")
html = warroom._run_html(run, mode="live", elapsed="30s", manual=False)

chk("Triage HTML shows 'Triaging'", "Triaging" in html)
chk("Shows verdict ANSWER", "ANSWER" in html)
chk("Shows 'verdict:' label", "verdict:" in html)
chk("No stale pipeline phases", "Working · Land" not in html)

# ---- (e) No triage phase (normal run) ----
print("\n==== Testing Normal Run (No Triage) ====")

normal_run = {
    "live": True,
    "ticket": "AUTO-140",
    "app": "automatixy",
    "passes": 1,
    "verdict": "PASS",
    "outcome": None,
    "branch": "feat/normal",
    "phases": phases,
    "reached": 2,
    "failed_phase": None,
    "sparkline": [1, 2, 3],
}
html = warroom._run_html(normal_run, mode="live", elapsed="5m 20s", manual=False)

chk("Normal run shows phase bar", "phasebar" in html and "triage-bar" not in html)
chk("Shows 'Working' with phase name", "Working" in html)
chk("Shows pass metadata", '<span class=meta>pass' in html)
chk("No triage-specific classes", "triage-meta" not in html)
chk("Shows normal phase bar structure", "ph.now" in html or "ph.done" in html)

# ---- (f) Already-merged ticket with triage state (edge case) ----
print("\n==== Testing Already-Merged Ticket Never Shows 'Working · Land' ====")

merged_with_triage = make_run_with_triage("closed", "CLOSE", "duplicate")
merged_with_triage["live"] = False
merged_with_triage["outcome"] = "merged→dev"
html = warroom._run_html(merged_with_triage, mode=None, elapsed=None, manual=False)

chk("Merged ticket with triage shows triage state", "Closed" in html)
chk("No 'Working · Land' appears", "Working · Land" not in html)
chk("Shows 'last run' badge", "last run" in html)

# ---- Summary ----
print("\n==== Test Summary ====")
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
for name, ok, detail in results:
    status = "✓" if ok else "✗"
    print(f"{status} {name}")
    if detail and not ok:
        print(f"   {detail}")

print(f"\nPassed: {passed}/{total}")
sys.exit(0 if passed == total else 1)
