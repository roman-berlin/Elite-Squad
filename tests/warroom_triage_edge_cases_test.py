"""Additional edge case tests for warroom triage detection."""
import sys, types, tempfile, json
from pathlib import Path

sys.path.insert(0, ".")

from orchestrator import warroom
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# ---- Test configuration ----
tmpdir = tempfile.mkdtemp()
audit_path = Path(tmpdir) / "audit.jsonl"

cfg = Config(
    apps=[
        AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                  protected_branch="MAIN", backlog_backend="none",
                  backlog={"project_key": "AUTO"}),
    ],
    audit_path=str(audit_path),
    use_worktree=True,
    worktree_dir=tempfile.mkdtemp()
)

# ---- (a) Empty ticket_id "—" ----
print("\n==== Testing Empty Ticket ID ====")

audit_path.write_text("""{"ts":"2026-06-30T12:00:00Z","event":"prebuild_triage","ticket_id":"AUTO-140","verdict":"CLOSE"}
""")

tasks = [{
    "ticket_id": "—",
    "app": "automatixy",
    "branch": "feat/test",
    "passes": 0,
    "verdict": None,
    "outcome": None,
    "passes_list": [],
    "cost": 0,
    "started": None,
    "ended": None,
}]

result = warroom.active_run(cfg, tasks, "automatixy", active=True)

chk("Empty ticket_id '—' does not crash", result is not None)
chk("No triage state for empty ticket_id", result.get("triage_phase") is None)

# ---- (b) Missing ticket_id (None) ----
print("\n==== Testing Missing Ticket ID ====")

tasks[0]["ticket_id"] = None

result = warroom.active_run(cfg, tasks, "automatixy", active=True)

chk("Missing ticket_id (None) does not crash", result is not None)
chk("No triage state for missing ticket_id", result.get("triage_phase") is None)

# ---- (c) No tasks available ----
print("\n==== Testing No Tasks ====")

result = warroom.active_run(cfg, [], "automatixy", active=True)

chk("No tasks returns None", result is None)

# ---- (d) Triage for different ticket when autopilot is active (EU-130) ----
print("\n==== Testing Triage For Different Ticket (Autopilot Active) ====")

audit_path.write_text("""{"ts":"2026-06-30T12:00:00Z","event":"prebuild_triage","ticket_id":"AUTO-150","verdict":"CLOSE"}
{"ts":"2026-06-30T12:01:00Z","event":"prebuild_close","ticket_id":"AUTO-150","reason":"invalid"}
""")

tasks[0]["ticket_id"] = "AUTO-999"  # Different ticket (old completed run)

result = warroom.active_run(cfg, tasks, "automatixy", active=True)

# EU-130: When autopilot is active, show current triage for the ticket being triaged
# (AUTO-150), NOT the stale completed run (AUTO-999). This prevents showing "Working · Land"
# for a ticket that merged hours ago.
chk("Shows current triage ticket AUTO-150", result.get("ticket") == "AUTO-150")
chk("Shows triage_phase 'closed'", result.get("triage_phase") == "closed")
chk("Shows triage_verdict 'CLOSE'", result.get("triage_verdict") == "CLOSE")
chk("Shows live=True (active triage)", result.get("live") == True)

# ---- (e) Triage for different ticket when NOT active (should not match) ----
print("\n==== Testing Triage For Different Ticket (NOT Active) ====")

result = warroom.active_run(cfg, tasks, "automatixy", active=False)

# When NOT active, the old run (AUTO-999) should be shown, not the triage state
chk("When NOT active, shows original task ticket", result.get("ticket") == "AUTO-999")
chk("When NOT active, no triage_phase", result.get("triage_phase") is None)

# ---- (e) Malformed audit line (should skip gracefully) ----
print("\n==== Testing Malformed Audit Lines ====")

audit_path.write_text("""{"ts":"2026-06-30T12:00:00Z","event":"prebuild_triage","ticket_id":"AUTO-160","verdict":"CLOSE"}
this is not valid json
{"ts":"2026-06-30T12:01:00Z","event":"prebuild_close","ticket_id":"AUTO-160","reason":"invalid"}
""")

tasks[0]["ticket_id"] = "AUTO-160"

result = warroom.active_run(cfg, tasks, "automatixy", active=True)

chk("Malformed audit lines handled gracefully", result is not None)
chk("Still detects triage despite malformed lines", result.get("triage_phase") == "closed")

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
