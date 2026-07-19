"""EU-78: Parked view deduplication + unblock removes from blocked_tickets.json.

Tests:
  1. latest_parked() returns ONE row per ticket (latest run) — same dedup as latest_needs_you.
  2. latest_parked() adds stub rows for ghost tickets (in blocked_set but no audit history).
  3. latest_parked() excludes tickets not in blocked_set.
  4. A ticket whose latest run is 'merged→dev' is NOT shown (it shouldn't be parked).
  5. autopilot.unblock(cfg, tid) removes the ticket from blocked_tickets.json.
  6. autopilot.unblock(cfg) clears ALL entries.
  7. autopilot._auto_clear_merged_ghosts() removes tickets whose latest run already merged.
  8. _auto_clear_merged_ghosts() is a no-op when blocked is empty.
  9. latest_parked() returns rows newest-first (sort order).
 10. POST /api/unblock removes the ticket from blocked_tickets.json (route-level regression).
"""
import sys, types, tempfile, json, unittest.mock
from pathlib import Path

# Minimal stubs so orchestrator imports work without live network deps.
req = types.ModuleType("requests")
req.RequestException = Exception
req.post = req.get = lambda *a, **k: types.SimpleNamespace(status_code=200, json=lambda: {}, text="")
req.Session = lambda: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules.setdefault("requests", req)
# Flask route test needs claude_agent_sdk stub (server.py imports loop which imports it).
sdk = types.ModuleType("claude_agent_sdk")
class _Dummy:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
sdk.__getattr__ = lambda n: _Dummy
sys.modules.setdefault("claude_agent_sdk", sdk)
sys.path.insert(0, ".")

from orchestrator import dashboard as D, autopilot as AP
from orchestrator.config import Config
from orchestrator.audit import AuditLog

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


# ── helpers ────────────────────────────────────────────────────────────────────

def task(tid: str, started: str, outcome: str = "escalated") -> dict:
    return {"ticket_id": tid, "started": started, "outcome": outcome,
            "ended": None, "passes": 0, "turns": 0, "cost": 0.0,
            "verdict": None, "note": "", "branch": "", "app": "",
            "passes_list": [], "duration": None, "dry_run": None, "detail": {}, "pr_url": None}


# ── 1. One row per ticket — latest run wins ────────────────────────────────────
blocked = {"AUTO-32", "AUTO-14"}
many = [
    task("AUTO-14", "2026-06-19T19:58:03", "errored"),
    task("AUTO-14", "2026-06-21T01:03:06", "errored"),      # newer — should win
    task("AUTO-32", "2026-06-24T03:29:37", "awaiting decision"),
    task("AUTO-9",  "2026-06-23T09:00:00", "merged→dev"),   # NOT in blocked_set
]
parked = D.latest_parked(many, blocked)
ids = {t["ticket_id"] for t in parked}

chk("one row per parked ticket (two unique tickets → two rows)", ids == {"AUTO-14", "AUTO-32"}, ids)
chk("ticket NOT in blocked_set is excluded", "AUTO-9" not in ids)
chk("latest run is kept for AUTO-14",
    next(t for t in parked if t["ticket_id"] == "AUTO-14")["started"] == "2026-06-21T01:03:06")

# ── 2. Ghost tickets (in blocked but no audit history) get stub rows ───────────
parked2 = D.latest_parked([], {"GHOST-1", "GHOST-2"})
ghost_ids = {t["ticket_id"] for t in parked2}
chk("ghost ticket with no history gets a stub row", ghost_ids == {"GHOST-1", "GHOST-2"}, ghost_ids)
chk("stub row has outcome=None", all(t["outcome"] is None for t in parked2))

# ── 3. latest_parked() with empty blocked_set returns nothing ──────────────────
parked3 = D.latest_parked(many, set())
chk("empty blocked_set → empty result", parked3 == [], parked3)

# ── 4. A ticket whose LATEST run merged is NOT shown as parked ─────────────────
# Scenario: ticket was escalated (run A), then unblocked & ran again and merged (run B).
# If auto-clear somehow missed it and it's still in blocked, latest_parked shows run B
# (merged). The parked view still shows it (removal is autopilot's job), but the
# correct row is the merged one, not the old escalated one.
mixed = [
    task("AUTO-5", "2026-06-20T10:00:00", "escalated"),     # old run
    task("AUTO-5", "2026-06-22T12:00:00", "merged→dev"),    # latest run — succeeded
]
parked4 = D.latest_parked(mixed, {"AUTO-5"})
chk("latest_parked keeps the newest run even if it merged (auto-clear hasn't run yet)",
    len(parked4) == 1 and parked4[0]["outcome"] == "merged→dev")

# ── 5–6. unblock() removes ticket / clears all from blocked_tickets.json ────────
tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[], audit_path=str(tmp / "audit.jsonl"))
blocked_file = tmp / "blocked_tickets.json"
blocked_file.write_text(json.dumps(["AUTO-32", "AUTO-33", "EU-17"]))

AP.unblock(cfg, "AUTO-32")
remaining = json.loads(blocked_file.read_text())
chk("unblock(tid) removes it from blocked_tickets.json", "AUTO-32" not in remaining, remaining)
chk("other tickets are untouched after single unblock",
    set(remaining) == {"AUTO-33", "EU-17"}, remaining)

AP.unblock(cfg, "MISSING-99")   # not in file — must not raise
chk("unblock of absent ticket is a no-op (state unchanged, no crash)",
    set(json.loads(blocked_file.read_text())) == {"AUTO-33", "EU-17"})

AP.unblock(cfg)                 # clear all
remaining_all = json.loads(blocked_file.read_text())
chk("unblock() with no arg clears all entries", remaining_all == [], remaining_all)

# ── 7. _auto_clear_merged_ghosts() removes tickets whose latest run already merged ──
tmp2 = Path(tempfile.mkdtemp())
cfg2 = Config(apps=[], audit_path=str(tmp2 / "audit.jsonl"))
blocked_file2 = tmp2 / "blocked_tickets.json"
blocked_file2.write_text(json.dumps(["AUTO-32", "AUTO-33"]))
audit2 = AuditLog(str(tmp2 / "audit.jsonl"))

# Patch load_tasks to return a fake history where AUTO-32's latest run merged.
fake_tasks = [
    task("AUTO-32", "2026-06-25T10:00:00", "merged→dev"),   # latest = merged → should be cleared
    task("AUTO-33", "2026-06-25T09:00:00", "errored"),       # still needs attention → keep
]
with unittest.mock.patch.object(D, "load_tasks", return_value=fake_tasks):
    blocked_in = {"AUTO-32", "AUTO-33"}
    blocked_out = AP._auto_clear_merged_ghosts(cfg2, blocked_in, audit2)

remaining_ghosts = json.loads(blocked_file2.read_text())
chk("_auto_clear_merged_ghosts removes merged ticket from blocked_tickets.json",
    "AUTO-32" not in remaining_ghosts, remaining_ghosts)
chk("non-merged ticket stays in blocked_tickets.json",
    "AUTO-33" in remaining_ghosts, remaining_ghosts)
chk("_auto_clear_merged_ghosts returns updated in-memory set",
    "AUTO-32" not in blocked_out and "AUTO-33" in blocked_out, blocked_out)

# ── 8. _auto_clear_merged_ghosts() is a no-op when blocked is empty ───────────
with unittest.mock.patch.object(D, "load_tasks", return_value=fake_tasks):
    result_empty = AP._auto_clear_merged_ghosts(cfg2, set(), audit2)
chk("_auto_clear_merged_ghosts no-ops on empty blocked set", result_empty == set(), result_empty)

# ── 9. latest_parked() returns rows newest-first ──────────────────────────────
# Three distinct blocked tickets; the one with the most-recent started should come first.
sort_tasks = [
    task("AUTO-10", "2026-06-20T08:00:00", "errored"),
    task("AUTO-11", "2026-06-22T10:00:00", "escalated"),   # newest → should be first
    task("AUTO-12", "2026-06-21T09:00:00", "errored"),
]
parked_sorted = D.latest_parked(sort_tasks, {"AUTO-10", "AUTO-11", "AUTO-12"})
order = [t["ticket_id"] for t in parked_sorted]
chk("latest_parked returns rows newest-first (most recent started at index 0)",
    order[0] == "AUTO-11", order)

# ── 10. POST /api/unblock removes ticket from blocked_tickets.json (route-level) ──
# This pins the Commander's comment: the Unblock button must call autopilot.unblock()
# (removing from blocked_tickets.json), NOT dashboard.dismiss() (which writes dismissed.json).
tmp3 = Path(tempfile.mkdtemp())
cfg3_path = str(tmp3 / "audit.jsonl")
(tmp3 / "audit.jsonl").write_text("")
blocked_file3 = tmp3 / "blocked_tickets.json"
blocked_file3.write_text(json.dumps(["AUTO-55", "AUTO-66"]))

from orchestrator.config import Config as _Config, AppConfig as _AppConfig
from orchestrator import server as _srv, sync as _sync
cfg3 = _Config(
    apps=[_AppConfig(name="automatixy", repo_path=str(tmp3),
                     base_branch="DEV", protected_branch="MAIN",
                     backlog_backend="none")],
    audit_path=cfg3_path, use_worktree=False,
)
cfg3.detected_auth = lambda: "test"
_sync.can_promote = lambda: False

client3 = _srv.create_app(cfg3).test_client()
resp = client3.post("/api/unblock", data={"ticket": "AUTO-55"})
# Route redirects to /tasks?filter=parked on success.
chk("POST /api/unblock redirects (302)", resp.status_code == 302, resp.status_code)
remaining3 = json.loads(blocked_file3.read_text())
chk("POST /api/unblock removes ticket from blocked_tickets.json",
    "AUTO-55" not in remaining3, remaining3)
chk("POST /api/unblock leaves other parked tickets intact",
    "AUTO-66" in remaining3, remaining3)

# Regression: /api/unblock must NOT write dismissed.json (that's dashboard.dismiss's job).
dismissed_file3 = tmp3 / "dismissed.json"
chk("POST /api/unblock does NOT write dismissed.json (wrong store)",
    not dismissed_file3.exists(), dismissed_file3)

# ── Report ─────────────────────────────────────────────────────────────────────
print("\n============ PARKED VIEW QA (EU-78) ============")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
