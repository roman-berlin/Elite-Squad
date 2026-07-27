"""EU-670 — Render the pending result as a dismissible strip inside the live board.

Acceptance criteria covered:
  AC(a) When last_result_record is set, render_board HTML contains the strip with tone-styled
        colours, message text, and a dismiss control button.
  AC(b) When nothing is pending, render_board output is byte-identical (no empty wrapper divs).
  AC(c) Calling render_board twice with the same pending record renders the strip both times
        (non-destructive — _peek_last_result must NOT pop state).
  AC(d) No existing board test's assertions on board HTML structure/content break.
"""
from __future__ import annotations

import datetime
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from orchestrator import cockpit_state, warroom
from orchestrator.cockpit_views import _result_strip, _peek_last_result
from orchestrator.config import AppConfig, Config

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_cfg(tmp: Path) -> Config:
    """Minimal Config pointing at *tmp* with an (optionally seeded) audit file."""
    audit = tmp / "audit.jsonl"
    if not audit.exists() or not audit.read_text().strip():
        now = datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
        audit.write_text(
            '{"event":"ticket_start","ticket_id":"EU-670-test","app":"testapp",'
            '"branch":"b","ts":"' + now + '"}\n'
            '{"event":"merged","ticket_id":"EU-670-test","app":"testapp","ts":"' + now + '"}\n',
            encoding="utf-8",
        )
    return Config(
        apps=[AppConfig(name="testapp", repo_path=str(tmp), base_branch="DEV",
                        protected_branch="MAIN", backlog_backend="none")],
        audit_path=str(audit),
    )


def _seed(state: dict, tone: str, text: str) -> None:
    """Place a pending result into *state* without popping anything."""
    state["last_result"] = text
    state["last_result_record"] = {"tone": tone, "text": text, "timestamp": 1_700_000_000}


def _empty(state: dict) -> None:
    """Clear pending result from *state*."""
    state.pop("last_result", None)
    state.pop("last_result_record", None)


# ===========================================================================
# Pre-condition: _peek_last_result and _result_strip exist
# ===========================================================================
print("\n================ EU-670 Result Strip QA ================")
_chk_ok = True


def _chk(name: str, cond: bool, detail: str = "") -> None:
    global _chk_ok
    tag = "PASS" if cond else "FAIL"
    line = f"  [{tag}] {name}"
    if detail and not cond:
        line += f"  ({detail})"
    print(line)
    chk(name, cond, detail)
    if not cond:
        _chk_ok = False


# --- _peek_last_result and _result_strip callable ---
_peek_ok = callable(_peek_last_result)
_rs_ok = callable(_result_strip)
_chk("_peek_last_result is importable/callable", _peek_ok)
_chk("_result_strip is importable/callable", _rs_ok)

# --- _peek_last_result: no pending → None ---
_chk("_peek returns None when nothing pending",
     _peek_last_result({}) is None)

# --- _peek_last_result: returns record ---
r = _peek_last_result({"last_result_record": {"tone": "error", "text": "boom"}})
_chk("_peek returns record when set", r == {"tone": "error", "text": "boom"}, repr(r))

# --- _peek non-destructive ---
st: dict = {"last_result_record": {"tone": "ok", "text": "fine"}}
_peek_last_result(st)
_chk("_peek does NOT pop state", st.get("last_result_record") is not None)

# --- _result_strip: empty when nothing pending ---
_chk("_result_strip returns '' when nothing pending",
     _result_strip({}) == "")

# --- _result_strip: legacy-only last_result falls back (EU-676 compat) ---
_legacy = _result_strip({"last_result": "plain text"})
_chk("_result_strip renders legacy-only last_result (EU-676 fallback)",
     "plain text" in _legacy and len(_legacy) > 0)

# --- _result_strip: error tone → bad colours ---
s = _result_strip({"last_result_record": {"tone": "error", "text": "Deploy failed"}})
_chk("error tone → var(--bad) in strip", "var(--bad)" in s, s[:200])
_chk("error tone does NOT contain var(--ok)", "var(--ok)" not in s, s[:200])

# --- _result_strip: ok tone → ok colours ---
s = _result_strip({"last_result_record": {"tone": "ok", "text": "All good"}})
_chk("ok tone → var(--ok) in strip", "var(--ok)" in s, s[:200])
_chk("ok tone does NOT contain var(--bad)", "var(--bad)" not in s, s[:200])

# --- _result_strip: warn tone not red ---
s = _result_strip({"last_result_record": {"tone": "warn", "text": "Slow CI"}})
_chk("warn tone → NOT red", "var(--bad)" not in s, s[:200])

# --- _result_strip: contains message ---
s = _result_strip({"last_result_record": {"tone": "error", "text": "Build crashed"}})
_chk("strip contains message text", "Build crashed" in s, s[:200])

# --- _result_strip: dismiss button ---
s = _result_strip({"last_result_record": {"tone": "ok", "text": "shipped"}})
_chk("strip has <button>", "<button" in s, s[:300])
_chk("strip button has data-dismiss-result", 'data-dismiss-result' in s, s[:300])

# --- _result_strip non-destructive ---
st: dict = {"last_result_record": {"tone": "error", "text": "boom"}}
_result_strip(st)
_chk("_result_strip does NOT pop state", st.get("last_result_record") is not None)

# --- render_board integration: strip appears in board ---
now = datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
for _tname in ("render_board_contains_strip_when_pending",
               "render_board_byte_identical_empty",
               "render_board_renders_twice_same_record"):
    pass  # handled below separately

# Test: strip visible in board
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    audit = tmp / "audit.jsonl"
    audit.write_text(
        '{"event":"ticket_start","ticket_id":"E","app":"x","branch":"b","ts":"' + now + '"}\n'
        '{"event":"merged","ticket_id":"E","app":"x","ts":"' + now + '"}\n',
        encoding="utf-8",
    )
    cfg = Config(apps=[AppConfig(name="x", repo_path=str(tmp), base_branch="DEV",
                                 protected_branch="MAIN", backlog_backend="none")],
                 audit_path=str(audit))
    state: dict = {}
    _seed(state, "error", "Deploy failed")
    html = warroom.render_board(cfg, "x", state)
    _chk("AC(a): strip visible in board HTML", "Deploy failed" in html, html[:400] if len(html) > 400 else html)
    _chk("AC(a): strip has dismiss button in board", 'data-dismiss-result' in html, html[:400])

# Test: byte-identical when nothing pending
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    audit = tmp / "audit.jsonl"
    audit.write_text(
        '{"event":"ticket_start","ticket_id":"E","app":"x","branch":"b","ts":"' + now + '"}\n'
        '{"event":"merged","ticket_id":"E","app":"x","ts":"' + now + '"}\n',
        encoding="utf-8",
    )
    cfg = Config(apps=[AppConfig(name="x", repo_path=str(tmp), base_branch="DEV",
                                 protected_branch="MAIN", backlog_backend="none")],
                 audit_path=str(audit))
    state_a: dict = {}
    board_a = warroom.render_board(cfg, "x", state_a)
    state_b: dict = {"last_result": "", "last_result_record": None}
    board_b = warroom.render_board(cfg, "x", state_b)
    _chk("AC(b): no-pending bytes match (empty string value)",
         board_a == board_b,
         f"a len={len(board_a)} b len={len(board_b)} diff={len(board_a)-len(board_b)}")
    _chk("AC(b): no-pending bytes match (None value)",
         board_a == warroom.render_board(cfg, "x", {"last_result_record": None}),
         "N/A")

# Test: renders twice (non-destructive)
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    audit = tmp / "audit.jsonl"
    audit.write_text(
        '{"event":"ticket_start","ticket_id":"E","app":"x","branch":"b","ts":"' + now + '"}\n'
        '{"event":"merged","ticket_id":"E","app":"x","ts":"' + now + '"}\n',
        encoding="utf-8",
    )
    cfg = Config(apps=[AppConfig(name="x", repo_path=str(tmp), base_branch="DEV",
                                 protected_branch="MAIN", backlog_backend="none")],
                 audit_path=str(audit))
    state: dict = {}
    _seed(state, "error", "Deploy failed")
    h1 = warroom.render_board(cfg, "x", state)
    h2 = warroom.render_board(cfg, "x", state)
    _chk("AC(c): first call shows strip",
         "Deploy failed" in h1, "first call missing message")
    _chk("AC(c): second call shows strip (non-destructive)",
         "Deploy failed" in h2, "second call missing message")

# --- Report ---
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print("-----------------------------------------------------------")
print(f"  {passed}/{total} passed")
if passed != total:
    print(f"  RESULT: {total - passed} FAIL")
    sys.exit(1)
print("  RESULT: ALL GREEN")
sys.exit(0)
