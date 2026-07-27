"""EU-715 — Verify & close: Render one-shot results as a dismissible strip on the live
(SSE) board (epic EU-700 integration check).

The sibling pieces each test ONE seam in isolation: EU-699 the persistent
(tone, text, timestamp) store, EU-712 the GET /api/last-result + POST /api/last-result/dismiss
endpoints, EU-701 the ok/error/warn tone map; EU-673's harness predates all three and covers the
board/SSE render + full-page dedup. This is the integration check none of them covers — the
cross-wiring between the EU-712 JSON surface and what the live board actually renders:

  A  trigger a one-shot result → GET /api/last-result returns the stored record AND the very
     same record is rendered as the board's dismissible strip (both surfaces read one store,
     and the GET is non-destructive — the strip survives repeated polls);
  B  AC5 regression verbatim: trigger → /api/last-result payload → dismiss → /api/last-result
     empty — and the board frame AND the next full GET / lose the strip with it;
  C  BOTH dismiss entry points clear the shared store: /api/dismiss-result (what the board's
     delegated JS click handler posts) empties GET /api/last-result, and the EU-712 endpoint
     empties the board;
  D  AC1 "styled by its tone" end-to-end on the board frame: ok/error/warn each render the
     strip with their own colour tokens (EU-701's neutral warn branch included);
  E  AC3 relative timestamp: the strip carries an "Xs ago" string regenerated server-side on
     every board frame — age the stored epoch and the NEXT poll frame (no page reload) shows
     the aged relative string;
  F  AC4: a pending result renders exactly once on a full GET / (board-embedded, no bar copy)
     and dismissing via the EU-712 endpoint keeps the next full GET / clean.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import time
import types
from pathlib import Path

# ── SDK / network stubs (same pattern as eu673_integration_test.py) ───────────
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


print("\n================ EU-715 Integration Verify & Close ================")

tmp = Path(tempfile.mkdtemp())
cfg = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(tmp / "app"), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(tmp / "audit.jsonl"),
    use_worktree=False,
)
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()

STRIP_BTN = "<button data-dismiss-result"


def _clear() -> None:
    """Remove every last_result key from BOTH scopes."""
    for st in (server._state, cockpit_state.get_state("automatixy")):
        st.pop("last_result", None)
        st.pop("last_result_record", None)


def _board() -> str:
    return client.get("/api/board?app=automatixy").get_data(as_text=True)


def _api() -> dict:
    return json.loads(client.get("/api/last-result?app=automatixy").get_data(as_text=True))


def _strip_open_tag(html: str, msg: str) -> str:
    """The strip's own opening <div ...> — the nearest 'background:var(--' div before *msg*.
    Every test uses a unique message, so the strip div is the one this isolates; colour-token
    assertions then can't false-match on an unrelated board element."""
    seg = html[:html.index(msg)]
    i = seg.rfind("<div style='background:var(--")
    return seg[i:] if i >= 0 else ""


# ===========================================================================
# Test A — one store, two surfaces: /api/last-result and the board frame agree
# ===========================================================================
print("\n--- Test A: endpoint payload and board strip read the same store ---")
_clear()
server.set_last_result("automatixy", "ok", "A: build succeeded on DEV")

payload = _api()
chk("A1: GET /api/last-result returns the stored tone/text/timestamp",
    payload.get("tone") == "ok" and payload.get("text") == "A: build succeeded on DEV"
    and isinstance(payload.get("timestamp"), (int, float)),
    f"got {payload}")

board_html = _board()
chk("A2: the same record renders as the board's dismissible strip",
    "A: build succeeded on DEV" in board_html and STRIP_BTN in board_html)

chk("A3: polling the endpoint is non-destructive (strip survives repeated GETs)",
    _api().get("text") == "A: build succeeded on DEV"
    and _api().get("text") == "A: build succeeded on DEV"
    and "A: build succeeded on DEV" in _board())

# ===========================================================================
# Test B — AC5 regression verbatim: trigger → payload → dismiss → empty
# ===========================================================================
print("\n--- Test B: /api/last-result lifecycle through the EU-712 endpoints ---")
r = client.post("/api/last-result/dismiss?app=automatixy")
chk("B1: POST /api/last-result/dismiss → 200 {ok: true}",
    r.status_code == 200 and json.loads(r.get_data(as_text=True)) == {"ok": True},
    f"{r.status_code} {r.get_data(as_text=True)[:80]}")
chk("B2: subsequent GET /api/last-result is empty", _api() == {}, f"got {_api()}")
chk("B3: the board frame lost the strip with the store entry",
    "A: build succeeded on DEV" not in _board())
page = client.get("/?app=automatixy").get_data(as_text=True)
chk("B4: the next FULL GET / does not bring it back (AC2)",
    "A: build succeeded on DEV" not in page and STRIP_BTN not in page)

# ===========================================================================
# Test C — both dismiss entry points clear the ONE shared store
# ===========================================================================
print("\n--- Test C: /api/dismiss-result (board JS) and /api/last-result/dismiss agree ---")
_clear()
server.set_last_result("automatixy", "error", "C: run failed on DEV")
r = client.post("/api/dismiss-result?app=automatixy")   # what the board's JS handler posts
chk("C1: board-JS endpoint /api/dismiss-result empties GET /api/last-result",
    r.status_code == 200 and _api() == {})

server.set_last_result("automatixy", "error", "C2: run failed again")
client.post("/api/last-result/dismiss?app=automatixy")  # the EU-712 endpoint
chk("C2: EU-712 endpoint empties the board frame too", "C2: run failed again" not in _board())

# ===========================================================================
# Test D — AC1 "styled by its tone", end-to-end on the board frame
# ===========================================================================
print("\n--- Test D: tone colours on the rendered strip (incl. EU-701 warn) ---")
for tone, msg, bg, fg in (
        ("ok",    "D: ok outcome",    "var(--okbg)",   "var(--ok)"),
        ("error", "D: error outcome", "var(--badbg)",  "var(--bad)"),
        ("warn",  "D: warn outcome",  "var(--warnbg)", "var(--warn)")):
    _clear()
    server.set_last_result("automatixy", tone, msg)
    html = _board()
    tag = _strip_open_tag(html, msg)
    chk(f"D({tone}): strip styled with {bg}/{fg} tokens",
        msg in html and f"background:{bg}" in tag and f"color:{fg};" in tag,
        f"strip tag: {tag[:90]!r}")
    chk(f"D({tone}): endpoint reports the same tone", _api().get("tone") == tone)
_clear()

# ===========================================================================
# Test E — AC3 relative timestamp regenerates per frame, no page reload
# ===========================================================================
print("\n--- Test E: relative time string updates across board frames ---")
_clear()
server.set_last_result("automatixy", "ok", "E: shipped to DEV")
frame1 = _board()
rel_in_strip = re.compile(r'margin-right:10px">\d+(?:s|m|min|h)')
chk("E1: fresh strip carries a relative 'ago' timestamp",
    bool(rel_in_strip.search(_strip_open_tag(frame1, "E: shipped to DEV")
                             + frame1[frame1.index("E: shipped to DEV"):][:200])),
    "no '…ago' span found in the strip")

# Age the stored epoch ~2 minutes; the NEXT poll frame (no reload) must re-render
# the relative string server-side from the SAME record — proving it is not frozen.
cockpit_state.get_state("automatixy")["last_result_record"]["timestamp"] = time.time() - 125
frame2 = _board()
seg2 = frame2[:frame2.index("E: shipped to DEV")]
chk("E2: next board frame shows the aged relative time (server-side regen, no reload)",
    bool(re.search(r'margin-right:10px">\d+min? ago</span>', seg2)),
    f"strip ts span: {seg2[seg2.rfind('margin-right'):][:60]!r}")
chk("E3: the stored record survived both renders (nothing popped it)",
    isinstance(cockpit_state.get_state("automatixy").get("last_result_record"), dict))
_clear()

# ===========================================================================
# Test F — AC4: full GET / renders the strip exactly once; EU-712 dismiss keeps
#          the next full page clean
# ===========================================================================
print("\n--- Test F: full-page dedup + endpoint dismiss persists across GET / ---")
_clear()
server.set_last_result("automatixy", "warn", "F: no backlog configured")
page1 = client.get("/?app=automatixy").get_data(as_text=True)
chk("F1: pending result renders exactly once on GET / (no bar+board double-render)",
    "F: no backlog configured" in page1 and page1.count(STRIP_BTN) == 1,
    f"button occurs {page1.count(STRIP_BTN)}×")
chk("F2: the page and the endpoint agree on the pending record",
    _api().get("text") == "F: no backlog configured")
client.post("/api/last-result/dismiss?app=automatixy")
page2 = client.get("/?app=automatixy").get_data(as_text=True)
chk("F3: EU-712 dismiss keeps the next full GET / strip-free",
    "F: no backlog configured" not in page2 and STRIP_BTN not in page2)
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
