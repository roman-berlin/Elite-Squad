"""EU-129 QA: needs.summary()/needs.count() are per-project, and the audit is parsed at most
once per summary() call (not up to 4x per tab switch).

Bug fixed: summary()/count() took no app and always aggregated EVERY project, so the Needs-you
badge/KPI card showed the SAME number on every cockpit tab. Data is already app-tagged (decisions/
proposals carry 'app'; specialist rosters carry 'app_name'; audit task rows carry 'app' and
ticket-key prefixes like EU-*/AUTO-*), so both entry points now take an ``app_name`` parameter:

  - a concrete app name filters every stream to that project (by 'app'/'app_name' field where the
    row carries one, else by ticket-key prefix for rows that don't — errored/parked/PR/specialist);
  - ``None`` / ``""`` / ``needs.ALL_PROJECTS`` ("*") preserves the old "aggregate everything"
    behaviour (the "All projects" view).

Separately, ``summary()`` used to call ``dashboard.load_tasks(cfg.audit_path)`` TWICE per call (the
errored/PR pass and the parked pass each parsed the whole audit from scratch), and the cockpit board
calls ``summary()`` twice per render (KPI card + side panel) — so a single tab switch could parse
the (potentially multi-thousand-line) audit log up to 4x. ``load_tasks`` is now called at most once
per ``summary()`` call, and the whole result is memoized for a few seconds per
(audit signature, app_name) so back-to-back calls in the same render share one computation.

Five test groups:
  1. A decision tagged app='automatixy' does NOT count for app='Elite-Unit' (per-app filter, own field).
  2. An errored run tagged app='automatixy' does NOT count for app='Elite-Unit' either — same
     invariant, but for a row filtered by ticket-key PREFIX fallback semantics (no 'app' field path
     wouldn't apply here since audit rows DO carry 'app', so this also exercises the 'app' field path
     for the errored/PR/parked stream specifically).
  3. "All projects" (None and the "*" sentinel) still aggregates every project's items — the old,
     unscoped total.
  4. count() == len(summary(app)['rows']) holds per-app too (the EU-93 invariant, now per-app).
  5. dashboard.load_tasks is called AT MOST ONCE per summary() call (perf fix) — verified via a
     call-counting stub, and summary() is memoized so two back-to-back calls for the SAME
     (audit, app) share one computation (load_tasks called once total, not once per call).
"""
import json
import sys
import tempfile
import types
from pathlib import Path

# --- stub the Agent SDK so orchestrator modules import cleanly ---
_sdk = types.ModuleType("claude_agent_sdk")
class _Stub:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
_sdk.__getattr__ = lambda _n: _Stub
sys.modules["claude_agent_sdk"] = _sdk

sys.path.insert(0, ".")

from orchestrator import needs, dashboard
from orchestrator.config import Config, AppConfig

results: list[tuple[str, bool, str]] = []

def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail) if not cond else ""))


def _make_multi_app_cfg(tmp: Path) -> Config:
    """A Config with TWO configured projects, sharing one audit log (as in the real cockpit)."""
    (tmp / "audit.jsonl").write_text("")
    apps = [
        AppConfig(name="automatixy", repo_path=str(tmp / "automatixy"), base_branch="DEV",
                  protected_branch="MAIN", backlog_backend="none"),
        AppConfig(name="Elite-Unit", repo_path=str(tmp / "eliteunit"), base_branch="DEV",
                  protected_branch="MAIN", backlog_backend="none"),
    ]
    return Config(apps=apps, audit_path=str(tmp / "audit.jsonl"), use_worktree=False)


def _decision(decision_id: str, app_name: str) -> dict:
    return {"id": decision_id, "app": app_name, "question": f"question for {decision_id}",
            "summary": "test decision"}


def _run(ticket_id: str, app_name: str, outcome: str = "errored") -> dict:
    return {"ticket_id": ticket_id, "outcome": outcome, "app": app_name,
            "note": "build blew up", "started": "2026-06-20T10:00:00"}


# ---------------------------------------------------------------------------
# Test 1 — a decision tagged for one app doesn't count for another app
# ---------------------------------------------------------------------------

def test_decision_scoped_by_app() -> None:
    tmp = Path(tempfile.mkdtemp())
    cfg = _make_multi_app_cfg(tmp)
    needs.clear_cache()

    (tmp / "pending_decisions.json").write_text(json.dumps([
        _decision("AUTO-9", "automatixy"),
        _decision("EU-40", "Elite-Unit"),
    ]))
    dashboard.load_tasks = lambda _p: []
    dashboard.load_dismissed = lambda _p: {}

    s_auto = needs.summary(cfg, "automatixy")
    s_eu = needs.summary(cfg, "Elite-Unit")

    chk("1a. automatixy tab sees only its own decision",
        [d["id"] for d in s_auto["decisions"]] == ["AUTO-9"],
        f"decisions={s_auto['decisions']}")
    chk("1b. Elite-Unit tab sees only its own decision",
        [d["id"] for d in s_eu["decisions"]] == ["EU-40"],
        f"decisions={s_eu['decisions']}")
    chk("1c. automatixy count() == 1 (not both projects' decisions)",
        needs.count(cfg, "automatixy") == 1,
        f"count={needs.count(cfg, 'automatixy')}")
    chk("1d. Elite-Unit count() == 1 (not both projects' decisions)",
        needs.count(cfg, "Elite-Unit") == 1,
        f"count={needs.count(cfg, 'Elite-Unit')}")


# ---------------------------------------------------------------------------
# Test 2 — an errored run tagged for one app doesn't count for another app
# ---------------------------------------------------------------------------

def test_errored_run_scoped_by_app() -> None:
    tmp = Path(tempfile.mkdtemp())
    cfg = _make_multi_app_cfg(tmp)
    needs.clear_cache()

    dashboard.load_tasks = lambda _p: [
        _run("AUTO-7", "automatixy", outcome="errored"),
        _run("EU-129", "Elite-Unit", outcome="errored"),
    ]
    dashboard.load_dismissed = lambda _p: {}

    s_auto = needs.summary(cfg, "automatixy")
    s_eu = needs.summary(cfg, "Elite-Unit")

    chk("2a. automatixy tab sees only its own errored run",
        [t["ticket_id"] for t in s_auto["tasks"]] == ["AUTO-7"],
        f"tasks={s_auto['tasks']}")
    chk("2b. Elite-Unit tab sees only its own errored run",
        [t["ticket_id"] for t in s_eu["tasks"]] == ["EU-129"],
        f"tasks={s_eu['tasks']}")
    chk("2c. automatixy count() == 1",
        needs.count(cfg, "automatixy") == 1,
        f"count={needs.count(cfg, 'automatixy')}")
    chk("2d. Elite-Unit count() == 1",
        needs.count(cfg, "Elite-Unit") == 1,
        f"count={needs.count(cfg, 'Elite-Unit')}")
    chk("2e. a project's badge is NOT the global total (the bug this ticket fixes)",
        needs.count(cfg, "automatixy") != needs.count(cfg, None),
        f"automatixy={needs.count(cfg, 'automatixy')}  all={needs.count(cfg, None)}")


# ---------------------------------------------------------------------------
# Test 3 — "All projects" (None and the "*" sentinel) still aggregates everything
# ---------------------------------------------------------------------------

def test_all_projects_still_aggregates() -> None:
    tmp = Path(tempfile.mkdtemp())
    cfg = _make_multi_app_cfg(tmp)
    needs.clear_cache()

    (tmp / "pending_decisions.json").write_text(json.dumps([
        _decision("AUTO-9", "automatixy"),
        _decision("EU-40", "Elite-Unit"),
    ]))
    dashboard.load_tasks = lambda _p: [
        _run("AUTO-7", "automatixy", outcome="errored"),
        _run("EU-129", "Elite-Unit", outcome="errored"),
    ]
    dashboard.load_dismissed = lambda _p: {}

    s_none = needs.summary(cfg, None)
    s_star = needs.summary(cfg, needs.ALL_PROJECTS)
    s_empty = needs.summary(cfg, "")

    chk("3a. app_name=None aggregates both projects' decisions",
        len(s_none["decisions"]) == 2, f"decisions={s_none['decisions']}")
    chk("3b. app_name=None aggregates both projects' errored runs",
        len(s_none["tasks"]) == 2, f"tasks={s_none['tasks']}")
    chk("3c. app_name='*' (ALL_PROJECTS sentinel) matches app_name=None",
        s_star["total"] == s_none["total"], f"star={s_star['total']} none={s_none['total']}")
    chk("3d. app_name='' also matches the all-projects total",
        s_empty["total"] == s_none["total"], f"empty={s_empty['total']} none={s_none['total']}")
    chk("3e. all-projects count() == 4 (2 decisions + 2 errored runs)",
        needs.count(cfg, None) == 4, f"count={needs.count(cfg, None)}")
    chk("3f. all-projects total == sum of the two per-app totals",
        needs.count(cfg, None) == needs.count(cfg, "automatixy") + needs.count(cfg, "Elite-Unit"),
        f"all={needs.count(cfg, None)} automatixy={needs.count(cfg, 'automatixy')} "
        f"eu={needs.count(cfg, 'Elite-Unit')}")


# ---------------------------------------------------------------------------
# Test 4 — count() == len(rows) invariant holds PER-APP (EU-93, now per-project)
# ---------------------------------------------------------------------------

def test_count_equals_rows_invariant_per_app() -> None:
    tmp = Path(tempfile.mkdtemp())
    cfg = _make_multi_app_cfg(tmp)
    needs.clear_cache()

    (tmp / "pending_decisions.json").write_text(json.dumps([_decision("AUTO-9", "automatixy")]))
    dashboard.load_tasks = lambda _p: [_run("AUTO-7", "automatixy", outcome="errored"),
                                       _run("EU-129", "Elite-Unit", outcome="errored")]
    dashboard.load_dismissed = lambda _p: {}

    for scope in ("automatixy", "Elite-Unit", None):
        s = needs.summary(cfg, scope)
        chk(f"4. count()=={ 'len(rows)' } holds for scope={scope!r}",
            needs.count(cfg, scope) == len(s["rows"]) == s["total"],
            f"scope={scope} count={needs.count(cfg, scope)} rows={len(s['rows'])} total={s['total']}")


# ---------------------------------------------------------------------------
# Test 5 — dashboard.load_tasks is called AT MOST ONCE per summary() call, and
# summary() memoizes so back-to-back calls for the SAME (audit, app) share one computation.
# ---------------------------------------------------------------------------

def test_load_tasks_called_once_per_summary() -> None:
    tmp = Path(tempfile.mkdtemp())
    cfg = _make_multi_app_cfg(tmp)
    needs.clear_cache()

    call_count = {"n": 0}
    real_load_tasks = dashboard.load_tasks

    def counting_load_tasks(p):
        call_count["n"] += 1
        return [_run("AUTO-7", "automatixy", outcome="errored")]

    dashboard.load_tasks = counting_load_tasks
    dashboard.load_dismissed = lambda _p: {}
    try:
        needs.summary(cfg, "automatixy")
        chk("5a. summary() calls load_tasks() at most once (not the old 2x)",
            call_count["n"] <= 1, f"calls={call_count['n']}")

        # A second call for the SAME (audit, app) within the memo TTL must be served from cache —
        # load_tasks must NOT be called again.
        calls_before = call_count["n"]
        needs.summary(cfg, "automatixy")
        chk("5b. a second summary() call for the same (audit, app) is memoized — no extra load_tasks",
            call_count["n"] == calls_before, f"before={calls_before} after={call_count['n']}")

        # count() internally calls summary() too — still must not add a fresh load_tasks call.
        calls_before2 = call_count["n"]
        needs.count(cfg, "automatixy")
        chk("5c. count() reuses the memoized summary() — no extra load_tasks call",
            call_count["n"] == calls_before2, f"before={calls_before2} after={call_count['n']}")

        # A genuinely different scope (different app) is a different cache key — it MAY reparse,
        # but still at most once for that call.
        calls_before3 = call_count["n"]
        needs.summary(cfg, "Elite-Unit")
        chk("5d. a different app scope costs at most one more load_tasks call",
            call_count["n"] - calls_before3 <= 1,
            f"before={calls_before3} after={call_count['n']}")
    finally:
        dashboard.load_tasks = real_load_tasks


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

test_decision_scoped_by_app()
test_errored_run_scoped_by_app()
test_all_projects_still_aggregates()
test_count_equals_rows_invariant_per_app()
test_load_tasks_called_once_per_summary()

print("\n======== EU-129 PER-PROJECT NEEDS-YOU QA ========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
