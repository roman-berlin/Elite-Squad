"""EU-672 — Regression guard: one-shot QA result appears on the live board BEFORE any GET /.

Acceptance criteria covered:
  AC(a)/Test A: Call ``set_last_result`` directly, then call ``render_board`` (or hit
      ``/api/board``) — assert the message text is present in the returned HTML.
      No ``GET /`` is performed anywhere in this test.
  AC(b)/Test B: Dismiss via ``POST /api/dismiss-result``, then re-render the board
      and assert the message is now ABSENT.
  AC(c)/Test C: Write a result, render the board (still pending, not dismissed), then
      simulate a full ``GET /`` and assert the message is STILL present
      (no regression to "disappears before the user sees it").
"""
from __future__ import annotations

import inspect
import json
import sys
import tempfile
import types
from pathlib import Path

# ── SDK / network stubs (same pattern as existing cockpit tests) ────────────────
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


print("\n================ EU-672 Live-Result Strip Guard ================")

# ── harness: live Flask test client + config ────────────────────────────────────
tmp = Path(tempfile.mkdtemp())
cfg = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(tmp / "app"), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(tmp / "audit.jsonl"),
    use_worktree=False,
)
cfg.detected_auth = lambda: "test"

client = server.create_app(cfg).test_client()

MSG = "✓ QA finished for automatixy"


def _seed() -> None:
    """Place a pending result into BOTH unit-wide and per-project state."""
    server._state.pop("last_result", None)
    server._state.pop("last_result_record", None)
    app_st = cockpit_state.get_state("automatixy")
    app_st.pop("last_result", None)
    app_st.pop("last_result_record", None)
    server.set_last_result("automatixy", "ok", MSG)


def _clear() -> None:
    """Remove all last_result keys everywhere."""
    server._state.pop("last_result", None)
    server._state.pop("last_result_record", None)
    app_st = cockpit_state.get_state("automatixy")
    app_st.pop("last_result", None)
    app_st.pop("last_result_record", None)


# ===========================================================================
# Test A — direct call: set_last_result → render_board → message PRESENT
# No GET / is performed anywhere in this test.
# ===========================================================================
print("\n--- Test A: direct render_board after set_last_result ---")
_clear()

# Seed via the real writer (exactly how QA verdict writes)
server.set_last_result("automatixy", "ok", MSG)

# Directly hit the board endpoint (no GET / ever issued)
board_html = client.get("/api/board?app=automatixy").get_data(as_text=True)

chk("A1: message text visible in /api/board HTML",
    MSG in board_html,
    f"'{MSG}' in response ({len(board_html)} chars)")
chk("A2: dismiss button present in board HTML",
    'data-dismiss-result' in board_html)

# Also verify via the render_board function directly (not just the route)
from orchestrator.warroom import render_board

_clear()
server.set_last_result("automatixy", "ok", MSG)
direct_html = render_board(cfg, "automatixy",
                           cockpit_state.get_state("automatixy"))

chk("A3: render_board() directly also shows the message",
    MSG in direct_html,
    f"'{MSG}' in render_board output")
# "No GET / inside render_board" verified by inspecting the REAL function source
# (a literal True here is a vacuous assertion — BUILD_DOCTRINE §3 / guard CLASS B).
_board_src = inspect.getsource(render_board)
_http_tokens = ("requests.", "urlopen", "urllib", "http.client",
                "client.get(", "client.post(")
_leaked = [tok for tok in _http_tokens if tok in _board_src]
chk("A4: render_board source issues no HTTP calls (GET / or otherwise)",
    not _leaked,
    f"HTTP tokens found in render_board source: {_leaked}")


# ===========================================================================
# Test B — dismiss via POST /api/dismiss-result → message ABSENT
# ===========================================================================
print("\n--- Test B: dismiss → re-render → message ABSENT ---")
_clear()
server.set_last_result("automatixy", "ok", MSG)

# Confirm strip was present first
pre_dismiss = client.get("/api/board?app=automatixy").get_data(as_text=True)
chk("B0 pre: strip present before dismiss",
    MSG in pre_dismiss and 'data-dismiss-result' in pre_dismiss)

# Dismiss
r = client.post("/api/dismiss-result?app=automatixy")
chk("B1: POST /api/dismiss-result returns 200 {ok: true}",
    r.status_code == 200 and json.loads(r.get_data(as_text=True)) == {"ok": True},
    f"{r.status_code} {r.get_data(as_text=True)[:80]}")

# Re-render board — message must be ABSENT
post_dismiss = client.get("/api/board?app=automatixy").get_data(as_text=True)
chk("B2: message text ABSENT after dismiss",
    MSG not in post_dismiss,
    f"'{MSG}' found in post-dismiss board (BAD!)")
chk("B3: dismiss button ABSENT after dismiss",
    'data-dismiss-result' not in post_dismiss)

# Idempotent: double-dismiss should also succeed
r2 = client.post("/api/dismiss-result?app=automatixy")
chk("B4: dismiss is idempotent (second call → 200)",
    r2.status_code == 200 and json.loads(r2.get_data(as_text=True)).get("ok") is True)


# ===========================================================================
# Test C — write result → render board (pending) → simulate GET / → message
#          STILL PRESENT (regression guard for EU-676 fix).
#
# The historical bug: index() called _result_banner() which POP'd last_result
# on every load, so doing a GET / would consume the one-shot and erase it
# from the board render path. EU-676 changed index() to _result_strip()
# (non-destructive). This test verifies that change holds.
# ===========================================================================
print("\n--- Test C: pending result survives a full GET / ---")
_clear()
server.set_last_result("automatixy", "ok", MSG)

# Step 1: Render the board while result is still pending
board_pending = client.get("/api/board?app=automatixy").get_data(as_text=True)
chk("C0a: board shows result while pending",
    MSG in board_pending)

# Step 2: Simulate a full GET / — this is what used to consume/pop the banner
page = client.get("/").get_data(as_text=True)
chk("C0b: full page loaded successfully (no crash)",
    page.startswith("<!DOCTYPE html") or "<html" in page.lower() or len(page) > 100,
    f"page length: {len(page)}")

# Step 3: Hit the board AGAIN — the message must STILL be present
# If EU-676's non-destructive fix regressed, the GET / would have popped
# last_result and the board would be empty.
board_after_get = client.get("/api/board?app=automatixy").get_data(as_text=True)
chk("C1: message STILL PRESENT after GET / (EU-676 non-destructive guard)",
    MSG in board_after_get and 'data-dismiss-result' in board_after_get,
    f"'{MSG}' missing after GET / — REGRESSION!")

# Step 4: Verify the state actually survived GET / (the peek didn't pop anything)
final_state = cockpit_state.get_state("automatixy")
chk("C2: last_result_record survived GET / (state intact)",
    final_state.get("last_result_record") is not None,
    repr(final_state.get("last_result_record")))

# Step 5: Verify the home page itself ALSO renders the strip (non-destructive at index level)
chk("C3: home page also shows the result strip",
    MSG in page,
    f"'{MSG}' not in home page HTML")


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
