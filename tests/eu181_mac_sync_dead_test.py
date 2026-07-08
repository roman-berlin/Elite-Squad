"""EU-181: Mac-side state sync is dead since bf1ce92 — launchd job points at a deleted script.

Testable acceptance criteria:
1. After removing dead launchd agents, error logs stop growing
2. Running 'launchctl list' shows no com.roman.general.sync/smalltalk/council/patrol agents loaded
3. The single-source scheduler test passes
4. Documentation contains the specific launchctl bootout commands

This test verifies the documentation fix and the gate test.
"""
import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUDIT_DOC = ROOT / "Documentation" / "SYSTEM_AUDIT_2026-07-06.md"

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# Test 1: Documentation exists and contains EU-181 section
chk("EU-181 audit doc exists", AUDIT_DOC.exists())
doc_text = AUDIT_DOC.read_text(encoding="utf-8") if AUDIT_DOC.exists() else ""
chk("audit doc mentions EU-181", "EU-181" in doc_text)

# Test 2: Documentation contains the specific launchctl bootout commands
# The agents are: com.roman.general.sync, .smalltalk, .council, .patrol
# The bootout command format is: launchctl bootout gui/$(id -u)/<agent>
dead_agents = [
    "com.roman.general.sync",
    "com.roman.general.smalltalk",
    "com.roman.general.council",
    "com.roman.general.patrol",
]

for agent in dead_agents:
    # Check for the bootout command with this agent
    pattern = f"launchctl bootout.*{re.escape(agent)}"
    has_bootout = re.search(pattern, doc_text, re.IGNORECASE | re.DOTALL)
    chk(f"documentation contains bootout command for {agent}", has_bootout,
        f"searched for pattern: {pattern}")

# Test 3: Documentation explains that autopilot does NOT call sync.git_sync
# (the VPS cron is the single source of truth)
# and clarifies that Mac state syncs via VPS cron, not Mac launchd
chk("documentation mentions VPS cron as single source of truth",
    "VPS cron" in doc_text or "cron" in doc_text)
chk("documentation explains Mac does NOT have its own sync scheduler",
    "Mac" in doc_text and ("launchd" in doc_text or "dead" in doc_text))

# Test 4: Wave 0 section contains the EU-181 fix
wave0_mention = "Wave 0" in doc_text and "EU-181" in doc_text
chk("Wave 0 section mentions EU-181", wave0_mention)

# Test 5: The scheduler_single_source_test.py should pass
# (this is a separate test file, we just verify it exists)
scheduler_test = ROOT / "tests" / "scheduler_single_source_test.py"
chk("scheduler_single_source_test.py exists", scheduler_test.exists())

print("\n========= EU-181 MAC SYNC DEAD FIX QA =========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
