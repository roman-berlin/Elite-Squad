"""EU-314 QA: the per-project pipeline board — orchestrator.cockpit_views._pipeline_board().

Renders, for the ACTIVE project tab only, every in-flight/recent ticket with its current stage
(dashboard.derive_pipeline_stage — a small local stand-in for Sub-ticket 1's not-yet-landed
canonical helper, see the note on that function) and a human age string. Scoped exactly like
needs.summary(cfg, app_name) already scopes per app (EU-129 pattern) — via the SAME
needs._row_matches_app rule (row 'app' field, else ticket-key prefix) — never a single global list.

Four test groups (one per testable acceptance criterion):
  1. A single in-flight ticket renders its id, a non-empty stage label, and a human age string.
  2. Per-project isolation: app=A sees only A's tickets, app=B sees only B's — mirrors needs.py's
     _row_matches_app scoping.
  3. The 'non-Merged-and-old' age-filter rule: a recently-merged ticket still shows (with its age);
     an old-merged ticket is dropped.
  4. dashboard.render_html(...) wires the board in right after the KPI cards_html block.
"""
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path

# --- stub the Agent SDK so orchestrator modules import cleanly ---
_sdk = types.ModuleType("claude_agent_sdk")
class _Stub:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
_sdk.__getattr__ = lambda _n: _Stub
sys.modules["claude_agent_sdk"] = _sdk
# server.py (imported for the end-to-end route test) pulls in `requests` transitively — stub it so
# the suite stays hermetic (no network), mirroring decompose_server_test.py.
_req = types.ModuleType("requests")
_req.Session = lambda: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules.setdefault("requests", _req)

sys.path.insert(0, ".")

from orchestrator import cockpit_views, dashboard

results: list[tuple[str, bool, str]] = []

def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail) if not cond else ""))


def _task(ticket_id: str, app: str, **over) -> dict:
    base = {
        "ticket_id": ticket_id, "app": app, "branch": f"auto/{ticket_id}",
        "started": None, "ended": None, "passes": 0, "turns": 0, "cost": 0.0,
        "verdict": None, "outcome": None, "pr_url": None, "dry_run": None,
        "note": "", "detail": {}, "passes_list": [], "duration": None,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Test 1 — a single in-flight ticket: id, stage label, and human age string all present
# ---------------------------------------------------------------------------

def test_in_flight_ticket_renders_id_stage_age() -> None:
    started = datetime.now() - timedelta(hours=2, minutes=14)
    t = _task("AUTO-50", "automatixy", started=started, passes=1)

    board = cockpit_views._pipeline_board(None, [t], "automatixy")

    chk("1a. ticket id appears in the board", "AUTO-50" in board, board)
    chk("1b. a non-empty stage label appears (Building — in-flight, no outcome yet)",
        "Building" in board, board)
    chk("1c. the age renders in human '2h 14m' form (dashboard._human_dur)", "2h 14m" in board, board)


# ---------------------------------------------------------------------------
# Test 2 — per-project isolation, mirroring needs.py's _row_matches_app scoping
# ---------------------------------------------------------------------------

def test_per_project_isolation() -> None:
    t_a = _task("AUTO-100", "automatixy", started=datetime.now() - timedelta(minutes=10))
    t_b = _task("EU-100", "Elite-Unit", started=datetime.now() - timedelta(minutes=10))
    tasks = [t_a, t_b]

    board_a = cockpit_views._pipeline_board(None, tasks, "automatixy")
    board_b = cockpit_views._pipeline_board(None, tasks, "Elite-Unit")

    chk("2a. app=automatixy board contains automatixy's ticket", "AUTO-100" in board_a, board_a)
    chk("2b. app=automatixy board omits Elite-Unit's ticket", "EU-100" not in board_a, board_a)
    chk("2c. app=Elite-Unit board contains Elite-Unit's ticket", "EU-100" in board_b, board_b)
    chk("2d. app=Elite-Unit board omits automatixy's ticket", "AUTO-100" not in board_b, board_b)


# ---------------------------------------------------------------------------
# Test 3 — the 'non-Merged-and-old' age-filter rule
# ---------------------------------------------------------------------------

def test_merged_recency_rule() -> None:
    recent_end = datetime.now() - timedelta(minutes=5)
    old_end = datetime.now() - timedelta(minutes=45)
    recent = _task("AUTO-200", "automatixy", outcome="merged→dev",
                    started=recent_end - timedelta(minutes=10), ended=recent_end)
    old = _task("AUTO-201", "automatixy", outcome="merged→dev",
                started=old_end - timedelta(minutes=10), ended=old_end)

    board = cockpit_views._pipeline_board(None, [recent, old], "automatixy")

    chk("3a. a recently-merged ticket still shows", "AUTO-200" in board, board)
    chk("3b. ...WITH its age, not just a bare id", "AUTO-200" in board and "5m" in board, board)
    chk("3c. an old-merged ticket is dropped from the board", "AUTO-201" not in board, board)


# ---------------------------------------------------------------------------
# Test 4 — dashboard.render_html wires the board in right after the KPI cards block
# ---------------------------------------------------------------------------

def test_render_html_positions_board_after_cards() -> None:
    t = _task("AUTO-300", "automatixy", started=datetime.now() - timedelta(minutes=3))
    html_out = dashboard.render_html([t], app_name="automatixy")

    cards_idx = html_out.find("<div class=cards>")
    board_idx = html_out.find("class=pbrow") if "class=pbrow" in html_out else html_out.find("Pipeline")
    chk("4a. render_html includes the pipeline-board markup", board_idx != -1, html_out[:400])
    chk("4b. the board is positioned AFTER the KPI cards block",
        cards_idx != -1 and board_idx > cards_idx, f"cards_idx={cards_idx} board_idx={board_idx}")
    chk("4c. the board's ticket shows up in the rendered page", "AUTO-300" in html_out)

    # No app_name -> no board (unscoped / static `general dashboard` usage stays unchanged).
    html_unscoped = dashboard.render_html([t])
    chk("4d. with no app_name, no board markup is added (backward compatible)",
        "class=pbrow" not in html_unscoped, html_unscoped[:400])


# ---------------------------------------------------------------------------
# Test 5 — END-TO-END through the live cockpit route (server.py /tasks?app=…).
# The direct-call tests above prove _pipeline_board scopes correctly; this proves the WIRING:
# GET /tasks?app=<project> (what the _tab_bar sets when you switch tabs) actually reaches
# render_html(cfg=…, app_name=…) and renders THAT tab's board — switching ?app= changes the
# board's ticket set in the running app, not just in a unit call. This is the regression the
# iteration-1 review flagged (render_html was called with no cfg/app_name, so no board rendered).
# ---------------------------------------------------------------------------

# A board row wraps its ticket id in this exact span (cockpit_views._pipeline_board) — a
# board-SPECIFIC marker, so we don't accidentally match the full task table below the board
# (which lists every run regardless of the active tab).
def _board_has(page: str, ticket_id: str) -> bool:
    return f'pbid" style="font-weight:600">{ticket_id}</span>' in page


def test_end_to_end_tab_switch_changes_board() -> None:
    from orchestrator import server
    from orchestrator.config import AppConfig, Config

    tmp = Path(tempfile.mkdtemp())
    audit = tmp / "audit.jsonl"
    # Two apps, each with one recent in-flight ticket — the "stubs two apps' audit logs" the
    # acceptance criterion asks for.
    ts = (datetime.now() - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%S")
    audit.write_text(
        '{"event":"ticket_start","ticket_id":"AUTO-500","app":"automatixy",'
        f'"branch":"auto/AUTO-500","ts":"{ts}"}}\n'
        '{"event":"ticket_start","ticket_id":"EU-500","app":"Elite-Unit",'
        f'"branch":"auto/EU-500","ts":"{ts}"}}\n',
        encoding="utf-8")

    cfg = Config(
        apps=[
            AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                      protected_branch="MAIN", backlog_backend="none"),
            AppConfig(name="Elite-Unit", repo_path=str(tmp), base_branch="dev",
                      protected_branch="main", backlog_backend="none"),
        ],
        audit_path=str(audit), use_worktree=False)

    client = server.create_app(cfg).test_client()
    page_a = client.get("/tasks?app=automatixy").get_data(as_text=True)
    page_b = client.get("/tasks?app=Elite-Unit").get_data(as_text=True)

    chk("5a. /tasks?app=automatixy renders a board with automatixy's ticket",
        _board_has(page_a, "AUTO-500"), page_a[:200])
    chk("5b. ...and NOT Elite-Unit's ticket in that board",
        not _board_has(page_a, "EU-500"), "EU-500 leaked into automatixy's board")
    chk("5c. switching ?app=Elite-Unit renders a board with Elite-Unit's ticket",
        _board_has(page_b, "EU-500"), page_b[:200])
    chk("5d. ...and NOT automatixy's ticket in that board",
        not _board_has(page_b, "AUTO-500"), "AUTO-500 leaked into Elite-Unit's board")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

test_in_flight_ticket_renders_id_stage_age()
test_per_project_isolation()
test_merged_recency_rule()
test_render_html_positions_board_after_cards()
test_end_to_end_tab_switch_changes_board()

print("\n============= EU-314 PIPELINE BOARD QA =============")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
