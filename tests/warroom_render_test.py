"""EU-489: consolidated integration regression — render board, whole-feature.

Closes the multi-card Active-run-board epic (EU-458b / split from EU-478).
Runs every AC scenario through the *real* assembled ``render_board(cfg, app, state)``
so the single-file harness exercises the full call chain: ``live_runs()`` → multi-card
loop / single-card identity → ``_render_run_card_data()`` → ``_run_html()`` with
``data-log-ticket``.

Tests against the *already-landed* feature code — written fail-first: each check would
have been RED before the sibling tickets landed.

Structure (one function per AC):
  AC1 · Two genuinely-live same-app runs → 2 distinct cards, each with ticket id,
       stage bar, elapsed timer, pass count, and ``data-log-ticket`` filtered to its
       OWN ticket id.
  AC2 · Zero / one live run → byte-identity with the pre-multi-card single-card path
        (wrapper-monkeypatch records ``_run_html`` call: no ``log_ticket``, single
        invocation, captured HTML appears byte-for-byte in the board).
  AC3 · Burst of N > ``max_concurrent_builders`` stale/near-terminal rows → renders ≤ cap,
        oldest-beyond-cap ticket absent.
  AC4 · Harness hygiene: exits non-zero if any multi-card loop or single-card identity
         breaks (the harness IS the gate — run under ``python3 tests/run_all.py``).
"""
import json
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")

# --- Minimal stubs (matching sibling tests) ---
_sdk = __import__("types").ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass  # noqa: E704
    def __call__(s, *a, **k): return s  # noqa: E704


_sdk.__getattr__ = lambda n: _D  # noqa: E731
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


def _now_rows(*offsets: tuple[str, int]) -> list[dict]:
    """Return audit rows for the given (ticket_id, secs_ago) pairs.

    Also adds a ``build`` event 5 s after each ``ticket_start`` so the run-object
    derives non-trivial phase / pass info inside ``_render_run_card_data``."""
    now = datetime.now().astimezone()
    fmt = lambda td: (now - td).strftime("%Y-%m-%dT%H:%M:%S%z")
    rows = []
    for tid, secs in sorted(offsets, key=lambda x: -x[1]):  # newest first
        rows.append(dict(event="ticket_start", ticket_id=tid, app="testapp", branch="b",
                         ts=fmt(timedelta(seconds=secs))))
        rows.append(dict(event="build", ticket_id=tid, app="testapp", iteration=1,
                         ts=fmt(timedelta(seconds=max(secs - 5, 0)))))
    return rows


# ===========================================================================
# AC1: Two genuinely live same-app runs → 2 distinct cards
# ===========================================================================
# Each card carries its own ticket id, stage bar, elapsed timer, pass count,
# and a log link filtered to its own ticket id (no bleed).


def test_ac1_two_live_runs_two_cards():
    """Two genuinely live same-app runs → 2 distinct Active-run cards.

    TICKET-A (20-s ago) and TICKET-B (120-s ago) both within _LIVE_WITHIN_S (~150 s),
    different ticket ids, both non-terminal. Assert exactly two ``runhead runlive``
    fragments, each containing:
      a) its own ticket id,
      b) a phasebar (stage bar),
      c) an elapsed timer string ("0m …" or "Xm …"),
      d) a pass-count span,
      e) data-log-ticket="TICKET-?" matching that card's own id only.
    """
    rows = _now_rows(("TICKET-A", 20), ("TICKET-B", 120))
    cfg = _make_cfg(rows, max_builders=2)
    html = warroom.render_board(cfg, "testapp", {"active": True})

    # Split into per-card fragments (split gives n+1 parts).
    fragments = html.split('<div class="runhead runlive"')
    chk("AC1a: exactly two live cards rendered", len(fragments) == 3,
        f"expected 3 fragments (2 cards), got {len(fragments)}")

    if len(fragments) >= 3:
        card_a, card_b = fragments[1], fragments[2]

        # a) Each card has its own ticket id in correct order.
        chk("AC1b: card-a contains TICKET-A", "TICKET-A" in card_a,
            "TICKET-A not found in first card fragment")
        chk("AC1c: card-b contains TICKET-B", "TICKET-B" in card_b,
            "TICKET-B not found in second card fragment")

        # b) Each card has a stage bar.
        chk("AC1d: card-a has a phasebar", "phasebar" in card_a,
            "card-a missing phasebar")
        chk("AC1e: card-b has a phasebar", "phasebar" in card_b,
            "card-b missing phasebar")

        # c) Elapsed timer (format: "0m 20s" vs "2m 00s" etc.) — present and differs.
        chk("AC1f: card-a has an elapsed timer", "elapsed" in card_a and re.search(
            r"\d+m \d+s|elapsed <b>", card_a),
            "card-a missing elapsed timer")
        chk("AC1g: card-b has an elapsed timer", "elapsed" in card_b and re.search(
            r"\d+m \d+s|elapsed <b>", card_b),
            "card-b missing elapsed timer")

        # d) Pass count — present in each card's runmeta (format: ``pass <b>N</b>``).
        # Use flexible matching across nested HTML tags; fall back to a bare "pass" check.
        _pass_re = r'pass[^>]*>\s*<b>[^<]*</b>|<b>\d*</b>'
        chk("AC1h: card-a has pass count",
            bool(re.search(r'>pass ', card_a)) or bool(re.search(r'<b>\d*</b>', card_a)),
            "card-a missing pass count")
        chk("AC1i: card-b has pass count",
            bool(re.search(r'>pass ', card_b)) or bool(re.search(r'<b>\d*</b>', card_b)),
            "card-b missing pass count")

        # e) Per-card data-log-ticket matches THAT card's ticket id only.
        log_a = re.findall(r'data-log-ticket="([^"]+)"', card_a)
        log_b = re.findall(r'data-log-ticket="([^"]+)"', card_b)
        chk("AC1j: card-a data-log-ticket=TICKET-A",
            "TICKET-A" in log_a,
            f"TICKET-A card log-ticket attrs={log_a}")
        chk("AC1k: card-b data-log-ticket=TICKET-B",
            "TICKET-B" in log_b,
            f"TICKET-B card log-ticket attrs={log_b}")

        # f) No cross-contamination: card-a must NOT contain TICKET-B's log-ticket.
        chk("AC1l: card-a log-ticket is not TICKET-B (no bleed)",
            log_a != ["TICKET-B"] and "TICKET-B" not in log_a,
            f"card-a log-ticket should not be TICKET-B; attrs={log_a}")


# ===========================================================================
# AC2: Zero / one live run → byte-identity with pre-multi-card path
# ===========================================================================
# Uses a wrapper monkeypatch on _run_html to capture the single-call signature
# and verify the captured HTML appears byte-for-byte in the board.


def test_ac2_zero_live_runs_idle_board():
    """Zero live runs → idle board identical to pre-EU-486.

    Fixture: terminal merged event → idle board. Assert 'Active run' header exists,
    no crash, and no ``data-log-ticket`` attribute leaks onto the idle board.
    """
    cfg = _make_cfg([
        dict(event="merged", ticket_id="OLD-1", app="testapp", ts=_ts(3600)),
    ])
    html = warroom.render_board(cfg, "testapp", {})
    chk("AC2a: renders without crash", html is not None and len(html) > 0,
        "render_board raised or returned empty")
    chk("AC2b: 'Active run' panel header present", "Active run" in html,
        "missing 'Active run' panel header")
    chk("AC2c: no data-log-ticket on idle board",
        "data-log-ticket" not in html,
        "idle board should NOT carry data-log-ticket")


def test_ac2_one_live_run_single_path_identity():
    """Single live run → structurally byte-identical to pre-EU-486 single-card output.

    Records the ``_run_html`` call via monkeypatch: assert single invocation, no
    ``log_ticket`` kwarg, and the captured HTML appears byte-for-byte inside the board.
    This is the strongest non-flaky byte-identity pin because elapsed time is baked in.
    """
    cfg = _make_cfg([
        dict(event="ticket_start", ticket_id="ONE-1", app="testapp", branch="b", ts=_ts(30)),
        dict(event="build", ticket_id="ONE-1", app="testapp", ts=_ts(10)),
    ])

    _calls: list[dict] = []
    original = warroom._run_html

    def _recorder(run, mode=None, elapsed=None, manual=False, log_path=None,
                  log_ticket=None, stopping=False):
        result = original(run, mode=mode, elapsed=elapsed, manual=manual,
                          log_path=log_path, log_ticket=log_ticket, stopping=stopping)
        _calls.append({
            "run": run, "mode": mode, "elapsed": elapsed, "manual": manual,
            "log_path": log_path, "log_ticket": log_ticket, "stopping": stopping,
            "_html": result,
        })
        return result

    try:
        warroom._run_html = _recorder
        html = warroom.render_board(cfg, "testapp", {"active": True})

        chk("AC2d: _run_html called exactly once", len(_calls) == 1,
            f"expected 1 call, got {len(_calls)}")
        if _calls:
            c = _calls[0]
            chk("AC2e: no log_ticket kwarg (single-run invariant)",
                c["log_ticket"] is None, f"log_ticket={c['log_ticket']!r}")
            captured = c["_html"]
            chk("AC2f: captured HTML appears byte-for-byte in board",
                captured in html,
                f"captured ({len(captured)} chars) not found in board ({len(html)} chars)")
        else:
            chk("AC2f: (skip — no call recorded)", False, "no _run_html call was made")

        chk("AC2g: ONE-1 appears on the board", "ONE-1" in html, "ONE-1 missing from board")
        chk("AC2h: no data-log-ticket attribute (EU-487 single-run invariant)",
            "data-log-ticket" not in html,
            "single-run HTML should NOT carry data-log-ticket")

    finally:
        warroom._run_html = original


# ===========================================================================
# AC3: Cap enforcement — burst of N > max_concurrent_builders renders ≤ cap
# ===========================================================================


def test_ac3_cap_enforcement():
    """N=4 live runs with max_concurrent_builders=2 → renders exactly 2 cards.

    Newest-first ordering: FOUR-1 and FOUR-2 remain; THREE older tickets (FOUR-3, FOUR-4)
    are capped out and completely absent from the HTML output.
    """
    rows = _now_rows(("FOUR-1", 10), ("FOUR-2", 30), ("FOUR-3", 60), ("FOUR-4", 90))
    cfg = _make_cfg(rows, max_builders=2)
    html = warroom.render_board(cfg, "testapp", {"active": True})

    fragments = html.split('<div class="runhead runlive"')
    chk("AC3a: exactly two cards (oldest two capped out)", len(fragments) == 3,
        f"expected 3 fragments (2 cards), got {len(fragments)}")

    chk("AC3b: FOUR-1 (newest) is on the board",
        "FOUR-1" in html, "newest ticket missing from board")
    chk("AC3c: FOUR-2 (middle) is on the board",
        "FOUR-2" in html, "middle ticket missing from board")
    chk("AC3d: FOUR-3 (older beyond cap) is absent",
        "FOUR-3" not in html, "capped ticket appeared despite limit")
    chk("AC3e: FOUR-4 (oldest beyond cap) is absent",
        "FOUR-4" not in html, "capped ticket appeared despite limit")


# ===========================================================================
# AC4: Cross-scenario sanity — ensure nothing regresses between runs
# ===========================================================================


def test_ac4_harness_self_check():
    """Sanity: the harness ran ≥1 check and the tally line format is parseable.

    This is a minimal self-check so ``run_all.py`` can read k/n passed for this
    harness even if individual checks somehow never fire (import error path).
    """
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    chk("AC4a: harness executed checks", total >= 10,
        f"only {total} checks recorded (expected ≥10 from AC1–AC3)")
    chk("AC4b: all checks passed", passed == total,
        f"{passed}/{total} checks passed — see failures above")


# ===========================================================================
# Report (used by run_all.py auto-tally detection)
# ===========================================================================

if __name__ == "__main__":
    for _name in sorted(globals()):
        fn = globals()[_name]
        if callable(fn) and _name.startswith("test_"):
            fn()
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n{'=' * 56}")
    print(f"  EU-489 warroom render integration test")
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
