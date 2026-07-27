"""EU-714 — Reconcile the GET / banner render with the live-board polling strip
so a single result is never shown twice, and a dismiss clears it for BOTH paths.

Chosen reconciliation behavior (documented per AC4):
  ONE server-side source of truth. The store KEEPS the record until dismissed
  (EU-676 non-destructive peek — a page visit must never consume it), and EVERY
  surface reads that one store through ONE renderer (cockpit_views._result_strip):
    * GET / embeds the board (warroom.render_page → render_board splices the
      strip at the top of #board) — there is no separate bar/banner copy;
    * the live board's 5s poll (/api/board) and SSE stream (/api/stream) render
      the SAME strip into the SAME #board slot, and the client applies frames by
      WHOLESALE innerHTML replacement (applyBoard) — never an append, so polls
      cannot accumulate a duplicate;
    * GET /api/last-result (EU-712) therefore legitimately STILL returns the
      record after GET / showed it — "not re-surfaced as NEW" is proven by the
      record's stable ``timestamp``: it is the identical record (same identity
      key), which a last-seen client dedup would use to skip re-rendering.
  Dismissing via EITHER channel — POST /api/dismiss-result (what the board's
  delegated click handler posts, EU-675) or POST /api/last-result/dismiss
  (EU-712) — clears BOTH scopes (per-app AND unit-wide), so neither a later
  GET / banner nor a later poll can resurrect the dismissed result.

Counting note: the full GET / page's inline JS contains the selector LITERAL
``[data-dismiss-result]`` (the EU-675 delegated click handler), so strip
presence is counted via the FULL rendered ``<button data-dismiss-result`` tag —
exactly one per rendered strip, zero when no strip is pending.

Acceptance criteria covered:
  AC1  trigger → GET / shows the strip once → the live board's subsequent poll
       does not show a duplicate for that same result (Test A);
  AC2  trigger → polling strip shown first → full GET / reload shows it once,
       no banner+strip double-render (Test B);
  AC3  dismissing via either dismiss endpoint keeps the next GET / (and the
       next poll) clean (Test C);
  AC4  the one-shot → GET / → poll sequence with the chosen dedup behavior
       documented above (Tests A + D).
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

# ── SDK / network stubs (same pattern as eu715_integration_test.py) ──────────
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


print("\n================ EU-714 Banner/Poll Reconciliation ================")

tmp = Path(tempfile.mkdtemp())
cfg = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(tmp / "app"), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(tmp / "audit.jsonl"),
    use_worktree=False,
)
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()

# The FULL rendered dismiss button — distinguishes a real strip from the page's
# JS selector literal "[data-dismiss-result]" (the EU-675 delegated handler),
# which is present on EVERY full page whether or not a strip is pending.
STRIP_BTN = "<button data-dismiss-result"


def _clear() -> None:
    """Remove every last_result key from BOTH scopes."""
    for st in (server._state, cockpit_state.get_state("automatixy")):
        st.pop("last_result", None)
        st.pop("last_result_record", None)


def _page() -> str:
    return client.get("/?app=automatixy").get_data(as_text=True)


def _board() -> str:
    return client.get("/api/board?app=automatixy").get_data(as_text=True)


def _api() -> dict:
    return json.loads(client.get("/api/last-result?app=automatixy").get_data(as_text=True))


# ===========================================================================
# Test A — AC1/AC4: GET / shows the strip once; the subsequent live-board poll
#                   does not re-surface the same result as a duplicate.
# ===========================================================================
print("\n--- Test A: banner-first — GET / then live-board poll (no duplicate) ---")
_clear()
server.set_last_result("automatixy", "ok", "A: merged EU-999 to DEV")
ts_before = cockpit_state.get_state("automatixy")["last_result_record"]["timestamp"]

page1 = _page()
chk("A1: GET / renders the pending result exactly once (one strip, no bar copy)",
    page1.count("A: merged EU-999 to DEV") == 1 and page1.count(STRIP_BTN) == 1,
    f"msg×{page1.count('A: merged EU-999 to DEV')} btn×{page1.count(STRIP_BTN)}")

api1 = _api()
chk("A2: poll of /api/last-result after GET / returns the SAME result, not a new one "
    "(identical timestamp = same identity; the store legitimately still holds it)",
    api1.get("text") == "A: merged EU-999 to DEV" and api1.get("timestamp") == ts_before,
    f"got {api1}")

frame1 = _board()
chk("A3: the live-board poll frame renders exactly one strip for that result "
    "(no banner+strip double-render)",
    frame1.count("A: merged EU-999 to DEV") == 1 and frame1.count(STRIP_BTN) == 1,
    f"msg×{frame1.count('A: merged EU-999 to DEV')} btn×{frame1.count(STRIP_BTN)}")

frame2 = _board()
chk("A4: repeated polls do not accumulate strips (frames replace the #board slot, "
    "never append)",
    frame2.count("A: merged EU-999 to DEV") == 1 and frame2.count(STRIP_BTN) == 1)

# ===========================================================================
# Test B — AC2: polling strip shown first; a later full GET / reload renders
#               the same result once — no duplicate banner.
#               (Unit-wide writer here — standup/council/QA write app=None —
#               to prove the _view_state overlay feeds both surfaces identically.)
# ===========================================================================
print("\n--- Test B: strip-first — board poll then full GET / reload (no duplicate) ---")
_clear()
server.set_last_result(None, "error", "B: council failed on DEV")

frame = _board()
chk("B1: the polling strip shows the result exactly once",
    frame.count("B: council failed on DEV") == 1 and frame.count(STRIP_BTN) == 1)

page = _page()
chk("B2: a full GET / reload after the poll shows it exactly once (no second banner)",
    page.count("B: council failed on DEV") == 1 and page.count(STRIP_BTN) == 1,
    f"msg×{page.count('B: council failed on DEV')} btn×{page.count(STRIP_BTN)}")

# ===========================================================================
# Test C — AC3: dismissing via EITHER path clears the store for BOTH surfaces.
# ===========================================================================
print("\n--- Test C: dismiss clears the result for the banner AND the poll ---")
_clear()
server.set_last_result("automatixy", "warn", "C: no backlog configured")
assert STRIP_BTN in _page()   # precondition: the banner is up

r = client.post("/api/dismiss-result?app=automatixy")   # the board's JS click channel (EU-675)
chk("C1: strip-dismiss endpoint returns 200 {ok: true}",
    r.status_code == 200 and json.loads(r.get_data(as_text=True)) == {"ok": True})
page = _page()
chk("C2: a subsequent GET / shows no banner for the dismissed result",
    "C: no backlog configured" not in page and STRIP_BTN not in page)
chk("C3: …and the live-board poll agrees (frame clean, endpoint empty)",
    "C: no backlog configured" not in _board() and _api() == {})

_clear()
server.set_last_result("automatixy", "ok", "C2: shipped to DEV")
assert "C2: shipped to DEV" in _board()   # precondition: the polling strip is up

client.post("/api/last-result/dismiss?app=automatixy")  # the EU-712 channel
page = _page()
chk("C4: EU-712 endpoint dismiss also keeps the next GET / banner-free",
    "C2: shipped to DEV" not in page and STRIP_BTN not in page)
chk("C5: …and the next poll frame is clean too",
    "C2: shipped to DEV" not in _board())

# ===========================================================================
# Test D — AC4 documentation pins: the single-source-of-truth contract.
# ===========================================================================
print("\n--- Test D: the dedup contract the chosen behavior relies on ---")
_clear()
server.set_last_result("automatixy", "ok", "D: one store, one renderer")

page = _page()
board_idx = page.index("<div id=board>")
msg_idx = page.index("D: one store, one renderer")
kpis_idx = page.index("<div class=kpis>", board_idx)
chk("D1: on GET / the strip renders INSIDE the #board slot (board-embedded, as the "
    "board's first element) — there is no separate banner region that could double it",
    board_idx < msg_idx < kpis_idx,
    f"board@{board_idx} msg@{msg_idx} kpis@{kpis_idx}")

ts_state = cockpit_state.get_state("automatixy")["last_result_record"]["timestamp"]
_page()                      # full reload
_board()                     # poll
ts_api = _api().get("timestamp")
ts_state_after = cockpit_state.get_state("automatixy")["last_result_record"]["timestamp"]
chk("D2: GET / and polls are non-destructive — the record's timestamp identity is "
    "stable across a GET / + poll sequence (the last-seen key a client dedup uses)",
    ts_state == ts_api == ts_state_after,
    f"before={ts_state} api={ts_api} after={ts_state_after}")
chk("D3: the store still holds the record after both renders (peek, not pop — EU-676; "
    "persistence-until-dismiss is the sanctioned behavior pinned by result_banner_test)",
    isinstance(cockpit_state.get_state("automatixy").get("last_result_record"), dict))

page_again = _page()
chk("D4: …and the NEXT full GET / still renders it exactly once (persistent, never doubled)",
    page_again.count("D: one store, one renderer") == 1 and page_again.count(STRIP_BTN) == 1)
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
