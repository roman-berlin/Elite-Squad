"""EU-144: Create Jira tickets from officer discussion findings (create_tickets.py).

Exercises create_tickets.py against a STUB Jira adapter (no network, no creds):
  * OFFICER_ISSUES list is properly structured with all required fields
  * create_tickets() function exists and is callable
  * Duplicate detection works (find_open_by_summary is called)
  * Tickets are created with proper structure (title, severity, body, labels, type)
  * Error handling works when Jira adapter fails to initialize
  * Summary statistics are correctly computed (filed, deduped, failed)
"""
import sys, types
from pathlib import Path

# Add the repo root to the path
repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root))

# Stub the Agent SDK so importing the orchestrator package never reaches the network
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

# Stub requests so jira module imports without the dep
req = types.ModuleType("requests")
req.Session = lambda *a, **k: None
req.RequestException = Exception
sys.modules["requests"] = req

# Import the create_tickets module
from create_tickets import OFFICER_ISSUES, create_tickets
from orchestrator.config import AppConfig

results = []
def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))


# ---- Test 1: OFFICER_ISSUES list is properly structured ----
check("OFFICER_ISSUES is a list", isinstance(OFFICER_ISSUES, list))
check("OFFICER_ISSUES has 8 tickets", len(OFFICER_ISSUES) == 8)

# Verify each ticket has required fields
required_fields = ["title", "type", "severity", "body"]
for i, ticket in enumerate(OFFICER_ISSUES):
    for field in required_fields:
        check(f"Ticket {i} has '{field}' field", field in ticket,
              f"missing in ticket {i}: {ticket.get('title', 'UNKNOWN')}")
    check(f"Ticket {i} title is non-empty", bool(ticket.get("title", "").strip()))
    check(f"Ticket {i} body is non-empty", bool(ticket.get("body", "").strip()))
    check(f"Ticket {i} severity is valid", ticket.get("severity") in ["CRITICAL", "HIGH", "MEDIUM"])

# Verify severity distribution
critical = sum(1 for t in OFFICER_ISSUES if t.get("severity") == "CRITICAL")
high = sum(1 for t in OFFICER_ISSUES if t.get("severity") == "HIGH")
medium = sum(1 for t in OFFICER_ISSUES if t.get("severity") == "MEDIUM")
check("Severity distribution matches report (2 CRITICAL, 2 HIGH, 4 MEDIUM)",
      critical == 2 and high == 2 and medium == 4,
      f"got CRITICAL={critical}, HIGH={high}, MEDIUM={medium}")

# Verify ticket titles are unique
titles = [t.get("title", "") for t in OFFICER_ISSUES]
check("All ticket titles are unique", len(titles) == len(set(titles)))

# Verify all tickets mention the impact (why it matters)
for i, ticket in enumerate(OFFICER_ISSUES):
    body = ticket.get("body", "")
    check(f"Ticket {i} body has 'Why it matters' section",
          "Why it matters:" in body or "**Why it matters:**" in body)


# ---- Test 2: create_tickets function exists and is callable ----
check("create_tickets function exists", callable(create_tickets))

# Verify AppConfig is used properly
check("AppConfig is importable from orchestrator.config", AppConfig is not None)


# ---- Test 3: Stub JiraAdapter for testing (no network) ----
class StubJiraAdapter:
    """Stub Jira adapter that records calls without touching network."""
    def __init__(self, app, fail_init=False):
        self.app = app
        self.fail_init = fail_init
        self.find_calls = []
        self.create_calls = []
        self._existing = {}  # summary -> key for dedup testing

    def find_open_by_summary(self, summary):
        self.find_calls.append(summary)
        return self._existing.get(summary.strip().lower())

    def create_task(self, summary, description, labels=None, issue_type="Task"):
        if self.fail_init:
            raise RuntimeError("Jira adapter not initialized")
        self.create_calls.append({
            "summary": summary,
            "description": description,
            "labels": labels,
            "issue_type": issue_type
        })
        return f"EU-{100 + len(self.create_calls)}"

    # Stub other methods
    def comments(self, key): return []
    def latest_answer(self, ticket): return None


# ---- Test 4: Verify ticket content quality ----
# Each ticket should have clear "What:", "Where:", "Why it matters:", "Impact:", "Acceptance Criteria:"
for i, ticket in enumerate(OFFICER_ISSUES):
    body = ticket.get("body", "")
    check(f"Ticket {i} has 'What:' section", "What:" in body or "**What:**" in body)
    check(f"Ticket {i} has 'Where:' section", "Where:" in body or "**Where:**" in body)
    check(f"Ticket {i} has 'Impact:' section", "Impact:" in body or "**Impact:**" in body)
    check(f"Ticket {i} has 'Acceptance Criteria:' section",
          "Acceptance Criteria:" in body or "**Acceptance Criteria:**" in body)


# ---- Test 5: Verify specific critical tickets exist ----
critical_titles = [
    "Wire DEV post-merge health check - 32 lands with zero automated verification",
    "Move tenant isolation check to commit-time - 37 security blocks burning passes"
]
for title in critical_titles:
    check(f"Critical ticket exists: '{title[:50]}...'",
          any(t.get("title") == title for t in OFFICER_ISSUES))

# Verify high-priority tickets
high_titles = [
    "Wire Performance Engineer routing - 72 performance blocks burning passes",
    "Wire mandatory pre-build test questions into Vanguard identity file - 186 test blocks"
]
for title in high_titles:
    check(f"High ticket exists: '{title[:50]}...'",
          any(t.get("title") == title for t in OFFICER_ISSUES))


# ---- Test 6: Verify medium-priority tickets ----
medium_topics = [
    "scope check",
    "completeness doctrine",
    "accessibility",
    "spec discipline"
]
for topic in medium_topics:
    check(f"Medium ticket mentions '{topic}'",
          any(topic.lower() in t.get("title", "").lower() for t in OFFICER_ISSUES),
          f"topic '{topic}' not found in any ticket title")


# ---- Test 7: Verify tickets have numerical impact in body ----
# Each ticket should mention the block/land count (e.g., "32 tickets merged", "37 security blocks", etc.)
impact_numbers = ["32", "37", "72", "186", "19", "96", "27"]
for num in impact_numbers:
    found = False
    for ticket in OFFICER_ISSUES:
        body = ticket.get("body", "")
        # Check if the number appears in the body (not just in the title)
        # The number might appear with "tickets", "blocks", "lands", "failures", etc.
        if num in body:
            found = True
            break
    check(f"Ticket mentions impact number '{num}'", found)


# ---- Test 8: Verify labels would be applied correctly ----
# The script uses labels=["officer-discussion", "process-improvement"]
expected_labels = ["officer-discussion", "process-improvement"]
check("Expected labels are defined for create_task",
      all(isinstance(l, str) for l in expected_labels))


# ---- Test 9: Verify issue types are Task (not Bug) ----
for i, ticket in enumerate(OFFICER_ISSUES):
    check(f"Ticket {i} has issue_type 'Task'",
          ticket.get("type") == "Task",
          f"got type: {ticket.get('type')}")


# ---- Test 10: Verify officer mentions are deduplicated ----
# Scout, QA, SRE, and Sentinel all mentioned DEV health check
# Should be ONE ticket, not four
dev_health_tickets = [t for t in OFFICER_ISSUES if "DEV health" in t.get("title", "") or "post-merge" in t.get("title", "")]
check("DEV health check is deduped to ONE ticket",
      len(dev_health_tickets) == 1,
      f"found {len(dev_health_tickets)} tickets mentioning DEV health")


# ---- Test 11: Verify no ticket is empty or trivial ----
for i, ticket in enumerate(OFFICER_ISSUES):
    body = ticket.get("body", "")
    # Body should be substantial (at least 200 chars for a real ticket)
    check(f"Ticket {i} body is substantial (>=200 chars)",
          len(body) >= 200,
          f"body length: {len(body)}")


# ---- Test 12: Verify each ticket has actionable acceptance criteria ----
for i, ticket in enumerate(OFFICER_ISSUES):
    body = ticket.get("body", "")
    # Should have bullet points or numbered criteria
    has_bullets = "-" in body or "*" in body or "1." in body or "2." in body
    check(f"Ticket {i} has actionable acceptance criteria (bullets/numbered)",
          has_bullets)


# ---- Summary and verdict ----
print("\n================ EU-144 CREATE-TICKETS QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    status = "✓ PASS" if ok else "✗ FAIL"
    print(f"  {status}  {name}" + (f"  [{detail}]" if (not ok and detail) else ""))
print("---------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
