"""QA for the unified Needs-you inbox aggregator + the cockpit render helpers (hero, side panel)."""
import json, sys, tempfile, types
from datetime import datetime
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))

from orchestrator import needs, warroom, dashboard
from orchestrator.config import Config, AppConfig

d = Path(tempfile.mkdtemp())
(d / "audit.jsonl").write_text("")
app = AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV", protected_branch="MAIN",
                backlog_backend="none")
cfg = Config(apps=[app], audit_path=str(d / "audit.jsonl"), use_worktree=False)

# --- empty state ---
s = needs.summary(cfg)
check("empty -> total 0", s["total"] == 0, str(s))
check("empty side panel says 'All clear'", "All clear" in warroom._needs_side_html(s))

# --- seed all three streams ---
(d / "pending_decisions.json").write_text(json.dumps(
    [{"id": "AUTO-9", "app": "automatixy", "question": "DD/MM or MM/DD?", "summary": "date format"}]))
(d / "drill-report.md").write_text("## Proposal: tighten the Engineer exit gate", encoding="utf-8")
dashboard.load_tasks = lambda p: [{"ticket_id": "AUTO-7", "outcome": "errored", "app": "automatixy",
                                   "note": "build blew up", "started": datetime.now()}]
dashboard.load_dismissed = lambda p: {}

s = needs.summary(cfg)
check("decisions stream picked up", len(s["decisions"]) == 1, str(s["decisions"]))
check("approvals stream picked up", len(s["approvals"]) == 1, str(s["approvals"]))
check("tasks (needs-you) stream picked up", len(s["tasks"]) == 1, str(s["tasks"]))
check("total aggregates all three", s["total"] == 3, str(s["total"]))
check("count() matches total", needs.count(cfg) == 3)

side = warroom._needs_side_html(s)
check("side panel shows the decision", "DD/MM" in side)
check("side panel shows the approval", "Engineering Coach" in side)
check("side panel shows the failed run", "AUTO-7" in side and "errored" in side)
check("side panel links to the inbox", "/needs" in side and "Open inbox" in side)

# --- EU-93: count == len(items) invariant ---
# needs.count() must equal the number of rows the panel will render — the single source of truth.
# All five streams (decisions + approvals + proposals + specialist_approvals + tasks) must sum to
# the same number that the KPI badge and the side-panel header badge both show.
all_items = (s.get("decisions", []) + s.get("approvals", []) + s.get("proposals", [])
             + s.get("specialist_approvals", []) + s.get("tasks", []))
check("EU-93: count() == len(panel items)", needs.count(cfg) == len(all_items),
      f"count={needs.count(cfg)} items={len(all_items)}")

# The 'Needs you' KPI card was deliberately removed from the board in 5a882a6 ("refactor: remove
# Needs you dashboard and UI components", 2026-07-01) — the /needs inbox and the side panel remain
# the surfaces for needs. Pin the removal so the card doesn't half-return without a decision.
from orchestrator import dashboard as _dash
kpi_cards = warroom.kpis(cfg, _dash.load_tasks(cfg.audit_path), None)
needs_card = next((c for c in kpi_cards if c["label"] == "Needs you"), None)
check("KPI 'Needs you' card stays removed (5a882a6); /needs inbox is the surface",
      needs_card is None, str(needs_card))

# --- hero (live run headline) ---
run = {"live": True, "ticket": "AUTO-7", "app": "automatixy", "passes": 2, "cost": 0, "verdict": "",
       "branch": "auto-7", "outcome": "running",
       "phases": ["Build", "Gate", "Review", "Security", "Land"], "reached": 3}
hero = warroom._hero_html(run, "3m 10s", "live")
check("hero shows the ticket", "AUTO-7" in hero)
check("hero shows the current phase (reached=3 -> Security)", "Security" in hero)
check("hero shows elapsed", "3m 10s" in hero)
check("idle run renders no hero", warroom._hero_html({**run, "live": False}, None, None) == "")

print("\n================ NEEDS / COCKPIT QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
