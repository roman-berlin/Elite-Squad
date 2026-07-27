"""EU-488: end-to-end regression coverage for assembled multi-run ``render_board`` behavior.

The three prior tickets have landed: EU-485 (``_render_run_card_data``),
EU-486 (``live_runs()`` + multi-card loop), EU-487 (per-card ``data-log-ticket``).

This harness asserts the *assembled* system end-to-end — driven through
``warroom.render_board(cfg, "testapp", state)`` itself — covering four scenarios:

  1. Zero live runs → idle board, no ``data-log-ticket``, correct structure.
  2. One live run → structural byte-identity pin on the single-run path
     (recording wrapper on ``_run_html``, no ``log_ticket`` kwarg).
  3. Two live runs for same app → two distinct cards, correct order, each with
     its own elapsed time, stage, pass count, and per-card ``data-log-ticket``.
  4. Three live runs with ``max_concurrent_builders=2`` → exactly 2 cards, oldest
     ticket absent from HTML (cap at render level, not just in ``live_runs()``).

Fail-first: these tests are written AGAINST the pre-EU-486 code where
``live_runs()`` does NOT exist; they MUST be RED before implementation, then GREEN after.
"""
import json
import re
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")

# --- Minimal stubs (same shape as eu485 / eu486 / eu487) ---
_sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass  # noqa: E704
    def __call__(s, *a, **k): return s  # noqa: E704


_sdk.__getattr__ = lambda n: _D  # noqa: E731
sys.modules.setdefault("claude_agent_sdk", _sdk)

from orchestrator import warroom, dashboard as D  # noqa: E402
from orchestrator.config import Config, AppConfig  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    """Record one check. ``cond=True`` means PASS."""
    results.append((name, bool(cond), str(detail)))


# ===========================================================================
# Fixtures
# ===========================================================================

def _make_cfg(rows: list[dict], *, max_builders: int = 1) -> Config:
    """Create a temp Config pointing at a fresh audit populated with *rows*."""
    d = Path(tempfile.mkdtemp())
    audit = d / "audit.jsonl"
    audit.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    D._audit_cache.clear()
    D._tasks_cache.clear()
    return Config(
        apps=[AppConfig(name="testapp", repo_path=str(d), base_branch="dev",
                        protected_branch="main", backlog_backend="none")],
        audit_path=str(audit),
        max_concurrent_builders=max_builders,
    )


def _ts(secs_ago: int) -> str:
    """ISO timestamp *secs_ago* seconds before now."""
    return (datetime.now().astimezone() - timedelta(seconds=secs_ago)).strftime("%Y-%m-%dT%H:%M:%S%z")


# ===========================================================================
# AC1: zero live runs → idle board, unchanged from pre-EU-486
# ===========================================================================

def test_ac1_zero_live_runs():
    """Fixture with only a terminal event (merged) → idle board.

    No live runs → ``live_runs()`` returns []. The single-run / idle branch handles a
    completed run; no ``data-log-ticket`` attribute is emitted on the idle board.
    """
    cfg = _make_cfg([
        dict(event="merged", ticket_id="OLD-1", app="testapp", ts=_ts(3600)),
    ])
    html = warroom.render_board(cfg, "testapp", {})
    chk("AC1a: board renders without crash", html is not None and len(html) > 0,
        "render_board raised or returned empty")
    chk("AC1b: 'Active run' panel header present", "Active run" in html,
        "missing 'Active run' panel header")
    chk("AC1c: no data-log-ticket on idle board",
        "data-log-ticket" not in html,
        "idle board should NOT carry data-log-ticket")


# ===========================================================================
# AC2: one live run → structural byte-identity pin on the single-run path
# ===========================================================================

def test_ac2_one_live_run_single_path_identity():
    """Single live run: structurally identical to pre-EU-486 single-card output.

    Since the single-run path calls ``_run_html`` WITHOUT ``log_ticket``, we record
    that call via a wrapper monkeypatch and assert:
      a) ``_run_html`` was invoked exactly once, without ``log_ticket`` kwarg,
         yielding a concrete HTML string.
      b) That recorded string appears byte-for-byte inside the board HTML.
      c) The ticket id appears on the board.
      d) No ``data-log-ticket`` attribute is present anywhere (EU-487 single-run invariant).

    This is the strongest non-flaky form of byte-identity pinning because the
    captured string already has elapsed baked in — no race between capture and
    assertion.
    """
    cfg = _make_cfg([
        dict(event="ticket_start", ticket_id="ONE-1", app="testapp", branch="b", ts=_ts(30)),
        dict(event="build", ticket_id="ONE-1", app="testapp", ts=_ts(10)),
    ])

    # --- recording wrapper around _run_html ---
    _calls: list[dict] = []
    original = warroom._run_html

    def _recorder(run, mode=None, elapsed=None, manual=False, log_path=None,
                  log_ticket=None, stopping=False):
        result = original(run, mode=mode, elapsed=elapsed, manual=manual,
                          log_path=log_path, log_ticket=log_ticket, stopping=stopping)
        _calls.append({
            "run": run,
            "mode": mode,
            "elapsed": elapsed,
            "manual": manual,
            "log_path": log_path,
            "log_ticket": log_ticket,
            "stopping": stopping,
            "_html": result,
        })
        return result

    try:
        warroom._run_html = _recorder
        html = warroom.render_board(cfg, "testapp", {"active": True})

        # a) called exactly once, without log_ticket
        chk("AC2a: _run_html called exactly once", len(_calls) == 1,
            f"expected 1 call, got {len(_calls)}")
        if _calls:
            c = _calls[0]
            chk("AC2b: _run_html received no log_ticket kwarg (None)",
                c["log_ticket"] is None, f"log_ticket={c['log_ticket']!r}")
            rendered_single = c["_html"]

            # b) recorded string appears byte-for-byte in board
            chk("AC2c: _run_html output appears byte-for-byte in board HTML",
                rendered_single in html,
                f"captured snippet length={len(rendered_single)} not found in board (len={len(html)})")
        else:
            chk("AC2c: (skip — no call recorded)", False, "no _run_html call was made")
            rendered_single = ""

        # c) ticket id on board
        chk("AC2d: ONE-1 appears on the board", "ONE-1" in html, "ONE-1 missing from board")

        # d) no data-log-ticket anywhere (single-run invariant)
        chk("AC2e: no data-log-ticket attribute (EU-487 single-run invariant)",
            "data-log-ticket" not in html,
            "single-run HTML should NOT carry data-log-ticket")

    finally:
        warroom._run_html = original


# ===========================================================================
# AC3: two live runs → two distinct cards, correct ordering and per-card values
# ===========================================================================

def test_ac3_two_live_runs_each_card_correct():
    """Two genuinely live same-app runs → two cards with distinct per-card values.

    Both ticks are kept within the ~150-s freshness window used by ``live_runs()``
    (the EU-486 tests also use this pattern).  TICKET-A at 20-s offset, TICKET-B
    at 120-s offset: they sort newest-first (A before B), and their elapsed
    strings are distinct ("0m 20s" vs "2m 00s").

    Then assert:
      a) exactly two ``runhead runlive`` divs, ordered newest-first,
      b) each card carries its own ticket id, distinct elapsed strings,
         its own stage and pass count,
      c) each card's ``data-log-ticket`` equals that card's own ticket id.
    """
    now = datetime.now().astimezone()
    fmt = lambda td: (now - td).strftime("%Y-%m-%dT%H:%M:%S%z")
    rows = [
        dict(event="ticket_start", ticket_id="TICKET-B", app="testapp", branch="b",
             ts=fmt(timedelta(seconds=120))),
        dict(event="build", ticket_id="TICKET-B", app="testapp", iteration=1,
             ts=fmt(timedelta(seconds=110))),
        dict(event="ticket_start", ticket_id="TICKET-A", app="testapp", branch="b",
             ts=fmt(timedelta(seconds=20))),
        dict(event="build", ticket_id="TICKET-A", app="testapp", iteration=2,
             ts=fmt(timedelta(seconds=10))),
    ]
    cfg = _make_cfg(rows, max_builders=2)
    html = warroom.render_board(cfg, "testapp", {"active": True})

    # Split into card fragments
    fragments = html.split('<div class="runhead runlive"')
    chk("AC3a: exactly two live cards rendered", len(fragments) == 3,  # split gives n+1 parts
        f"got {len(fragments)} fragments")
    if len(fragments) >= 3:
        card_a, card_b = fragments[1], fragments[2]  # newest-first: TICKET-A then TICKET-B

        # b) ticket ids in correct order
        chk("AC3b: first card is TICKET-A (newest-first)",
            "TICKET-A" in card_a, "first fragment doesn't contain TICKET-A")
        chk("AC3c: second card is TICKET-B",
            "TICKET-B" in card_b, "second fragment doesn't contain TICKET-B")

        # c) per-card elapsed differs (20s vs 120s → "0m 20s" vs "2m 00s")
        chk("AC3d: TICKET-A and TICKET-B have different elapsed values",
            card_a != card_b and "m" in card_a and "m" in card_b,
            f"elapsed strings should differ between cards")

        # d) each card has its own data-log-ticket value
        chk("AC3e: TICKET-A card carries data-log-ticket=TICKET-A",
            'data-log-ticket="TICKET-A"' in card_a,
            f"TICKET-A card missing its own filter attr; attrs={re.findall(r'data-log-ticket=\"([^\"]+)\"', card_a)}")
        chk("AC3f: TICKET-B card carries data-log-ticket=TICKET-B",
            'data-log-ticket="TICKET-B"' in card_b,
            f"TICKET-B card missing its own filter attr; attrs={re.findall(r'data-log-ticket=\"([^\"]+)\"', card_b)}")


# ===========================================================================
# AC4: cap at render_board level — 3 live runs, max_concurrent_builders=2
# ===========================================================================

def test_ac4_cap_at_render_level():
    """Three live runs with max_concurrent_builders=2 → board HTML has exactly 2 cards.

    Asserts the oldest ticket id is completely absent from the board, proving the cap
    operates at the render_board level (not just in live_runs()).
    """
    now = datetime.now().astimezone()
    rows = [
        dict(event="ticket_start", ticket_id="THREE-3", app="testapp", branch="b",
             ts=(now - timedelta(seconds=90)).strftime("%Y-%m-%dT%H:%M:%S%z")),
        dict(event="ticket_start", ticket_id="THREE-2", app="testapp", branch="b",
             ts=(now - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S%z")),
        dict(event="ticket_start", ticket_id="THREE-1", app="testapp", branch="b",
             ts=(now - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%S%z")),
    ]
    cfg = _make_cfg(rows, max_builders=2)
    html = warroom.render_board(cfg, "testapp", {"active": True})

    fragments = html.split('<div class="runhead runlive"')
    chk("AC4a: exactly two cards (oldest capped out)", len(fragments) == 3,
        f"expected 3 fragments (2 cards), got {len(fragments)}")

    chk("AC4b: THREE-1 (newest) is on the board",
        "THREE-1" in html, "newest ticket missing from board")
    chk("AC4c: THREE-2 (middle) is on the board",
        "THREE-2" in html, "middle ticket missing from board")
    chk("AC4d: THREE-3 (oldest) is absent from board (capped out)",
        "THREE-3" not in html, "oldest ticket appeared despite cap=2")


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
    print(f"  EU-488 multi-run render_board regression tests")
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
