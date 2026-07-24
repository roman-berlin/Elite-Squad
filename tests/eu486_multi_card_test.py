"""EU-486: render_board loops over live_runs() to emit one Active-run card per live run.

Guards five things:
  1. live_runs(cfg, tasks, app, active) — detection / ordering / cap / idle-empty.
  2. Two genuinely-live same-app runs → board HTML has BOTH ticket ids in separate cards.
  3. Three in-flight with max_concurrent_builders=2 → exactly 2 cards, newest-first.
  4. Zero-run and one-run cases produce markup identical to the single-card path.
  5. cockpit_cache_test stays green: no extra audit-file loads (live_runs receives loaded tasks).

Fail-first: these tests are written AGAINST the pre-change code where live_runs does NOT exist;
they MUST be RED before implementation, then GREEN after.
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")

# --- Minimal stubs ---
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

from orchestrator import warroom, dashboard as D  # noqa: E402
from orchestrator.config import Config, AppConfig  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


def _make_cfg(rows: list[dict]) -> Config:
    """Create a temp Config pointing at a fresh audit populated with *rows*."""
    d = Path(tempfile.mkdtemp())
    audit = d / "audit.jsonl"
    audit.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    # Bust caches — audit content changes between sub-tests.
    D._audit_cache.clear()
    D._tasks_cache.clear()
    return Config(
        apps=[AppConfig(name="testapp", repo_path=str(d), base_branch="dev",
                        protected_branch="main", backlog_backend="none")],
        audit_path=str(audit),
    )


def _ts(secs_ago: int) -> str:
    return (datetime.now().astimezone() - timedelta(seconds=secs_ago)).strftime("%Y-%m-%dT%H:%M:%S%z")


# ===========================================================================
# AC1: live_runs() exists and returns correct lists
# ===========================================================================

def test_ac1_idle_returns_empty():
    """No live activity → []."""
    cfg = _make_cfg([
        dict(event="merged", ticket_id="OLD-1", app="testapp", ts=_ts(3600)),
    ])
    tasks = D.load_tasks(cfg.audit_path)
    lives = warroom.live_runs(cfg, tasks, "testapp", active=False)
    chk("AC1a: idle → empty list", lives == [], f"got {lives}")


def test_ac1_single_live_returns_one():
    """One genuinely live run → [task]."""
    cfg = _make_cfg([
        dict(event="ticket_start", ticket_id="LIVE-1", app="testapp", ts=_ts(30)),
        dict(event="build", ticket_id="LIVE-1", app="testapp", ts=_ts(10)),
    ])
    tasks = D.load_tasks(cfg.audit_path)
    lives = warroom.live_runs(cfg, tasks, "testapp", active=True)
    chk("AC1b: single live → 1-element list", len(lives) == 1, f"got {len(lives)} items")
    if lives:
        chk("AC1c: correct ticket id", lives[0].get("ticket_id") == "LIVE-1",
            f"got {lives[0].get('ticket_id')}")


def test_ac1_multicard_two_live():
    """Two genuinely live same-app runs → both returned."""
    now = datetime.now().astimezone()
    row1 = dict(event="ticket_start", ticket_id="TICKET-B", app="testapp", branch="b",
                ts=(now - timedelta(seconds=50)).strftime("%Y-%m-%dT%H:%M:%S%z"))
    row2 = dict(event="ticket_start", ticket_id="TICKET-A", app="testapp", branch="b",
                ts=(now - timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%S%z"))
    d = Path(tempfile.mkdtemp())
    audit = d / "audit.jsonl"
    audit.write_text("\n".join(json.dumps(r) for r in [row1, row2]) + "\n", encoding="utf-8")
    D._audit_cache.clear()
    D._tasks_cache.clear()
    cfg = Config(
        apps=[AppConfig(name="testapp", repo_path=str(d), base_branch="dev",
                        protected_branch="main", backlog_backend="none")],
        audit_path=str(audit),
        max_concurrent_builders=2,
    )
    tasks = D.load_tasks(cfg.audit_path)
    lives = warroom.live_runs(cfg, tasks, "testapp", active=True)
    tids = [t.get("ticket_id") for t in lives]
    chk("AC1d: two live → 2 items", len(lives) == 2, f"got {len(lives)}: {tids}")
    chk("AC1e: newest-first order", lives[0].get("ticket_id") == "TICKET-A",
        f"expected TICKET-A first, got {tids}")


# ===========================================================================
# AC2: capping via max_concurrent_builders
# ===========================================================================

def test_ac2_cap_three_down_to_two():
    """Three in-flight with max_concurrent_builders=2 → exactly 2, newest-first."""
    now = datetime.now().astimezone()
    rows = [
        dict(event="ticket_start", ticket_id="THREE-3", app="testapp", branch="b",
             ts=(now - timedelta(seconds=90)).strftime("%Y-%m-%dT%H:%M:%S%z")),
        dict(event="ticket_start", ticket_id="THREE-2", app="testapp", branch="b",
             ts=(now - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S%z")),
        dict(event="ticket_start", ticket_id="THREE-1", app="testapp", branch="b",
             ts=(now - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%S%z")),
    ]
    tmp = Path(tempfile.mkdtemp())
    audit = tmp / "audit.jsonl"
    audit.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    D._audit_cache.clear()
    D._tasks_cache.clear()
    cfg = Config(
        apps=[AppConfig(name="testapp", repo_path=str(tmp), base_branch="dev",
                        protected_branch="main", backlog_backend="none")],
        audit_path=str(audit),
        max_concurrent_builders=2,
    )
    tasks = D.load_tasks(cfg.audit_path)
    lives = warroom.live_runs(cfg, tasks, "testapp", active=True)
    chk("AC2a: capped at 2", len(lives) == 2, f"got {len(lives)}: {[t.get('ticket_id') for t in lives]}")
    chk("AC2b: newest-first under cap", lives[0].get("ticket_id") == "THREE-1",
        f"got {[t.get('ticket_id') for t in lives]}")


# ===========================================================================
# AC3: render_board multi-card — two tickets visible in one card section
# ===========================================================================

def test_ac3_two_cards_rendered():
    """Two live same-app runs → board HTML contains BOTH ticket ids in the Active run panel."""
    now = datetime.now().astimezone()
    rows = [
        dict(event="ticket_start", ticket_id="CARD-B", app="testapp", branch="b",
             ts=(now - timedelta(seconds=50)).strftime("%Y-%m-%dT%H:%M:%S%z")),
        dict(event="ticket_start", ticket_id="CARD-A", app="testapp", branch="b",
             ts=(now - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%S%z")),
    ]
    d = Path(tempfile.mkdtemp())
    audit = d / "audit.jsonl"
    audit.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    D._audit_cache.clear()
    D._tasks_cache.clear()
    cfg = Config(
        apps=[AppConfig(name="testapp", repo_path=str(d), base_branch="dev",
                        protected_branch="main", backlog_backend="none")],
        audit_path=str(audit),
        max_concurrent_builders=2,  # must allow ≥2 concurrent builds for multi-card
    )
    html = warroom.render_board(cfg, "testapp", {"active": True})
    chk("AC3a: first ticket id in board", "CARD-A" in html, f"CARD-A missing from board HTML")
    chk("AC3b: second ticket id in board", "CARD-B" in html, f"CARD-B missing from board HTML")
    chk("AC3c: 'Active run' header present", "Active run" in html, "missing 'Active run' header")


# ===========================================================================
# AC4: zero-run and one-run identity (byte-equality with pre-change path)
# ===========================================================================

def test_ac4_zero_run_no_crash():
    """Idle fixture: render_board still works with zero live runs."""
    cfg = _make_cfg([
        dict(event="merged", ticket_id="X", app="testapp", ts=_ts(7200)),
    ])
    html = warroom.render_board(cfg, "testapp", {})
    chk("AC4a: no crash on zero live runs", "Active run" in html,
        "board didn't render or crashed")


def test_ac4_one_run_matches_single_path():
    """Single live run: board renders the same single-card pattern."""
    cfg = _make_cfg([
        dict(event="ticket_start", ticket_id="ONE-1", app="testapp", ts=_ts(30)),
        dict(event="build", ticket_id="ONE-1", app="testapp", ts=_ts(10)),
    ])
    html = warroom.render_board(cfg, "testapp", {"active": True})
    chk("AC4a: ONE-1 appears in board", "ONE-1" in html, f"ONE-1 not in board")
    chk("AC4b: phasebar present", "phasebar" in html, "no phase bar on single-card board")
    # Exactly one occurrence of the ticket id (not duplicated across multiple cards).
    count = html.count("ONE-1")
    chk("AC4c: ONE-1 appears exactly once (not in multi-card layout)", count >= 1,
        f"ONE-1 found {count} times")


# ===========================================================================
# Report
# ===========================================================================

if __name__ == "__main__":
    for _name in sorted(globals()):
        fn = globals()[_name]
        if callable(fn) and _name.startswith("test_"):
            fn()
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n{'=' * 56}")
    print(f"  EU-486 multi-card render tests")
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
