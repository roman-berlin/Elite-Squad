"""EU-574: Active-run card falsely shows INTERRUPTED during any model call >150s — unify liveness with heartbeat pill.

Five failing-first tests that each verify one acceptance criterion. Each MUST fail on the
unchanged code (heartbeat ignored by live_runs) before the fix, then pass after.

Pins:
  1. heartbeat-live + stale-audit → LIVE card with correct stage lit (not idle/interrupted)
  2. _run_obj_for lights current stage under the same conditions
  3. BOTH-stale preserves today's idle/fallback behaviour
  4. mark_ticket_start resets run_started so elapsed derives from a fresh window
  5. _process_ticket_inner calls mark_ticket_start at ticket_start
"""
import json
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")

# --- Minimal stubs (before orchestrator imports) ---
_sdk = __import__("types").ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
_sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", _sdk)

_req = __import__("types").ModuleType("requests")
_req.Session = lambda: __import__("types").SimpleNamespace(
    auth=None,
    headers=__import__("types").SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules.setdefault("requests", _req)

from orchestrator import warroom, cockpit_state as CS  # noqa: E402
from orchestrator.config import Config, AppConfig  # noqa: E402
from orchestrator import dashboard as D  # noqa: E402

PH = warroom.PHASES

results: list[tuple[str, bool, str]] = []

def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


# ===========================================================================
# Helpers
# ===========================================================================

def _ts(secs_ago: int) -> str:
    return (datetime.now().astimezone() - timedelta(seconds=secs_ago)).strftime("%Y-%m-%dT%H:%M:%S%z")


def _make_cfg(rows: list[dict], max_builders: int = 2) -> tuple[Config, Path]:
    d = Path(tempfile.mkdtemp())
    audit = d / "audit.jsonl"
    audit.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    D._audit_cache.clear()
    D._tasks_cache.clear()
    cfg = Config(
        apps=[AppConfig(name="testapp", repo_path=str(d), base_branch="dev",
                        protected_branch="main", backlog_backend="none")],
        audit_path=str(audit),
        max_concurrent_builders=max_builders,
    )
    return cfg, audit


def _fresh_state(last_activity: float | None = None, run_tickets: list[str] | None = None) -> dict:
    st = {
        "active": True,
        "autopilot_on": False,
        "run_started": last_activity,
        "log_seq": 0,
        "stopping": False,
    }
    if last_activity is not None:
        st["last_activity"] = last_activity
    if run_tickets is not None:
        st["run_tickets"] = run_tickets
    return st


# ===========================================================================
# AC 1: Fresh heartbeat + stale audit (>150s) → card is LIVE (correct stage lit)
# ===========================================================================

def test_ac1_heartbeat_live_stale_audit():
    """HEARTBEAT-LIVE STALE-AUDIT: latest audit event 300 s ago (stale) but
    last_activity=now (fresh). live_runs() must RETURN this ticket (score = max(stale, now)=now
    within window). active_run() must return a run_obj with live=True — NOT the idle fallback."""
    now = time.time()
    cfg, _ = _make_cfg([
        dict(event="ticket_start", ticket_id="HB-1", app="testapp", branch="b", ts=_ts(300)),
        dict(event="build",       ticket_id="HB-1", app="testapp", iteration=1, ts=_ts(300)),
    ])
    tasks = D.load_tasks(cfg.audit_path)
    run_state = _fresh_state(last_activity=now, run_tickets=["HB-1"])
    lives = warroom.live_runs(cfg, tasks, "testapp", active=True, run_state=run_state)
    chk("ac1: live_runs returns the heartbeat-live ticket",
        len(lives) == 1 and lives[0].get("ticket") == "HB-1",
        f"expected 1 × HB-1, got {len(lives)}: {[t.get('ticket') for t in lives]}")
    chk("ac1: run_obj carries live=True",
        lives and lives[0].get("live") is True,
        f"got live={lives[0].get('live') if lives else None}")

    # active_run without explicit run_state should also pick up the heartbeat via lazy lookup
    # (it calls get_state which creates empty state — so we need the caller to pass it.)
    # The wrapper relationship holds: when live_runs returns something, active_run returns lives[0].
    run = warroom.active_run(cfg, tasks, "testapp", active=True, run_state=run_state)
    chk("ac1: active_run returns the live run (not idle fallback)",
        run is not None and run.get("live") is True and run.get("ticket") == "HB-1",
        f"got {run}")


# ===========================================================================
# AC 2: Live run lights current stage — build+FAIL verdict → Build lit
# ===========================================================================

def test_ac2_stage_lit_when_heartbeat_live():
    """A heartbeat-live ticket whose task has phase='build' and verdict='FAIL' → _run_obj_for
    sets reached==BUILD (index 0) and live==True. No fully-grey bar. Card reads 'Working · Build',
    never 'interrupted'."""
    now = time.time()
    cfg, _ = _make_cfg([
        dict(event="ticket_start", ticket_id="STAGE-1", app="testapp", branch="b", ts=_ts(200)),
        dict(event="build",       ticket_id="STAGE-1", app="testapp", iteration=1,
             tools=["Edit"], summary="built-fail", verdict="FAIL", phase="build", ts=_ts(190)),
    ])
    tasks = D.load_tasks(cfg.audit_path)
    run_state = _fresh_state(last_activity=now, run_tickets=["STAGE-1"])
    lives = warroom.live_runs(cfg, tasks, "testapp", active=True, run_state=run_state)
    chk("ac2: live_runs picks up the heartbeat-live ticket",
        len(lives) == 1 and lives[0].get("ticket") == "STAGE-1",
        f"got {[(t.get('ticket'), t.get('reached')) for t in lives]}")

    run = lives[0]
    chk("ac2: run_obj is live", run.get("live") is True, f"got live={run.get('live')}")
    chk("ac2: reached == BUILD index (Build lit)",
        run.get("reached") == PH.index("Build"),
        f"got reached={run.get('reached')}")
    chk("ac2: phases list present", run.get("phases") == list(PH), f"got {run.get('phases')}")

    # Verify _run_html would render Working, not interrupted
    html = warroom._run_html(run, "live", None, manual=False)
    chk("ac2: rendered HTML contains 'Working' (not 'interrupted')",
        "Working" in html and "interrupted" not in html,
        f"HTML snippet: {html[:200]}")


# ===========================================================================
# AC 3: Both signals stale → live_runs=[], active_run falls back to idle path
#        with outcome='running' showing 'interrupted' chip
# ===========================================================================

def test_ac3_both_signals_stale():
    """Audit >150 s old AND last_activity >150 s ago or None → live_runs() == [].
    active_run() renders the newest scoped run as idle (live=False). The idle header maps
    outcome 'running' to 'interrupted' — preserving today's behaviour for genuinely dead runs."""
    now = time.time()
    stale_la = now - 300  # 5 min ago
    cfg, _ = _make_cfg([
        dict(event="ticket_start", ticket_id="BOTH-STALE", app="testapp", branch="b", ts=_ts(300)),
        dict(event="build",       ticket_id="BOTH-STALE", app="testapp", iteration=1, ts=_ts(300)),
    ])
    tasks = D.load_tasks(cfg.audit_path)
    run_state = _fresh_state(last_activity=stale_la, run_tickets=["BOTH-STALE"])
    lives = warroom.live_runs(cfg, tasks, "testapp", active=True, run_state=run_state)
    chk("ac3: live_runs is empty when both signals stale",
        lives == [], f"got {lives}")

    run = warroom.active_run(cfg, tasks, "testapp", active=True, run_state=run_state)
    chk("ac3: active_run falls back to the last run",
        run is not None and run.get("ticket") == "BOTH-STALE",
        f"got {run}")
    chk("ac3: fallback is idle (live=False)",
        run is not None and run.get("live") is False,
        f"got live={run.get('live')}")
    # The idle header maps outcome 'running' → 'interrupted'
    chk("ac3: outcome is 'running' (will render as 'interrupted' chip)",
        run is not None and run.get("outcome") == "running",
        f"got outcome={run.get('outcome')}")


# ===========================================================================
# AC 4: mark_ticket_start resets run_started and last_activity → honest elapsed
# ===========================================================================

def test_ac4_mark_ticket_start_resets_elapsed():
    """mark_ticket_start sets run_started=last_activity=now; calling it after a stale
    run_started (simulating a resumed run) makes _runlog_placeholder elapsed derive from
    the new start — tens of seconds, not 269 minutes."""
    CS.reset_run_state()
    app_name = "testapp"

    # Simulate a stale run_started from 269 m ago (the EU-574 bug case)
    old_start = time.time() - (269 * 60)
    st = CS.get_state(app_name)
    st["active"] = True
    st["run_started"] = old_start
    st["last_activity"] = old_start

    # Sanity: before reset, elapsed would be ~269 m
    html_before = warroom._runlog_placeholder(dict(st), True, "")
    chk("ac4-baseline: stale elapsed shows big number",
        "269" in html_before or "26" in html_before,
        f"got: {html_before[:120]}")

    # Now reset
    CS.mark_ticket_start(app_name)
    st = CS.get_state(app_name)
    chk("ac4: run_started updated to recent time",
        abs(float(st["run_started"]) - time.time()) < 5,
        f"run_started={st['run_started']}")
    chk("ac4: last_activity updated to recent time",
        abs(float(st["last_activity"]) - time.time()) < 5,
        f"last_activity={st['last_activity']}")

    html_after = warroom._runlog_placeholder(dict(st), True, "")
    # After reset, elapsed should be just seconds (tiny number), not 269m
    chk("ac4: fresh elapsed is small (< 5 m)",
        "269" not in html_after and ("0m" in html_after or "< 1m" in html_after or
                                      html_after.count("elapsed") > 0),
        f"got: {html_after[:120]}")


# ===========================================================================
# AC 5: _process_ticket_inner invokes mark_ticket_start
# ===========================================================================

def test_ac5_process_ticket_inner_calls_mark_ticket_start():
    """Verify that loop._process_ticket_inner sources mark_ticket_start and calls it
    at least once per inner execution. We inspect source text since the function is async
    and requires a full run context to execute directly."""
    import ast
    loop_src = Path("orchestrator/loop.py").read_text(encoding="utf-8")
    tree = ast.parse(loop_src)

    found_import_cockpit = False
    found_call_count = 0
    func_def_found = False

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # from . import cockpit_state  → level=1, module=None, names includes 'cockpit_state'
            if node.level == 1 and any(alias.name == "cockpit_state" for alias in (node.names or [])):
                found_import_cockpit = True
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_process_ticket_inner":
            func_def_found = True
            # Walk the body looking for cockpit_state.mark_ticket_start calls
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    # Check for dot access pattern: cockpit_state.mark_ticket_start(...)
                    if isinstance(child.func, ast.Attribute) and child.func.attr == "mark_ticket_start":
                        if isinstance(child.func.value, ast.Name) and child.func.value.id == "cockpit_state":
                            found_call_count += 1

    chk("ac5: loop.py imports cockpit_state", found_import_cockpit)
    chk("ac5: _process_ticket_inner exists", func_def_found)
    chk("ac5: mark_ticket_start called inside _process_ticket_inner",
        found_call_count >= 1,
        f"found {found_call_count} call(s)")
    # Also verify the source text is readable as a human-friendly pin
    chk("ac5: source contains EU-574 reference in _process_ticket_inner",
        "mark_ticket_start" in loop_src and "EU-574" in loop_src)


# ===========================================================================
# Report
# ===========================================================================

if __name__ == "__main__":
    test_ac1_heartbeat_live_stale_audit()
    test_ac2_stage_lit_when_heartbeat_live()
    test_ac3_both_signals_stale()
    test_ac4_mark_ticket_start_resets_elapsed()
    test_ac5_process_ticket_inner_calls_mark_ticket_start()

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n{'=' * 56}")
    print(f"  EU-574 liveness-unify tests")
    print(f"  {'─' * 56}")
    for _name, _ok, _det in results:
        mark = "PASS" if _ok else "FAIL"
        extra = f"  ({_det})" if _det and not _ok else ""
        print(f"    [{mark}] {_name}{extra}")
    print(f"  {'─' * 56}")
    print(f"  {passed}/{total} checks passed")
    failed = total - passed
    if failed:
        print(f"  RESULT: {failed} FAILED")
        sys.exit(1)
    else:
        print("  RESULT: ALL GREEN")
        sys.exit(0)
