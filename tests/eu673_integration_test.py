"""EU-673 — Verify & close: one-shot action results on the live SSE board (integration).

The sibling pieces each test ONE seam in isolation (EU-669 peek/dismiss, EU-670 strip render,
EU-672 per-app live-board guard, EU-675 JS handler + endpoint, EU-676 index off the pop). This
is the integration check none of them covers: the REAL unit-level writers — the QA verdict
(server.py ``set_last_result(None, 'ok', "✓ QA finished for …")``), standup, council, scribe,
start-refusals — store their outcome on the UNIT-WIDE ``_state`` (app=None), while the live
board and its SSE stream render from the TAB's per-app view state. Before EU-673, a QA verdict
therefore NEVER reached the live board — only a full GET / could surface it (via the old bar
strip), defeating AC1 of EU-648. EU-673 closes the gap with a newest-wins overlay in
``server._view_state`` and removes the now-duplicate bar strip from index().

Acceptance criteria of the ORIGINAL feature (EU-648), verified end-to-end here:
  AC1  A pending one-shot result appears on the live SSE-driven board within one poll/SSE tick,
       with no full GET / required — for BOTH writer scopes (per-app AND unit-wide).
  AC2  The strip is dismissible; dismissing clears the underlying stored record (both scopes)
       so it does not reappear on the next load — including a next FULL GET /.
  AC3  If left un-dismissed, it still renders correctly on a subsequent full GET / — exactly
       once (no double-render), no regression to the old pop-and-clear behaviour.
  AC4  Regression path: write a QA-verdict result DIRECTLY (the production writer call), hit
       the live-board endpoint/poll, assert the message is present before any GET / occurs.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import types
from pathlib import Path

# ── SDK / network stubs (same pattern as the sibling tests) ────────────────────
_sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
_sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = _sdk

_req = types.ModuleType("requests")
_req.Session = lambda: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = _req

sys.path.insert(0, ".")

from orchestrator import cockpit_state, server
from orchestrator.config import AppConfig, Config

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))
    tag = "PASS" if cond else "FAIL"
    line = f"  [{tag}] {name}"
    if detail and not cond:
        line += f"  ({detail})"
    print(line)


print("\n================ EU-673 Integration Verify & Close ================")

tmp = Path(tempfile.mkdtemp())
cfg = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(tmp / "app"), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(tmp / "audit.jsonl"),
    use_worktree=False,
)
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()

QA_MSG = "✓ QA finished for automatixy — ship verdict posted (see /council + Telegram)"
STRIP_BTN = "<button data-dismiss-result"


def _clear() -> None:
    """Remove every last_result key from BOTH scopes."""
    for st in (server._state, cockpit_state.get_state("automatixy")):
        st.pop("last_result", None)
        st.pop("last_result_record", None)


def _board() -> str:
    return client.get("/api/board?app=automatixy").get_data(as_text=True)


def _qa_verdict() -> None:
    """Write the QA verdict EXACTLY as the production route does (server.py: app=None)."""
    server.set_last_result(None, "ok", QA_MSG)


def _sse_first_frame(timeout: float = 8.0) -> str | None:
    """The first 'board' SSE frame from /api/stream, read in a worker thread (the stream is
    infinite, so the reader is bounded by `timeout`). Returns None when no frame arrived."""
    out: list[str] = []

    def _read() -> None:
        with client.get("/api/stream?app=automatixy") as resp:
            buf: list[str] = []
            kind = None
            for raw in resp.response:
                chunk = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
                for line in chunk.split("\n"):
                    if line.startswith("event:"):
                        kind = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        buf.append(line[5:].lstrip(" "))
                    elif line == "" and kind == "board" and buf:
                        out.append("\n".join(buf))
                        return

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout)
    return out[0] if out else None


# ===========================================================================
# Test A — AC4 + AC1(poll): QA verdict written directly → live board shows it,
#          NO GET / anywhere in this test.
# ===========================================================================
print("\n--- Test A: QA-verdict writer → /api/board before any GET / ---")
_clear()
_qa_verdict()                                  # the production writer call, verbatim scope
board_html = _board()                          # the live-board poll endpoint
chk("A1: QA verdict text present on /api/board (no GET / issued)",
    QA_MSG in board_html, f"'{QA_MSG[:40]}…' in {len(board_html)} chars of board HTML")
chk("A2: dismiss button present alongside the verdict",
    STRIP_BTN in board_html)


# ===========================================================================
# Test B — AC1(SSE): the verdict arrives on the SSE stream within ONE tick,
#          still with no GET /.
# ===========================================================================
print("\n--- Test B: QA verdict delivered by the SSE stream ---")
_clear()
_qa_verdict()
frame = _sse_first_frame()
chk("B1: first SSE board frame carries the QA verdict (one tick, no GET /)",
    frame is not None and QA_MSG in frame,
    "no SSE frame within timeout" if frame is None else "verdict missing from frame")
chk("B2: the SSE frame carries the dismiss button too",
    frame is not None and STRIP_BTN in frame)

# Per-app writers must keep flowing over SSE as before (EU-672's scope, regression guard).
_clear()
server.set_last_result("automatixy", "ok", "✓ Closed EU-673 from directive")
frame = _sse_first_frame()
chk("B3: per-app result still delivered over SSE (regression)",
    frame is not None and "Closed EU-673 from directive" in frame)


# ===========================================================================
# Test C — AC2: dismiss clears the stored record; it does NOT reappear on the
#          next load — including the next FULL GET /.
# ===========================================================================
print("\n--- Test C: dismiss persists across full-page reloads ---")
_clear()
_qa_verdict()
chk("C0: strip present before dismiss", QA_MSG in _board() and STRIP_BTN in _board())

r = client.post("/api/dismiss-result?app=automatixy")
chk("C1: POST /api/dismiss-result → 200 {ok: true}",
    r.status_code == 200 and json.loads(r.get_data(as_text=True)) == {"ok": True},
    f"{r.status_code} {r.get_data(as_text=True)[:80]}")
chk("C2: BOTH scopes cleared (unit-wide record AND per-app copy)",
    "last_result_record" not in server._state
    and "last_result_record" not in cockpit_state.get_state("automatixy")
    and not server._state.get("last_result")
    and not cockpit_state.get_state("automatixy").get("last_result"))

board_after = _board()
chk("C3: board no longer shows the verdict", QA_MSG not in board_after)
page_after = client.get("/").get_data(as_text=True)
chk("C4: the next FULL GET / does not bring it back either",
    QA_MSG not in page_after and STRIP_BTN not in page_after)


# ===========================================================================
# Test D — AC3: un-dismissed, the verdict renders correctly on subsequent full
#          GET / — exactly once (the pre-EU-673 bar strip would double-render).
# ===========================================================================
print("\n--- Test D: pending verdict survives full GET /, rendered exactly once ---")
_clear()
_qa_verdict()

page1 = client.get("/").get_data(as_text=True)
chk("D1: first full GET / shows the verdict strip", QA_MSG in page1)
chk("D2: strip rendered exactly once on the page (no bar+board double-render)",
    page1.count(STRIP_BTN) == 1, f"button occurs {page1.count(STRIP_BTN)}×")

page2 = client.get("/").get_data(as_text=True)
chk("D3: second full GET / still shows it (nothing pops it)",
    QA_MSG in page2 and page2.count(STRIP_BTN) == 1)

chk("D4: the live board agrees (same strip as the page)",
    QA_MSG in _board())
chk("D5: stored record survived every render (state intact for the SSE stream)",
    isinstance(server._state.get("last_result_record"), dict)
    and server._state["last_result_record"].get("text") == QA_MSG)


# ===========================================================================
# Test E — overlay precedence: newest record wins across the two scopes.
# ===========================================================================
print("\n--- Test E: newest-wins overlay across unit-wide vs per-app ---")
_clear()
server.set_last_result("automatixy", "error", "✗ per-app run failed")
_qa_verdict()
# Deterministic ordering: stamp the unit-wide verdict NEWER than the per-app failure.
cockpit_state.get_state("automatixy")["last_result_record"]["timestamp"] = 1_000.0
server._state["last_result_record"]["timestamp"] = 2_000.0
chk("E1: newer unit-wide verdict wins over older per-app result",
    QA_MSG in _board() and "per-app run failed" not in _board())

server._state["last_result_record"]["timestamp"] = 500.0   # verdict now OLDER
chk("E2: newer per-app result wins over older unit-wide verdict",
    "per-app run failed" in _board() and QA_MSG not in _board())

# Dismiss from the tab clears BOTH scopes regardless of which one was showing.
client.post("/api/dismiss-result?app=automatixy")
chk("E3: one dismiss clears both scopes",
    QA_MSG not in _board() and "per-app run failed" not in _board())
_clear()


# ===========================================================================
# Test F — legacy plain-string writers (pre-EU-653, or anything assigning
#          last_result directly) still reach the live board and the page.
# ===========================================================================
print("\n--- Test F: unit-wide legacy string fallback ---")
_clear()
server._state["last_result"] = "Shipped automatixy DEV→MAIN (3 commit(s)) to PRODUCTION."
chk("F1: legacy unit-wide string shows on the live board",
    "3 commit(s)" in _board())
chk("F2: …and on a full GET / (exactly once)",
    (p := client.get("/").get_data(as_text=True)).count(STRIP_BTN) == 1
    and "3 commit(s)" in p)
# A structured record anywhere beats the undated legacy string (_result_strip precedence).
server.set_last_result("automatixy", "error", "✗ structured beats legacy")
chk("F3: a per-app record outranks the unit-wide legacy string",
    "structured beats legacy" in _board() and "3 commit(s)" not in _board())
_clear()


# ===========================================================================
# Test G — no stray destructive readers: visiting /memory (the page that used
#          to POP the stored result) must not erase the board's strip.
# ===========================================================================
print("\n--- Test G: /memory peeks, the board strip survives a page visit ---")
_clear()
_qa_verdict()
mem = client.get("/memory").get_data(as_text=True)
chk("G1: /memory shows the confirmation banner (peek)", QA_MSG in mem)
chk("G2: the board strip survives the /memory visit (EU-673 pop-reader fix)",
    QA_MSG in _board())
chk("G3: the stored record is still intact afterwards",
    isinstance(server._state.get("last_result_record"), dict))
_clear()


# ===========================================================================
# Report
# ===========================================================================
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print("\n-----------------------------------------------------------")
print(f"  {passed}/{total} passed")
if passed != total:
    print(f"  RESULT: {total - passed} FAIL")
    sys.exit(1)
print("  RESULT: ALL GREEN")
sys.exit(0)
