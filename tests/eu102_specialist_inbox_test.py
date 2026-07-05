"""EU-102 (iter-3) — the specialist-only inbox is NOT a dead-end, and the badge is one number.

The iter-2 rejection flagged two UI gaps left by the two-tier design:
  • render_board()'s side-panel badge read summary()['total'] while the KPI card read needs.count()
    = len(rows) — so with a pending specialist roster the two disagreed (count != list on the board);
  • /needs was gated on summary()['total'] but never RENDERED specialist_approvals, so a specialist-
    only state showed a non-zero badge pointing at an empty inbox.

Both are now fixed by folding specialist rosters into ``rows`` (count == len(rows) == total) AND
rendering a "Specialist rosters" section on /needs.  This harness asserts, end to end:

  1. Specialist-only state — count()/total/len(rows) all agree at 1; the row is category 'specialist'.
  2. The cockpit board badge ("Needs you · N") equals needs.count(); the side panel shows the row
     (not "All clear"); the KPI "Needs you" card value equals the same N — ONE number everywhere.
  3. /needs renders an actionable "Specialist rosters" section (approve/decline + roster), never the
     "All clear" dead-end.
  4. Mixed state (decision + errored + specialist) keeps count() == board badge == KPI value.

All disk I/O is fixture/tempdir only — no network, no real models.
"""
import json
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path

# ── stub the Agent SDK so orchestrator imports cleanly ─────────────────────
_sdk = types.ModuleType("claude_agent_sdk")


class _Stub:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self


_sdk.__getattr__ = lambda _n: _Stub
sys.modules["claude_agent_sdk"] = _sdk
sys.path.insert(0, ".")

from orchestrator import needs, warroom, dashboard as D, server
from orchestrator.config import Config, AppConfig

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail) if not cond else ""))


def _make_cfg(tmp: Path) -> Config:
    (tmp / "audit.jsonl").write_text("")
    app = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")
    cfg = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
    cfg.detected_auth = lambda: "test"   # let server.create_app build a client
    return cfg


def _seed_specialist(tmp: Path) -> None:
    (tmp / "pending_specialist_approvals.json").write_text(json.dumps({
        "AUTO-90::mql5": {
            "status": "pending", "ticket_id": "AUTO-90", "domain": "mql5",
            "ticket_summary": "Build MQL5 EA", "app_name": "automatixy",
            "charters": [{"lane_key": "mql5-algo", "name": "MQL5 Algo Specialist"}],
        }
    }), encoding="utf-8")


def _needs_card_value(cfg) -> int | None:
    """The value the KPI 'Needs you' card shows — the number the cockpit headline prints."""
    tasks = D.load_tasks(cfg.audit_path)
    for c in warroom.kpis(cfg, tasks, None):
        if c.get("label") == "Needs you":
            return c.get("value")
    return None


# ───────────────────────────────────────────────────────────────────────────
# 1. Specialist-only state — the data layer agrees at 1
# ───────────────────────────────────────────────────────────────────────────
tmp1 = Path(tempfile.mkdtemp())
cfg1 = _make_cfg(tmp1)
_seed_specialist(tmp1)
D.load_tasks = lambda _p: []
D.load_dismissed = lambda _p: {}

s1 = needs.summary(cfg1)
chk("1a. specialist-only: count() == 1", needs.count(cfg1) == 1, f"count={needs.count(cfg1)}")
chk("1b. specialist-only: total == len(rows) == count", s1["total"] == len(s1["rows"]) == needs.count(cfg1),
    f"total={s1['total']} rows={len(s1['rows'])} count={needs.count(cfg1)}")
chk("1c. specialist-only: the single row is category 'specialist'",
    len(s1["rows"]) == 1 and s1["rows"][0].get("category") == "specialist", f"rows={s1['rows']}")
chk("1d. specialist-only: the row names the ticket in 'why'",
    "AUTO-90" in str(s1["rows"][0].get("why", "")), f"why={s1['rows'][0].get('why')!r}")

# ───────────────────────────────────────────────────────────────────────────
# 2. Cockpit board — the Needs-you badge/KPI card were deliberately removed (5a882a6,
# 2026-07-01 "refactor: remove Needs you dashboard and UI components"); /needs is the
# surface now. Pin the removal + that the ONE number still holds at the data layer.
# ───────────────────────────────────────────────────────────────────────────
board1 = warroom.render_board(cfg1, None, {"active": False})
chk("2a. board no longer renders a Needs-you badge (removed by 5a882a6)",
    "Needs you ·" not in board1)
chk("2b. no Needs-you KPI card either (removed by 5a882a6)",
    _needs_card_value(cfg1) is None, f"card={_needs_card_value(cfg1)}")
chk("2c. the ONE number still holds at the data layer: count() == summary total == 1",
    needs.count(cfg1) == s1["total"] == 1, f"count={needs.count(cfg1)} total={s1['total']}")

# ───────────────────────────────────────────────────────────────────────────
# 3. /needs renders an actionable specialist section (no dead-end)
# ───────────────────────────────────────────────────────────────────────────
client1 = server.create_app(cfg1).test_client()
r1 = client1.get("/needs")
body1 = r1.get_data(as_text=True)
chk("3a. /needs returns 200", r1.status_code == 200, str(r1.status_code))
chk("3b. /needs has a 'Specialist rosters' section", "Specialist rosters" in body1)
chk("3c. /needs offers Approve (routes to /api/answer with text=approve)",
    "Approve &amp; provision" in body1 and "value='approve'" in body1 and "/api/answer" in body1)
chk("3d. /needs offers Decline (text=decline)", "value='decline'" in body1)
chk("3e. /needs shows the pinned roster (charter name)", "MQL5 Algo Specialist" in body1)
chk("3f. /needs is NOT the 'All clear' dead-end", "All clear — nothing needs you" not in body1)

# ───────────────────────────────────────────────────────────────────────────
# 4. Mixed state — decision + errored + specialist: the ONE number holds at the data
# layer (board badge/KPI card removed by 5a882a6 — see section 2).
# ───────────────────────────────────────────────────────────────────────────
tmp2 = Path(tempfile.mkdtemp())
cfg2 = _make_cfg(tmp2)
_seed_specialist(tmp2)
(tmp2 / "pending_decisions.json").write_text(json.dumps(
    [{"id": "AUTO-9", "app": "automatixy", "question": "DD/MM or MM/DD?", "summary": "date"}]))
# A realistic errored run dict (datetime fields, like dashboard.load_tasks emits) so the board's
# feed/active-run renderers sort cleanly — not a half-populated stub.
_errored_run = {"ticket_id": "AUTO-7", "outcome": "errored", "app": "automatixy", "branch": "auto/AUTO-7",
                "note": "build blew up", "started": datetime(2026, 6, 28, 8, 0, 0), "ended": None,
                "passes": 1, "turns": 3, "cost": 0.0, "verdict": "FAIL", "pr_url": None,
                "dry_run": None, "detail": {}, "duration": None, "passes_list": []}
D.load_tasks = lambda _p: [_errored_run]
D.load_dismissed = lambda _p: {}

cnt2 = needs.count(cfg2)
board2 = warroom.render_board(cfg2, None, {"active": False})
chk("4a. mixed: count() == 3 (decision + errored + specialist)", cnt2 == 3, f"count={cnt2}")
chk("4b. mixed: board renders with no Needs-you badge (removed by 5a882a6)",
    "Needs you ·" not in board2)
chk("4c. mixed: no Needs-you KPI card (removed by 5a882a6)",
    _needs_card_value(cfg2) is None, f"card={_needs_card_value(cfg2)}")
chk("4d. mixed: count() == len(rows) == total", needs.summary(cfg2)["total"] == cnt2)

# ── tally ──────────────────────────────────────────────────────────────────
print("\n======== EU-102 SPECIALIST INBOX QA ========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("--------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results) - passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
