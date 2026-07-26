"""EU-656 — _result_banner and the control-bar note render from STORED TONE only.

Both cockpit consumers used to guess tone by substring-checking the message text —
which flagged success text containing "error" as a failure and painted "0 failed"
summaries red. The fix: writers store a structured (tone, text, timestamp) record
via set_last_result / set_last_msg(app, tone, text) — an EXPLICIT tone, mirrored
signatures, zero substring inference at the write site OR the read site — and the
renderers read that record only.

Coverage:
  1. _result_banner reads last_result_record["tone"] exclusively (AC a/b).
  2. set_last_msg(app, tone, text): explicit tone, validated like set_last_result,
     no auto-detection helper left anywhere.
  3. The control-bar note is rendered through _control_bar (the real render path,
     not a tautological record read-back):
       AC(c) — "0 failed" with tone "ok"  → class "tbnote ok",  NOT "tbnote bad"
       AC(a) — success text with "error"  → class "tbnote ok",  NOT "tbnote bad"
       AC(b) — a real failure (tone "error") → class "tbnote bad"
     plus legacy-writer (no record → neutral dim) and stale-record-after-clear guards.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import autopilot as _ap_mod
from orchestrator.cockpit_state import get_state, reset_workspaces
from orchestrator.cockpit_views import _control_bar, _result_banner
from orchestrator.config import AppConfig, Config
import orchestrator.server as server

# Isolate the autopilot liveness probe (matches the suite-wide EU-355 env pin) so a
# live drain on this machine can't flip the control bar's Autopilot section mid-test.
_ap_mod._PID_FILE = Path(tempfile.mkdtemp()) / "general-autopilot.pid"

results = []


def chk(name, cond, detail=""):
    results.append((name, bool(cond), str(detail)))


_d = Path(tempfile.mkdtemp())
(_d / "audit.jsonl").write_text("")
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(_d), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(_d / "audit.jsonl"), use_worktree=False)


def _reset():
    """Deterministic slate: reset_workspaces only drops tab state, so also clear the
    message keys + run flag the renderers read (the harness is one process start to finish)."""
    reset_workspaces()
    st = get_state(None)
    st["last_msg"] = ""
    st["last_msg_record"] = None
    st["last_result"] = ""
    st["last_result_record"] = None
    st["active"] = False


# ── 1. _result_banner — tone from last_result_record ONLY ─────────────────────
_reset()
server.set_last_result(None, "ok", "No build error detected on PR #42")
b = _result_banner(server._state)
chk("AC(a) banner: ok-tone text containing 'error' renders green",
    "var(--ok)" in b and "var(--bad)" not in b, b[:180])

_reset()
server.set_last_result(None, "error", "Deploy failed: conflict")
b = _result_banner(server._state)
chk("AC(b) banner: real failure (tone error) renders red",
    "var(--bad)" in b and "var(--ok)" not in b, b[:180])

_reset()
server.set_last_result(None, "warn", "Slow CI — rerun recommended")
b = _result_banner(server._state)
chk("banner: warn tone is not red", "var(--bad)" not in b, b[:180])

_reset()
server._state["last_result"] = "plain legacy text mentioning error"
server._state["last_result_record"] = None
b = _result_banner(server._state)
chk("banner: missing record defaults to ok — zero substring fallback",
    "var(--ok)" in b and "var(--bad)" not in b, b[:180])

_reset()
server.set_last_result(None, "ok", "shipped")
_result_banner(server._state)
chk("banner: still one-shot (pops last_result)",
    server._state.get("last_result", "") == "", repr(server._state.get("last_result")))


# ── 2. set_last_msg(app, tone, text) — explicit tone, mirroring set_last_result ─
_reset()
before = __import__("time").time()
server.set_last_msg("appx", "ok", "build succeeded")
st = get_state("appx")
rec = st.get("last_msg_record") or {}
chk("set_last_msg: plain text stored for legacy readers",
    st["last_msg"] == "build succeeded", repr(st["last_msg"]))
chk("set_last_msg: structured record stored alongside",
    rec.get("tone") == "ok" and rec.get("text") == "build succeeded"
    and isinstance(rec.get("timestamp"), float) and rec["timestamp"] >= before - 1,
    repr(rec))

_reset()
server.set_last_msg(None, "ok", "0 failed")
chk("set_last_msg: writer-provided tone wins over text ('0 failed' stays ok)",
    (get_state(None).get("last_msg_record") or {}).get("tone") == "ok",
    repr(get_state(None).get("last_msg_record")))

_reset()
server.set_last_msg(None, "error", "standup failed: timeout")
chk("set_last_msg: error tone round-trips",
    (get_state(None).get("last_msg_record") or {}).get("tone") == "error")

_reset()
server.set_last_msg(None, "warn", "⚠ heads up")
chk("set_last_msg: warn tone round-trips",
    (get_state(None).get("last_msg_record") or {}).get("tone") == "warn")

try:
    server.set_last_msg("badtone", "info", "x")
    chk("set_last_msg: invalid tone raises ValueError", False, "no exception raised")
except ValueError as e:
    chk("set_last_msg: invalid tone raises ValueError",
        "invalid last_msg tone" in str(e), str(e))
chk("set_last_msg: invalid tone leaves state untouched",
    get_state("badtone").get("last_msg", "") == ""
    and get_state("badtone").get("last_msg_record") is None,
    repr(get_state("badtone").get("last_msg")))

chk("no substring-inference helper remains at the write site",
    not hasattr(server, "_detect_last_msg_tone"))


# ── 3. Control-bar note — rendered through _control_bar, read from the record ──
def _bar():
    return _control_bar(cfg, "automatixy", healthy=True)


_reset()
server.set_last_msg(None, "ok", "0 failed")
bar = _bar()
chk("AC(c): '0 failed' with tone ok renders 'tbnote ok' (green/neutral)",
    'class="tbnote ok"' in bar and "0 failed" in bar, bar[bar.find("tbnote") - 40:bar.find("tbnote") + 60])
chk("AC(c): '0 failed' with tone ok does NOT render 'tbnote bad'",
    'class="tbnote bad"' not in bar)

_reset()
server.set_last_msg(None, "ok", "No error found — build passed")
bar = _bar()
chk("AC(a) note: success text containing 'error' renders 'tbnote ok'",
    'class="tbnote ok"' in bar and 'class="tbnote bad"' not in bar)

_reset()
server.set_last_msg(None, "error", "standup failed: timeout")
bar = _bar()
chk("AC(b) note: real failure renders 'tbnote bad'",
    'class="tbnote bad"' in bar and 'class="tbnote ok"' not in bar)

_reset()
server.set_last_msg(None, "error", "autopilot error: boom")
bar = _bar()
chk("failure write-site class (autopilot error) renders red end-to-end",
    'class="tbnote bad"' in bar)

_reset()
server.set_last_msg(None, "warn", "a run is already in progress — wait for it to finish")
bar = _bar()
chk("warn note renders neutral dim (not red, not green)",
    'class="tbnote dim"' in bar and 'class="tbnote bad"' not in bar
    and 'class="tbnote ok"' not in bar)

_reset()
server._state["last_msg"] = "legacy note mentioning error"
server._state["last_msg_record"] = None
bar = _bar()
chk("legacy writer without record renders dim — never red, never text-guessed",
    'class="tbnote dim"' in bar and 'class="tbnote bad"' not in bar)

_reset()
server.set_last_msg(None, "error", "boom")
server._state["last_msg"] = ""          # a run's _bg clears the note; the record lingers
bar = _bar()
chk("cleared last_msg renders no note even with a stale error record",
    'class="tbnote bad"' not in bar and "boom" not in bar)


# ── tally ─────────────────────────────────────────────────────────────────────
print("\n======== EU-656 TONE-FROM-RECORD QA ========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {name}"
    if det and not ok:
        line += f"  ({det})"
    print(line)
print("--------------------------------------------")
print(f"  {passed}/{len(results)} passed")
if passed != len(results):
    print(f"  RESULT: {len(results) - passed} FAIL")
    sys.exit(1)
print("  RESULT: ALL GREEN")
sys.exit(0)
