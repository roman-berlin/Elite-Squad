"""Test warroom.py active_run() prebuild triage detection (EU-130).

Verifies that the Active-run card shows live triage state (ANSWER/CLOSE/REFILE)
when a run is in prebuild triage, rather than showing stale data from a previous
completed pipeline.
"""
import sys, types, tempfile, json
from pathlib import Path
from unittest.mock import patch

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

# ---- (a) Prebuild CLOSE detection ----
print("\n==== Testing Prebuild CLOSE Detection ====")

audit_path.write_text("""{"ts":"2026-06-30T12:00:00Z","event":"prebuild_triage","ticket_id":"AUTO-130","verdict":"CLOSE","citations":[{"source":"JIRA","claim":"duplicate of AUTO-100"}]}
{"ts":"2026-06-30T12:01:00Z","event":"prebuild_close","ticket_id":"AUTO-130","reason":"invalid"}
""")

tasks = [{
    "ticket_id": "AUTO-130",
    "app": "automatixy",
    "branch": "feat/test",
    "passes": 0,
    "verdict": None,
    "outcome": None,  # no terminal outcome - still in flight
    "passes_list": [],  # no build events yet
    "cost": 0,
    "started": None,
    "ended": None,
}]

result = warroom.active_run(cfg, tasks, "automatixy", active=True)

chk("CLOSE triage detected when no build events", result is not None)
chk("triage_phase is 'closed'", result.get("triage_phase") == "closed")
chk("triage_verdict is CLOSE", result.get("triage_verdict") == "CLOSE")
chk("triage_reason is present", result.get("triage_reason") == "invalid")

# ---- (b) Prebuild ANSWER detection ----
print("\n==== Testing Prebuild ANSWER Detection ====")

audit_path.write_text("""{"ts":"2026-06-30T12:00:00Z","event":"prebuild_triage","ticket_id":"AUTO-131","verdict":"ANSWER","citations":[{"source":"CLAUDE.md","claim":"tenant isolation rules"}]}
{"ts":"2026-06-30T12:01:00Z","event":"prebuild_close","ticket_id":"AUTO-131","reason":"answered"}
""")

tasks[0]["ticket_id"] = "AUTO-131"

result = warroom.active_run(cfg, tasks, "automatixy", active=True)

chk("ANSWER triage detected", result.get("triage_phase") == "closed")
chk("triage_verdict is ANSWER", result.get("triage_verdict") == "ANSWER")
chk("triage_reason is 'answered'", result.get("triage_reason") == "answered")

# ---- (c) Prebuild REFILE detection ----
print("\n==== Testing Prebuild REFILE Detection ====")

audit_path.write_text("""{"ts":"2026-06-30T12:00:00Z","event":"prebuild_triage","ticket_id":"AUTO-132","verdict":"REFILE","citations":[{"source":"JIRA","claim":"missing acceptance criteria"}]}
{"ts":"2026-06-30T12:01:00Z","event":"prebuild_refile","ticket_id":"AUTO-132","new_ticket_key":"AUTO-133","title":"Fix auth bug with clear criteria"}
{"ts":"2026-06-30T12:02:00Z","event":"prebuild_refile","ticket_id":"AUTO-132","new_ticket_key":"AUTO-134","title":"Add unit tests for auth"}
""")

tasks[0]["ticket_id"] = "AUTO-132"

result = warroom.active_run(cfg, tasks, "automatixy", active=True)

chk("REFILE triage detected", result.get("triage_phase") == "refiled")
chk("triage_verdict is REFILE", result.get("triage_verdict") == "REFILE")
chk("new_tickets array has 2 entries", len(result.get("triage_new_tickets", [])) == 2)
chk("first new ticket key is AUTO-133", result.get("triage_new_tickets", [{}])[0].get("key") == "AUTO-133")

# ---- (d) In-triage (not yet resolved) ----
print("\n==== Testing In-Triage (Still Triaging) ====")

audit_path.write_text("""{"ts":"2026-06-30T12:00:00Z","event":"prebuild_triage","ticket_id":"AUTO-135","verdict":"ANSWER","citations":[{"source":"CLAUDE.md","claim":"config location"}]}
""")

tasks[0]["ticket_id"] = "AUTO-135"

result = warroom.active_run(cfg, tasks, "automatixy", active=True)

chk("In-triage state detected", result.get("triage_phase") == "triaging")
chk("triage_verdict is ANSWER", result.get("triage_verdict") == "ANSWER")
chk("no triage_reason (not resolved)", "triage_reason" not in result)

# ---- (e) Build events override triage ----
print("\n==== Testing Build Events Override Triage ====")

audit_path.write_text("""{"ts":"2026-06-30T12:00:00Z","event":"prebuild_triage","ticket_id":"AUTO-136","verdict":"CLOSE"}
{"ts":"2026-06-30T12:01:00Z","event":"build","ticket_id":"AUTO-136","build_summary":"Built 2 files"}
""")

tasks[0]["ticket_id"] = "AUTO-136"
tasks[0]["passes_list"] = [{"build_summary": "Built files"}]  # has build

result = warroom.active_run(cfg, tasks, "automatixy", active=True)

chk("Build events suppress triage detection", result.get("triage_phase") is None)
chk("Normal phase data present", "phases" in result and "reached" in result)

# ---- (f) No triage events -> no triage state ----
print("\n==== Testing No Triage Events ====")

audit_path.write_text("""{"ts":"2026-06-30T12:00:00Z","event":"other","ticket_id":"AUTO-137"}
""")

tasks[0]["ticket_id"] = "AUTO-137"
tasks[0]["passes_list"] = []

result = warroom.active_run(cfg, tasks, "automatixy", active=True)

chk("No triage events -> no triage fields", result.get("triage_phase") is None)

# ---- (f) EU-130 bug scenario: autopilot triaging new tickets while old run shows as stale ----
print("\n==== Testing EU-130 Bug Scenario (Autopilot Triage vs Stale Run) ====")

# Simulate the bug scenario: EU-109 merged at 16:16, then at 18:22 autopilot
# starts triaging EU-113 and EU-114. The cockpit should show the CURRENT
# triage activity (EU-113/EU-114), NOT the stale "Working · Land" for EU-109.
audit_path.write_text("""{"ts":"2026-06-30T16:16:00Z","event":"merged","ticket_id":"AUTO-109","outcome":"merged→dev"}
{"ts":"2026-06-30T18:22:21Z","event":"prebuild_triage","ticket_id":"AUTO-113","verdict":"REFILE"}
{"ts":"2026-06-30T18:22:24Z","event":"prebuild_refile","ticket_id":"AUTO-113","new_ticket_key":"AUTO-115","title":"Split out auth fix"}
{"ts":"2026-06-30T18:25:27Z","event":"prebuild_triage","ticket_id":"AUTO-114","verdict":"CLOSE"}
{"ts":"2026-06-30T18:25:29Z","event":"prebuild_close","ticket_id":"AUTO-114","reason":"invalid"}
""")

# The task list shows the old completed run (AUTO-109)
tasks[0]["ticket_id"] = "AUTO-109"
tasks[0]["outcome"] = "merged→dev"
tasks[0]["passes_list"] = [{"build_summary": "Built"}]  # has build events

result = warroom.active_run(cfg, tasks, "automatixy", active=True)

# Should show the CURRENT triage (AUTO-114, the most recent), not the stale AUTO-109
chk("Shows current triage ticket AUTO-114", result.get("ticket") == "AUTO-114")
chk("Shows triage_phase 'closed'", result.get("triage_phase") == "closed")
chk("Shows triage_verdict 'CLOSE'", result.get("triage_verdict") == "CLOSE")
chk("No stale 'Working · Land' for AUTO-109", "AUTO-109" not in str(result.get("ticket")))
chk("live=True (active autopilot run)", result.get("live") == True)

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
