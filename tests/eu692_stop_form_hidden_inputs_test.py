"""EU-692: stop form hidden ``app`` and ``ticket`` inputs on every rendered run card.

Three checks:
  1. A single live+manual run card renders the stop form with correct hidden inputs.
  2. Two concurrent live cards each have distinct app/ticket pairs in their stop forms.
  3. Non-live runs (idle) or non-manual runs never emit the stop form at all.
"""
import json
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")

# ---- Minimal stubs ----------------------------------------------------------
_sdk = type("claude_agent_sdk", (), {
    "__getattr__": lambda n: type("_D", (), {
        "__init__": lambda s, *a, **k: None,
        "__call__": lambda s, *a, **k: s,
    })()
})()
sys.modules.setdefault("claude_agent_sdk", _sdk)

from orchestrator import warroom, dashboard as D
from orchestrator.config import Config, AppConfig

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


# ===========================================================================
# Helpers
# ===========================================================================

def _make_cfg(rows: list[dict]) -> Config:
    d = Path(tempfile.mkdtemp())
    audit = d / "audit.jsonl"
    audit.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return Config(
        apps=[AppConfig(name="testapp", repo_path=str(d), base_branch="dev",
                        protected_branch="main", backlog_backend="none")],
        audit_path=str(audit),
    )


def _fake_live_run(ticket: str = "EU-100", app: str = "testapp") -> dict:
    """Build a minimal live run object that _run_html accepts."""
    now = datetime.now().astimezone()
    started_dt = now - timedelta(seconds=60)
    return {
        "live": True,
        "ticket": ticket,
        "app": app,
        "branch": "eu692-test",
        "passes": 1,
        "verdict": "",
        "outcome": "running",
        "cost": 0,
        "phases": ["Build", "Gate", "Review", "Land"],
        "reached": 0,
        "failed_phase": None,
        "sparkline": [],
        "started": started_dt.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def _render(run: dict, *, manual: bool = True) -> str:
    """Call _run_html with a live run and return the HTML string."""
    elapsed = "1m 00s"
    return warroom._run_html(run, mode="live", elapsed=elapsed, manual=manual)


# ===========================================================================
# AC1: single live+manual card → stop form has hidden app & ticket
# ===========================================================================

def test_ac1_single_card_has_hidden_inputs():
    """A single live+manual run card renders hidden app/ticket matching its own run."""
    run = _fake_live_run(ticket="EU-420", app="myapp")
    html = _render(run)

    # Stop button must exist (live=True, manual=True)
    chk("AC1a: stop button present", "class=stopbtn" in html,
        "no stop button — form wasn't rendered")
    chk("AC1b: stop form action correct", "/api/stop-run" in html,
        "wrong form action")

    # Hidden inputs match this card's run
    app_match = re.search(r'<input[^>]*name="app"[^>]*value="([^"]*)"', html)
    ticket_match = re.search(r'<input[^>]*name="ticket"[^>]*value="([^"]*)"', html)

    chk("AC1c: hidden app input present", app_match is not None,
        "no <input name=\"app\"> found in stop form")
    if app_match:
        chk("AC1d: hidden app value matches card's run",
            app_match.group(1) == "myapp",
            f"got {app_match.group(1)!r}, expected 'myapp'")

    chk("AC1e: hidden ticket input present", ticket_match is not None,
        "no <input name=\"ticket\"> found in stop form")
    if ticket_match:
        chk("AC1f: hidden ticket value matches card's run",
            ticket_match.group(1) == "EU-420",
            f"got {ticket_match.group(1)!r}, expected 'EU-420'")


# ===========================================================================
# AC2: two concurrent cards → distinct app/ticket pairs
# ===========================================================================

def test_ac2_two_cards_distinct_pairs():
    """Directly rendering two cards proves distinct app/ticket pairs end up in their own stop forms.

    We call _run_html twice with different live+manual runs and concatenate.
    Each card's form carries ONLY its own values.
    """
    run_a = _fake_live_run(ticket="EU-101", app="testapp")
    html_a = _render(run_a)

    run_b = _fake_live_run(ticket="EU-202", app="another-app")
    html_b = _render(run_b)

    combined = html_a + html_b

    # Card A
    tickets_a = re.findall(r'name="ticket"[^>]*value="([^"]*)"', html_a)
    apps_a = re.findall(r'name="app"[^>]*value="([^"]*)"', html_a)
    chk("AC2a: EU-101 card carries ticket=EU-101",
        "EU-101" in tickets_a,
        f"EU-101 fragment tickets={tickets_a}")
    chk("AC2b: EU-101 card carries app=testapp",
        "testapp" in apps_a,
        f"apps in A card: {apps_a}")

    # Card B
    tickets_b = re.findall(r'name="ticket"[^>]*value="([^"]*)"', html_b)
    apps_b = re.findall(r'name="app"[^>]*value="([^"]*)"', html_b)
    chk("AC2c: EU-202 card carries ticket=EU-202",
        "EU-202" in tickets_b,
        f"EU-202 fragment tickets={tickets_b}")
    chk("AC2d: EU-202 card carries app=another-app",
        "another-app" in apps_b,
        f"apps in B card: {apps_b}")

    # Cross-contamination checks — no value from B appears in A's form and vice versa
    chk("AC2e: no cross-contamination (102 absent from card A)",
        "EU-202" not in tickets_a,
        f"A card leaked B's ticket: {tickets_a}")
    chk("AC2f: no cross-contamination (101 absent from card B)",
        "EU-101" not in tickets_b,
        f"B card leaked A's ticket: {tickets_b}")
    chk("AC2g: no cross-contamination (another-app absent from card A)",
        "another-app" not in apps_a,
        f"A card leaked B's app: {apps_a}")
    chk("AC2h: no cross-contamination (testapp absent from card B)",
        "testapp" not in apps_b,
        f"B card leaked A's app: {apps_b}")


# ===========================================================================
# AC3: non-live or non-manual → NO stop form (backward-compatible)
# ===========================================================================

def test_ac3_no_stop_without_live_or_manual():
    """Non-live runs and non-manual runs do NOT emit the stop form."""
    live_run = _fake_live_run()

    # live=True, manual=False → autopilot path: no stop
    html_auto = _render(live_run, manual=False)
    chk("AC3a: no stop form when manual=False", "class=stopbtn" not in html_auto,
        "autopilot run should not have stop button")
    chk("AC3b: no stop form when manual=False", "/api/stop-run" not in html_auto,
        "form shouldn't appear at all")

    # Build a dead/idle run object
    idle_run = dict(_fake_live_run(), live=False)
    html_idle = warroom._run_html(idle_run, mode=None, elapsed=None, manual=False)
    chk("AC3c: no stop form when live=False (idle)", "class=stopbtn" not in html_idle,
        "idle run should not have stop button")


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
    print(f"  EU-692 stop form hidden inputs tests")
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
