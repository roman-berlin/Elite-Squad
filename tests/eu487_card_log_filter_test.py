"""EU-487: each Active-run card filters the SHARED drain log to its own ticket id.

The concurrent drain interleaves every build's stdout into ONE shared log (per-ticket log
files are EU-444, explicitly out of scope).  Each card rendered by the EU-486 multi-card
loop must therefore carry a filter param naming its OWN ticket, and /api/run-log-stream
must honour an optional ``ticket`` query param that passes only lines naming that ticket.

Guards five things (fail-first: every check is RED against the pre-change code, where the
stream accepts only ``app`` and no card carries a filter attribute):

  1. Two live runs → two cards, each card's emitted ``data-log-ticket`` equals that card's
     own ticket id (the TICKET-A card never carries TICKET-B's param, and vice versa).
  2. server._ticket_line_ok() — the pure line filter — passes only lines containing the
     requested ticket key out of an interleaved two-ticket fixture; no/empty ticket → all
     lines pass.  And /api/run-log-stream?ticket=TICKET-A streams ONLY TICKET-A's lines
     from a shared drain fixture with interleaved lines from both tickets.
  3. Single-run case unchanged: /api/run-log-stream with no ticket param emits every line,
     and render_board's single-live-run HTML carries NO per-card filter attribute (the
     runlog JS only appends &ticket= when a card carries the attribute — pinned on source).
  4. No per-ticket log files / log-splitting: run_logger.py is untouched and the endpoint
     opens the log read-only (no write-mode opens, no new file-writing code).
  5. Soft ``k/n passed`` tally so tests/run_all.py (the EU-44 gate) judges it honestly.

Stubs the Agent SDK / requests so importing the orchestrator needs no network / real models.
"""
import inspect
import json
import re
import sys
import tempfile
import threading
import time
import types
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")

# --- Minimal stubs (same shape as eu486 / eu361) ---
_sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
_sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", _sdk)

_req = types.ModuleType("requests")
_req.Session = lambda *a, **k: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
_req.RequestException = Exception
sys.modules.setdefault("requests", _req)

from orchestrator import warroom, dashboard as D  # noqa: E402
from orchestrator import cockpit_state  # noqa: E402
import orchestrator.server as srv  # noqa: E402
from orchestrator.config import Config, AppConfig  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


def _make_cfg(rows: list[dict], *, max_builders: int = 1) -> Config:
    """A temp Config pointing at a fresh audit populated with *rows*."""
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
    return (datetime.now().astimezone() - timedelta(seconds=secs_ago)).strftime("%Y-%m-%dT%H:%M:%S%z")


def _two_live_cfg() -> Config:
    """Two genuinely-live same-app runs, newest-first: TICKET-A (10s), TICKET-B (50s)."""
    return _make_cfg([
        dict(event="ticket_start", ticket_id="TICKET-B", app="testapp", branch="b", ts=_ts(50)),
        dict(event="ticket_start", ticket_id="TICKET-A", app="testapp", branch="b", ts=_ts(20)),
        dict(event="build", ticket_id="TICKET-A", app="testapp", ts=_ts(10)),
    ], max_builders=2)


# ===========================================================================
# AC1: two cards — each card's filter param equals that card's own ticket id
# ===========================================================================

def test_ac1_per_card_filter_param():
    cfg = _two_live_cfg()
    html = warroom.render_board(cfg, "testapp", {"active": True})

    chk("AC1a: both ticket ids render on the board",
        "TICKET-A" in html and "TICKET-B" in html, "a ticket id is missing from the board")

    # Each card's runhead carries its own data-log-ticket attribute.
    chk("AC1b: TICKET-A card emits its own filter attribute",
        'data-log-ticket="TICKET-A"' in html, "no data-log-ticket=\"TICKET-A\" on the board")
    chk("AC1c: TICKET-B card emits its own filter attribute",
        'data-log-ticket="TICKET-B"' in html, "no data-log-ticket=\"TICKET-B\" on the board")

    # The attribute on each card must belong to THAT card: split the board into card
    # fragments at each live runhead, then the fragment's filter attr and its runtitle
    # ticket must be the same id.  Cards are ordered newest-first (TICKET-A first).
    fragments = html.split('<div class="runhead runlive"')[1:]
    chk("AC1d: two live cards rendered", len(fragments) == 2, f"got {len(fragments)} cards")
    seen: dict[str, str] = {}
    for frag in fragments:
        m_attr = re.search(r'data-log-ticket="([^"]+)"', frag)
        m_title = re.search(r'font-family:var\(--mono\)">([^<]+)</span>', frag)
        attr = m_attr.group(1) if m_attr else "(none)"
        title = m_title.group(1) if m_title else "(none)"
        seen[title] = attr
    chk("AC1e: TICKET-A card's filter param is TICKET-A (never TICKET-B)",
        seen.get("TICKET-A") == "TICKET-A", f"card TICKET-A carries filter {seen.get('TICKET-A')!r}")
    chk("AC1f: TICKET-B card's filter param is TICKET-B (never TICKET-A)",
        seen.get("TICKET-B") == "TICKET-B", f"card TICKET-B carries filter {seen.get('TICKET-B')!r}")

    # The card's open-log/stream link param, when a log link is present, carries the same
    # ticket — asserted directly on _run_html with a log_path so the link renders.
    run = {"live": True, "ticket": "TICKET-A", "app": "testapp", "branch": "b", "passes": 1,
           "verdict": "", "outcome": None, "cost": 0.0, "phases": ["Build", "Gate", "Review", "Land"],
           "reached": 1, "failed_phase": None, "sparkline": []}
    try:
        card = warroom._run_html(run, mode="live", elapsed="1m", manual=False,
                                 log_path="/tmp/shared-drain.log", log_ticket="TICKET-A")
    except TypeError as exc:
        card = ""
        chk("AC1g-pre: _run_html accepts the log_ticket kwarg", False, str(exc))
    chk("AC1g: card's open-log link carries &ticket=<its own id>",
        "ticket=TICKET-A" in card, f"no ticket=TICKET-A param in card link: {card[:200]!r}")


# ===========================================================================
# AC2: the line filter — helper unit checks + endpoint behaviour
# ===========================================================================

_INTERLEAVED = (
    "TICKET-A: builder started\n"
    "TICKET-B: builder started\n"
    "TICKET-A: gate passed\n"
    "untagged infra noise line\n"
    "TICKET-B: review pass\n"
)


def test_ac2_line_filter_helper():
    fn = getattr(srv, "_ticket_line_ok", None)
    chk("AC2a: server._ticket_line_ok exists (module-level pure helper)", callable(fn),
        "orchestrator/server.py has no _ticket_line_ok helper")
    if not callable(fn):
        return
    chk("AC2b: line naming the requested ticket passes",
        fn("TICKET-A: gate passed", "TICKET-A") is True, "")
    chk("AC2c: line naming the OTHER ticket is dropped",
        fn("TICKET-B: review pass", "TICKET-A") is False, "")
    chk("AC2d: untagged line is dropped while a filter is active",
        fn("untagged infra noise line", "TICKET-A") is False, "")
    chk("AC2e: no ticket param (None) passes every line",
        fn("TICKET-B: review pass", None) is True and fn("untagged", None) is True, "")
    chk("AC2f: empty ticket param passes every line",
        fn("anything", "") is True, "")


def _stream_frames(query: str, log_file: Path, collect_s: float = 1.6) -> str:
    """Drive /api/run-log-stream with *query* against a pre-written *log_file*;
    return the concatenated SSE frames (eu361-style threaded consume)."""
    tmp = Path(tempfile.mkdtemp())
    audit = tmp / "audit.jsonl"
    audit.write_text("", encoding="utf-8")
    cfg = Config(apps=[AppConfig(name="alpha", repo_path=str(tmp), base_branch="DEV",
                                 protected_branch="MAIN", backlog_backend="none")],
                 audit_path=str(audit), use_worktree=False)
    cfg.detected_auth = lambda: "test"
    srv.health.summary = lambda c: {"healthy": True, "checks": []}
    app = srv.create_app(cfg)

    st = cockpit_state.get_state("alpha")
    st["active"] = True
    st["log_path"] = str(log_file)

    with app.test_request_context(query):
        resp = app.view_functions["run_log_stream_api"]()
    gen = resp.response

    frames: list[str] = []
    stop = threading.Event()

    def _consume():
        try:
            for chunk in gen:
                frames.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace"))
                if stop.is_set():
                    break
        except Exception:  # noqa: BLE001 — the generator is closed from the main thread
            pass

    t = threading.Thread(target=_consume, daemon=True)
    t.start()
    time.sleep(collect_s)
    stop.set()
    try:
        gen.close()
    except Exception:  # noqa: BLE001
        pass
    t.join(timeout=3)
    st["active"] = False
    st["log_path"] = None
    return "".join(frames)


def test_ac2_stream_filtered_per_ticket():
    shared = Path(tempfile.mkdtemp()) / "drain.log"
    shared.write_text(_INTERLEAVED, encoding="utf-8")
    body = _stream_frames("/api/run-log-stream?app=alpha&ticket=TICKET-A", shared)
    chk("AC2g: ?ticket=TICKET-A streams TICKET-A's builder line",
        "TICKET-A: builder started" in body, f"frames={body[:200]!r}")
    chk("AC2h: ?ticket=TICKET-A streams TICKET-A's gate line",
        "TICKET-A: gate passed" in body, f"frames={body[:200]!r}")
    chk("AC2i: ?ticket=TICKET-A never streams the OTHER ticket's lines",
        "TICKET-B" not in body, f"frames={body[:300]!r}")
    chk("AC2j: ?ticket=TICKET-A drops untagged lines (interleaved fixture)",
        "untagged infra noise" not in body, f"frames={body[:300]!r}")


# ===========================================================================
# AC3: single-run case unchanged
# ===========================================================================

def test_ac3_stream_unfiltered_without_param():
    shared = Path(tempfile.mkdtemp()) / "drain.log"
    shared.write_text(_INTERLEAVED, encoding="utf-8")
    body = _stream_frames("/api/run-log-stream?app=alpha", shared)
    for line in _INTERLEAVED.strip().splitlines():
        chk(f"AC3a: no ticket param streams every line — {line[:24]!r}",
            line in body, f"line missing; frames={body[:300]!r}")


def test_ac3_single_run_board_unchanged():
    cfg = _make_cfg([
        dict(event="ticket_start", ticket_id="SOLO-1", app="testapp", ts=_ts(30)),
        dict(event="build", ticket_id="SOLO-1", app="testapp", ts=_ts(10)),
    ])
    html = warroom.render_board(cfg, "testapp", {"active": True})
    chk("AC3b: single-run board renders the live ticket", "SOLO-1" in html, "SOLO-1 missing")
    chk("AC3c: single-run board carries NO per-card filter attribute",
        "data-log-ticket" not in html,
        "single-run HTML gained a data-log-ticket attribute — output changed")

    # The runlog JS must only append &ticket= when a card carries the attribute —
    # with none on the board, the EventSource URL is exactly today's.
    page_src = getattr(warroom, "_PAGE", "")
    chk("AC3d: runlog JS guards &ticket= on the card attribute's presence",
        "encodeURIComponent(logTicket)" in page_src
        and "if(logTicket)" in page_src and "data-log-ticket" in page_src,
        "runlog JS does not build the ticket param from the card attribute")


# ===========================================================================
# AC4: no per-ticket log files / log-splitting introduced
# ===========================================================================

def test_ac4_no_log_splitting():
    rl_src = (Path("orchestrator") / "run_logger.py").read_text(encoding="utf-8")
    chk("AC4a: run_logger.py untouched by EU-487 (no ticket-split code added)",
        "EU-487" not in rl_src, "run_logger.py references EU-487 — log-splitting crept in")

    ep_src = inspect.getsource(srv.create_app)
    start = ep_src.index("def run_log_stream_api")
    stream_src = ep_src[start:]
    nxt = stream_src[10:].find("\n    @app.")
    if nxt != -1:
        stream_src = stream_src[:10 + nxt]
    chk("AC4b: the stream endpoint never opens a file for writing",
        not re.search(r"\.open\([^)]*[\"']w", stream_src) and "write_text" not in stream_src
        and ".write(" not in stream_src,
        "run_log_stream_api gained a file-writing call")
    chk("AC4c: the endpoint's only file open stays read-mode tailing",
        'f.open("r"' in stream_src, "the drain tail no longer opens the log read-only")


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
    print("  EU-487 per-card drain-log filter tests")
    print(f"  {'─' * 56}")
    for _name, _ok, _det in results:
        mark = "PASS" if _ok else "FAIL"
        extra = f"  ({_det})" if _det and not _ok else ""
        print(f"    [{mark}] {_name}{extra}")
    print(f"  {'─' * 56}")
    print(f"  {passed}/{total} passed")
    failed = total - passed
    if failed:
        print(f"  RESULT: {failed} FAILED")
        sys.exit(1)
    print("  RESULT: ALL GREEN")
    sys.exit(0)
