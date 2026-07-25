"""EU-477: live_runs() is the single shared live-subset computation, and active_run()
is a thin wrapper over live_runs()[0].

Guards:
  1. Two fresh non-terminal same-app runs → exactly 2 run-object dicts, each carrying its
     own ticket, newest-first.
  2. Only one run live → exactly 1.
  3. A terminal run (outcome set) is excluded even inside the 150 s window.
  4. A stale run (last audit activity >150 s ago) is excluded — INCLUDING the newest
     scoped task: there is NO always-include-ts[0] exception (the written AC wins over the
     iteration-1 NOTE; a run silent for >150 s reads as crashed/interrupted, and active_run
     renders it as an idle 'last run' via its fallback).
  5. More live runs than max_concurrent_builders → list capped, newest-first.
  6. active=False → [] (idle boards render the last run via active_run's fallback).
  7. Wrapper relationship (review-mandated regression): active_run() == live_runs()[0]
     for the representative fixtures — single live run, EU-448 phase derivation, EU-130
     triage synthesis — and the idle/last-run fallback contract when live_runs() is empty.

Every element live_runs() returns carries the SAME run-object shape active_run() has
always produced (``ticket``, ``app``, ``phases``/``reached``, ``sparkline`` …) plus the
``ticket_id``/``started`` compat keys the EU-486 multi-card loop reads off each element.
Ground truth is the audit: tasks come from ``D.load_tasks`` (one row per ``ticket_start``),
so a live row always has a ticket_start/started event behind it.
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")

# --- Minimal stubs (must appear BEFORE any orchestrator import) ---
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

from orchestrator import warroom, dashboard as D, phases  # noqa: E402
from orchestrator.config import Config, AppConfig  # noqa: E402

PH = phases.PHASES

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


# ===========================================================================
# Helpers
# ===========================================================================

def _ts(secs_ago: int) -> str:
    """Return an ISO-timestamp *secs_ago* in the past."""
    return (datetime.now().astimezone() - timedelta(seconds=secs_ago)).strftime("%Y-%m-%dT%H:%M:%S%z")


def _make_cfg(rows: list[dict], max_builders: int = 2) -> Config:
    """Create a temp Config pointing at a fresh audit populated with *rows*."""
    d = Path(tempfile.mkdtemp())
    audit = d / "audit.jsonl"
    audit.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    # Bust caches so each sub-test sees its own audit data.
    D._audit_cache.clear()
    D._tasks_cache.clear()
    return Config(
        apps=[AppConfig(name="testapp", repo_path=str(d), base_branch="dev",
                        protected_branch="main", backlog_backend="none")],
        audit_path=str(audit),
        max_concurrent_builders=max_builders,
    )


# ===========================================================================
# AC 1: Two fresh non-terminal same-app runs → both returned, newest-first
# ===========================================================================

def test_two_fresh_both_returned():
    """Two fresh non-terminal same-app runs with distinct ticket_ids → exactly 2 run-object
    dicts, each carrying its OWN ticket, newest-first."""
    cfg = _make_cfg([
        dict(event="ticket_start", ticket_id="TWO-B", app="testapp", branch="b", ts=_ts(50)),
        dict(event="ticket_start", ticket_id="TWO-A", app="testapp", branch="c", ts=_ts(10)),
    ], max_builders=2)
    tasks = D.load_tasks(cfg.audit_path)
    lives = warroom.live_runs(cfg, tasks, "testapp", active=True)
    tids = [t.get("ticket") for t in lives]
    chk("two-fresh: exactly 2", len(lives) == 2, f"expected 2, got {len(lives)}: {tids}")
    chk("two-fresh: TWO-A first (newest)", tids[:1] == ["TWO-A"], f"got {tids}")
    chk("two-fresh: TWO-B second", tids[1:2] == ["TWO-B"], f"got {tids}")
    chk("two-fresh: each dict is live", all(t.get("live") for t in lives), f"got {lives}")
    chk("two-fresh: run-object shape (phases/reached)",
        all(t.get("phases") == list(PH) and "reached" in t for t in lives), f"got {lives}")
    chk("two-fresh: compat keys for the EU-486 multi-card loop",
        all(t.get("ticket_id") == t.get("ticket") and "started" in t for t in lives),
        f"got {lives}")


# ===========================================================================
# AC 1b (2026-07-25 regression): the SAME ticket run twice → ONE card, the newest.
# Production shape: EU-474 had a restart-orphaned old run (no terminal outcome, 25h stale)
# plus the current re-run. `last_ts` is a per-ticket MAX, so BOTH rows inherited the fresh
# score and the board rendered the ticket twice ("It shows me two times the same ticket").
# The sibling-mutex guarantees one build per ticket at a time → one live card per ticket_id.
# ===========================================================================

def test_same_ticket_rerun_dedupes_to_one_card():
    cfg = _make_cfg([
        dict(event="ticket_start", ticket_id="DUP-1", app="testapp", branch="b",
             ts=_ts(91_000)),                                     # orphaned old run (~25h ago)
        dict(event="ticket_start", ticket_id="DUP-1", app="testapp", branch="b", ts=_ts(60)),
        dict(event="build", ticket_id="DUP-1", app="testapp", iteration=1, ts=_ts(5)),
    ], max_builders=2)
    tasks = D.load_tasks(cfg.audit_path)
    lives = warroom.live_runs(cfg, tasks, "testapp", active=True)
    tids = [t.get("ticket") for t in lives]
    chk("dup-rerun: exactly ONE card for the twice-run ticket",
        tids == ["DUP-1"], f"expected ['DUP-1'], got {tids}")
    if len(tasks) < 2 or str(tasks[0].get("ticket_id")) != str(tasks[1].get("ticket_id")):
        # If load_tasks itself collapses per-ticket rows this guard is vacuous — flag it.
        chk("dup-rerun: fixture really produced two task rows for one ticket",
            False, f"load_tasks rows: {[(t.get('ticket_id'), t.get('started')) for t in tasks]}")


# ===========================================================================
# AC 2: Only one run live → exactly 1
# ===========================================================================

def test_single_live_returns_one():
    """One genuinely live run → exactly one run-object dict with the right ticket."""
    cfg = _make_cfg([
        dict(event="ticket_start", ticket_id="ONE-1", app="testapp", branch="b", ts=_ts(30)),
        dict(event="build", ticket_id="ONE-1", app="testapp", iteration=1,
             tools=["Edit"], summary="built", ts=_ts(10)),
    ])
    tasks = D.load_tasks(cfg.audit_path)
    lives = warroom.live_runs(cfg, tasks, "testapp", active=True)
    chk("single-live: exactly 1", len(lives) == 1, f"got {len(lives)}")
    chk("single-live: correct ticket", lives and lives[0].get("ticket") == "ONE-1",
        f"got {[t.get('ticket') for t in lives]}")


# ===========================================================================
# AC 3: Terminal runs are excluded
# ===========================================================================

def test_terminal_excluded():
    """DONE-B has an outcome event → absent even though both are within the 150 s window."""
    cfg = _make_cfg([
        dict(event="ticket_start", ticket_id="LIVE-A", app="testapp", branch="b", ts=_ts(20)),
        dict(event="build",       ticket_id="LIVE-A", app="testapp", ts=_ts(10)),
        dict(event="ticket_start", ticket_id="DONE-B", app="testapp", branch="c", ts=_ts(60)),
        dict(event="merged",      ticket_id="DONE-B", app="testapp", ts=_ts(40)),
    ])
    tasks = D.load_tasks(cfg.audit_path)
    lives = warroom.live_runs(cfg, tasks, "testapp", active=True)
    tids = [t.get("ticket") for t in lives]
    chk("terminal: LIVE-A present", "LIVE-A" in tids, f"expected LIVE-A in {tids}")
    chk("terminal: DONE-B absent", "DONE-B" not in tids, f"unexpected DONE-B in {tids}")
    chk("terminal: exactly 1 live", len(lives) == 1, f"expected 1, got {len(lives)}: {tids}")


# ===========================================================================
# AC 4: Stale runs (>150 s without audit activity) are excluded — newest included
# ===========================================================================

def test_stale_non_newest_excluded():
    """FRESH-1 wins; STALE-2 drops because its last audit was ~400 s ago."""
    cfg = _make_cfg([
        dict(event="ticket_start", ticket_id="FRESH-1", app="testapp", branch="b", ts=_ts(10)),
        dict(event="build",       ticket_id="FRESH-1", app="testapp", ts=_ts(5)),
        dict(event="ticket_start", ticket_id="STALE-2", app="testapp", branch="c", ts=_ts(400)),
    ])
    tasks = D.load_tasks(cfg.audit_path)
    lives = warroom.live_runs(cfg, tasks, "testapp", active=True)
    tids = [t.get("ticket") for t in lives]
    chk("stale: FRESH-1 present", "FRESH-1" in tids, f"expected FRESH-1 in {tids}")
    chk("stale: STALE-2 absent", "STALE-2" not in tids, f"unexpected STALE-2 in {tids}")
    chk("stale: exactly 1 live", len(lives) == 1, f"expected 1, got {len(lives)}: {tids}")


def test_stale_newest_excluded_no_ts0_exception():
    """The SOLE run is the newest scoped task AND its last audit is >150 s old → it drops
    out of the live set. There is no always-include-ts[0] exception (EU-477 AC; resolves the
    iteration-1 review point): a run silent for >150 s reads as crashed/interrupted, and
    active_run renders it as an idle 'last run' via its fallback instead."""
    cfg = _make_cfg([
        dict(event="ticket_start", ticket_id="STALE-1", app="testapp", branch="b", ts=_ts(400)),
        dict(event="build",       ticket_id="STALE-1", app="testapp", ts=_ts(390)),
    ])
    tasks = D.load_tasks(cfg.audit_path)
    lives = warroom.live_runs(cfg, tasks, "testapp", active=True)
    chk("stale-newest: live_runs is empty (no ts[0] exception)", lives == [], f"got {lives}")
    run = warroom.active_run(cfg, tasks, "testapp", active=True)
    chk("stale-newest: active_run falls back to the last run",
        run is not None and run.get("ticket") == "STALE-1", f"got {run}")
    chk("stale-newest: fallback renders as idle 'last run' (live=False)",
        run is not None and run.get("live") is False, f"got {run}")


def test_stale_newest_fresh_older_generalisation():
    """ts[0] is stale but an OLDER run still has fresh activity → the live set is the fresh
    older run (the ts[0]-only collapse generalised to the full live subset), and active_run
    shows THAT run, not the stale newest one."""
    cfg = _make_cfg([
        # FRESH-OLD started earlier but is still building (activity 10 s ago) …
        dict(event="ticket_start", ticket_id="FRESH-OLD", app="testapp", branch="b", ts=_ts(300)),
        dict(event="build",       ticket_id="FRESH-OLD", app="testapp", ts=_ts(10)),
        # … STALE-NEW started later (so it is ts[0]) but went silent >150 s ago.
        dict(event="ticket_start", ticket_id="STALE-NEW", app="testapp", branch="c", ts=_ts(200)),
    ])
    tasks = D.load_tasks(cfg.audit_path)
    # Sanity: load_tasks orders newest-start first, so STALE-NEW is ts[0].
    chk("generalisation: STALE-NEW is ts[0]",
        tasks and tasks[0].get("ticket_id") == "STALE-NEW",
        f"got {[t.get('ticket_id') for t in tasks]}")
    lives = warroom.live_runs(cfg, tasks, "testapp", active=True)
    chk("generalisation: only the fresh older run is live",
        [t.get("ticket") for t in lives] == ["FRESH-OLD"],
        f"got {[t.get('ticket') for t in lives]}")
    run = warroom.active_run(cfg, tasks, "testapp", active=True)
    chk("generalisation: active_run shows the live run, not stale ts[0]",
        run is not None and run.get("ticket") == "FRESH-OLD" and run.get("live") is True,
        f"got {run}")


# ===========================================================================
# AC 5: Cap at max_concurrent_builders, newest-first
# ===========================================================================

def test_cap_newest_first():
    """Three live runs with max_concurrent_builders=2 → exactly 2, newest-first; the oldest
    row cannot flood the caller."""
    cfg = _make_cfg([
        dict(event="ticket_start", ticket_id="CAP-3", app="testapp", branch="b", ts=_ts(90)),
        dict(event="ticket_start", ticket_id="CAP-2", app="testapp", branch="c", ts=_ts(60)),
        dict(event="ticket_start", ticket_id="CAP-1", app="testapp", branch="d", ts=_ts(10)),
    ], max_builders=2)
    tasks = D.load_tasks(cfg.audit_path)
    lives = warroom.live_runs(cfg, tasks, "testapp", active=True)
    tids = [t.get("ticket") for t in lives]
    chk("cap: list capped at max_concurrent_builders=2", len(lives) == 2, f"got {tids}")
    chk("cap: newest-first under the cap", tids == ["CAP-1", "CAP-2"], f"got {tids}")
    chk("cap: oldest excluded", "CAP-3" not in tids, f"got {tids}")


# ===========================================================================
# AC 6: active=False → idle → []
# ===========================================================================

def test_active_false_returns_empty():
    """active=False → empty list regardless of freshness (idle boards render the last run
    via active_run's fallback, never via live_runs)."""
    cfg = _make_cfg([
        dict(event="ticket_start", ticket_id="NOW", app="testapp", branch="b", ts=_ts(5)),
    ])
    tasks = D.load_tasks(cfg.audit_path)
    lives = warroom.live_runs(cfg, tasks, "testapp", active=False)
    chk("active=False: empty list", lives == [], f"got {len(lives)} items")


# ===========================================================================
# AC 7: active_run() IS live_runs()[0] — the wrapper relationship, enforced
# ===========================================================================

def test_wrapper_single_live_run():
    """Single live run: active_run() returns exactly the dict live_runs()[0] produces."""
    cfg = _make_cfg([
        dict(event="ticket_start", ticket_id="WRAP-1", app="testapp", branch="b", ts=_ts(30)),
        dict(event="build", ticket_id="WRAP-1", app="testapp", iteration=1,
             tools=["Edit"], summary="built", ts=_ts(10)),
    ])
    tasks = D.load_tasks(cfg.audit_path)
    lives = warroom.live_runs(cfg, tasks, "testapp", active=True)
    run = warroom.active_run(cfg, tasks, "testapp", active=True)
    chk("wrapper(single-live): live_runs has exactly 1", len(lives) == 1, f"got {lives}")
    chk("wrapper(single-live): active_run() == live_runs()[0]",
        lives and run == lives[0], f"active_run={run} vs live_runs[0]={lives[0] if lives else None}")
    chk("wrapper(single-live): live=True", run is not None and run.get("live") is True,
        f"got {run}")


def test_wrapper_eu448_phase_derivation():
    """EU-448 fixture (gate event fired): active_run() == live_runs()[0], and the live-phase
    derivation (reached == Gate) comes out of the shared run-object builder."""
    cfg = _make_cfg([
        dict(event="ticket_start", ticket_id="EU-448X", app="testapp", branch="b", ts=_ts(60)),
        dict(event="build", ticket_id="EU-448X", app="testapp", iteration=1,
             tools=["Edit"], summary="built", ts=_ts(50)),
        dict(event="gate", ticket_id="EU-448X", app="testapp", iteration=1,
             passed=True, ts=_ts(10)),
    ])
    tasks = D.load_tasks(cfg.audit_path)
    lives = warroom.live_runs(cfg, tasks, "testapp", active=True)
    run = warroom.active_run(cfg, tasks, "testapp", active=True)
    chk("wrapper(EU-448): active_run() == live_runs()[0]",
        lives and run == lives[0], f"active_run={run} vs live_runs[0]={lives[0] if lives else None}")
    chk("wrapper(EU-448): reached == Gate index",
        run is not None and run.get("reached") == PH.index("Gate"),
        f"got reached={run.get('reached') if run else None}")


def test_wrapper_eu130_triage_synthesis():
    """EU-130 fixture (old merged run + current triage of a different ticket): the synthetic
    triage run pre-empts the live subset, and active_run() == live_runs()[0] — the branch
    moved into live_runs unchanged."""
    cfg = _make_cfg([
        dict(event="ticket_start", ticket_id="OLD-1", app="testapp", branch="b", ts=_ts(3600)),
        dict(event="build", ticket_id="OLD-1", app="testapp", iteration=1,
             tools=["Edit"], summary="built", ts=_ts(3590)),
        dict(event="merged", ticket_id="OLD-1", app="testapp", ts=_ts(3580)),
        dict(event="prebuild_triage", ticket_id="NEW-2", verdict="CLOSE", ts=_ts(30)),
        dict(event="prebuild_close", ticket_id="NEW-2", reason="invalid", ts=_ts(25)),
    ])
    tasks = D.load_tasks(cfg.audit_path)
    lives = warroom.live_runs(cfg, tasks, "testapp", active=True)
    run = warroom.active_run(cfg, tasks, "testapp", active=True)
    chk("wrapper(EU-130): live_runs is the single synthetic triage run",
        len(lives) == 1 and lives[0].get("triage_phase") == "closed",
        f"got {lives}")
    chk("wrapper(EU-130): active_run() == live_runs()[0]",
        lives and run == lives[0], f"active_run={run} vs live_runs[0]={lives[0] if lives else None}")
    chk("wrapper(EU-130): shows the triage ticket, not the stale run",
        run is not None and run.get("ticket") == "NEW-2" and run.get("live") is True,
        f"got {run}")
    chk("wrapper(EU-130): triage verdict/reason carried",
        run is not None and run.get("triage_verdict") == "CLOSE"
        and run.get("triage_reason") == "invalid", f"got {run}")


def test_wrapper_idle_last_run_fallback():
    """Idle / last-run case: live_runs() is empty and active_run() falls back to the newest
    scoped run rendered with live=False — the documented '(or equivalent)' wrapper contract
    that keeps the cockpit's last-run panel (and the EU-76/phase-fail suites) intact."""
    cfg = _make_cfg([
        dict(event="ticket_start", ticket_id="DONE-9", app="testapp", branch="b", ts=_ts(7200)),
        dict(event="build", ticket_id="DONE-9", app="testapp", iteration=1,
             tools=["Edit"], summary="built", ts=_ts(7190)),
        dict(event="review", ticket_id="DONE-9", iteration=1, verdict="PASS",
             summary="ok", ts=_ts(7180)),
        dict(event="merged", ticket_id="DONE-9", app="testapp", ts=_ts(7170)),
    ])
    tasks = D.load_tasks(cfg.audit_path)
    lives = warroom.live_runs(cfg, tasks, "testapp", active=False)
    run = warroom.active_run(cfg, tasks, "testapp", active=False)
    chk("wrapper(idle): live_runs(active=False) == []", lives == [], f"got {lives}")
    chk("wrapper(idle): active_run still shows the last run",
        run is not None and run.get("ticket") == "DONE-9", f"got {run}")
    chk("wrapper(idle): rendered as idle (live=False, structural reached)",
        run is not None and run.get("live") is False and run.get("reached") == len(PH),
        f"got {run}")


# ===========================================================================
# Report
# ===========================================================================

if __name__ == "__main__":
    # Explicitly call each test function to keep ordering deterministic.
    test_two_fresh_both_returned()
    test_single_live_returns_one()
    test_terminal_excluded()
    test_stale_non_newest_excluded()
    test_stale_newest_excluded_no_ts0_exception()
    test_stale_newest_fresh_older_generalisation()
    test_cap_newest_first()
    test_active_false_returns_empty()
    test_wrapper_single_live_run()
    test_wrapper_eu448_phase_derivation()
    test_wrapper_eu130_triage_synthesis()
    test_wrapper_idle_last_run_fallback()

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n{'=' * 56}")
    print(f"  EU-477 live_runs / active_run wrapper tests")
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
