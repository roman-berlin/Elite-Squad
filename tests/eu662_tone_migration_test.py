"""Tests for EU-662: integration verification of explicit-tone migration.

Covers all four original acceptance criteria end-to-end:
1. All three sites use set_last_result with tone "error" on failure.
2. No substring/text-content inference remains at these sites.
3. Regression: failure produces stored tone=="error", distinct from "ok".
4. _result_banner reads tone from last_result_record (not substring).
Plus: python3 tests/run_all.py stays green.
"""
import json
import sys
import tempfile
import time
import types
import inspect
from pathlib import Path

sys.path.insert(0, ".")

# --- Minimal module stubs so imports succeed without network / real models ---
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

req = types.ModuleType("requests")
req.Session = lambda *a, **k: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules["requests"] = req

# --- Real imports after stubs installed ---
from orchestrator.cockpit_state import get_state, reset_workspaces  # noqa: E402
import orchestrator.server as srv  # noqa: E402
from orchestrator.cockpit_views import _result_banner  # noqa: E402

results: list[tuple[str, bool, str]] = []

def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ═══════════════════════════════════════════════════════════════════════════
# AC1 (standup/council): failure → tone == "error", text starts with site prefix
# AC2 (scribe): failure → tone "error"; success → tone "ok"
# (These overlap with eu660/eu661; re-checked here for full coverage.)
# ═══════════════════════════════════════════════════════════════════════════

# Import council early (before monkeypatch) — same pattern as eu660
from orchestrator import council  # noqa: E402
from orchestrator import memory  # noqa: E402
from orchestrator.config import AppConfig, Config  # noqa: E402

tmp = Path(tempfile.mkdtemp())
AUDIT = tmp / "audit.jsonl"
AUDIT.write_text("", encoding="utf-8")

cfg = Config(apps=[AppConfig(name="alpha", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(AUDIT), use_worktree=False)
cfg.detected_auth = lambda: "test"
srv.health.summary = lambda c: {"healthy": True, "checks": []}


def _reset():
    for k in ("standuping", "councilling", "scribing", "meeting", "patrolling",
              "grouping", "shipreview", "qa"):
        srv._state.pop(k, None)
    srv._state.pop("last_result", None)
    srv._state.pop("last_result_record", None)
    srv._state.pop("last_msg", None)
    reset_workspaces()


class _SyncThread:
    """Runs target inline on .start() — deterministic visibility."""
    def __init__(s, target=None, daemon=None, **kw): s.t = target
    def start(s):
        if s.t:
            s.t()

_srv_threading_swap = srv.threading
del srv.threading
srv.threading = types.SimpleNamespace(Thread=_SyncThread, Event=__import__("threading").Event,
                                     Lock=__import__("threading").Lock)

app = srv.create_app(cfg)
client = app.test_client()

# -- AC1: standup failure ────────────────────────────────────────────────
_reset()
_raise = lambda cfg, audit=None: exec("raise RuntimeError('boom')") or None
_orig_su = getattr(council, "hold_standup", None)
async def _su_fail(_cfg, audit=None):
    raise RuntimeError("boom — standup stub")
council.hold_standup = _su_fail

r = client.post("/api/standup", data={})
chk("AC1a: POST /api/standup redirects", r.status_code == 302, f"{r.status_code}")
rec = get_state(None).get("last_result_record", {})
chk("AC1b: standup tone='error'", rec.get("tone") == "error", repr(rec.get("tone")))
chk("AC1c: standup text starts with 'standup failed:'",
    str(rec.get("text", "")).startswith("standup failed:"), repr(rec.get("text")))

# -- AC1: council failure ────────────────────────────────────────────────
_reset()
_orig_co = getattr(council, "hold_council", None)
async def _co_fail(_cfg, topic=None, audit=None, *, broadcast=None):
    raise RuntimeError("boom — council stub")
council.hold_council = _co_fail

r = client.post("/api/council", data={})
chk("AC1d: POST /api/council redirects", r.status_code == 302, f"{r.status_code}")
rec = get_state(None).get("last_result_record", {})
chk("AC1e: council tone='error'", rec.get("tone") == "error", repr(rec.get("tone")))
chk("AC1f: council text starts with 'council failed:'",
    str(rec.get("text", "")).startswith("council failed:"), repr(rec.get("text")))

council.hold_standup = _orig_su
council.hold_council = _orig_co

# -- Restore real threading for scribe (uses async + thread) ─────────────
del srv.threading
srv.threading = _srv_threading_swap

# -- AC2: scribe failure → tone "error" ─────────────────────────────────
_reset()
_async_orig = memory.scribe
async def _mem_fail(_cfg):
    raise RuntimeError("scribe boom")
memory.scribe = _mem_fail

async def _wait_scribe(timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not srv._state.get("scribing"):
            return True
        await asyncio.sleep(0.05)  # type: ignore[name-defined]
    return False

import asyncio  # noqa: E402

# Directly invoke scribe via test client — it starts a background thread
r = client.post("/api/scribe")
assert r.status_code in (301, 302, 303)
# Wait for background thread to finish (sync-friendly since we're single-threaded here)
for _ in range(100):
    if not srv._state.get("scribing"):
        break
    time.sleep(0.05)

rec = get_state(None).get("last_result_record", {})
chk("AC2a: scribe failure tone='error'", rec.get("tone") == "error", repr(rec.get("tone")))
chk("AC2b: scribe failure text starts with 'scribe failed:'",
    str(rec.get("text", "")).startswith("scribe failed:"), repr(rec.get("text")))

memory.scribe = _async_orig

# -- AC3: regression — distinct error vs ok records, never merged ────────
_reset()
memory.scribe = _async_orig  # will be swapped below

orig_mem = memory.scribe

async def _mem_ok(_cfg):
    return "folded 1 lesson"

async def _mem_fail2(_cfg):
    raise RuntimeError("second boom")

# Success then failure
memory.scribe = _mem_ok
r = client.post("/api/scribe")
for _ in range(100):
    if not srv._state.get("scribing"):
        break
    time.sleep(0.05)
chk("AC3a: after success tone='ok'",
    get_state(None).get("last_result_record", {}).get("tone") == "ok")

_reset()
memory.scribe = _mem_fail2
r = client.post("/api/scribe")
for _ in range(100):
    if not srv._state.get("scribing"):
        break
    time.sleep(0.05)
rec = get_state(None).get("last_result_record", {})
chk("AC3b: after failure tone='error' (overwrites)",
    rec.get("tone") == "error", repr(rec.get("tone")))
chk("AC3c: error record has ONLY failure text (no merge)",
    "failed" in str(rec.get("text", "")) and "boom" in str(rec.get("text", "")))
chk("AC3d: no stale success text in error record",
    "folded" not in str(rec.get("text", "")), repr(rec.get("text")))

# Failure then success (reverse order)
_reset()
memory.scribe = _mem_fail2
r = client.post("/api/scribe")
for _ in range(100):
    if not srv._state.get("scribing"):
        break
    time.sleep(0.05)
chk("AC3e: after failure tone='error'",
    get_state(None).get("last_result_record", {}).get("tone") == "error")

_reset()
memory.scribe = _mem_ok
r = client.post("/api/scribe")
for _ in range(100):
    if not srv._state.get("scribing"):
        break
    time.sleep(0.05)
rec = get_state(None).get("last_result_record", {})
chk("AC3f: after success tone='ok' (overwrites)",
    rec.get("tone") == "ok", repr(rec.get("tone")))

memory.scribe = orig_mem


# ═══════════════════════════════════════════════════════════════════════════
# AC4: _result_banner uses tone from last_result_record (NOT substring)
# ═══════════════════════════════════════════════════════════════════════════

_reset()
# Tone='ok' but text contains the word 'failure' → should render GREEN (OK styling)
srv._state["last_result"] = "There was a failure in processing."
srv._state["last_result_record"] = {"tone": "ok", "text": "There was a failure in processing.",
                                     "timestamp": time.time()}
banner = _result_banner(srv._state)
chk("AC4a: tone='ok' overrides 'failure' in text → green CSS",
    "--okbg" in banner and "--okline" in banner, f"banner missing green tone: {banner[:200]}")
chk("AC4b: tone='ok' does NOT use red error CSS",
    "--badbg" not in banner and "--badline" not in banner, f"banner incorrectly has red: {banner[:200]}")
chk("AC4c: text still present in banner", "There was a failure" in banner,
    f"text missing: {banner[:200]}")

_reset()
# Tone='error', text contains 'success' → should render RED
srv._state["last_result"] = "The task succeeded but config was wrong."
srv._state["last_result_record"] = {"tone": "error", "text": "The task succeeded but config was wrong.",
                                     "timestamp": time.time()}
banner = _result_banner(srv._state)
chk("AC4d: tone='error' overrides 'succeeded' in text → red CSS",
    "--badbg" in banner and "--badline" in banner, f"banner missing red: {banner[:200]}")
chk("AC4e: tone='error' does NOT use green OK CSS",
    "--okbg" not in banner and "--okline" not in banner, f"banner incorrectly has green: {banner[:200]}")

_reset()
# EU-656: zero substring fallback — legacy writer without record defaults to ok tone.
srv._state["last_result"] = "An unexpected error occurred during processing."
srv._state.pop("last_result_record", None)
banner = _result_banner(srv._state)
chk("AC4f: legacy (no record) defaults to ok/green — no substring fallback per EU-656",
    "--okbg" in banner and "--okline" in banner, f"legacy fallback wrong: {banner[:200]}")
chk("AC4g: legacy pop removes last_result",
    srv._state.get("last_result", "") == "", "last_result not popped")


# ═══════════════════════════════════════════════════════════════════════════
# AC5 (subset of original AC2): no substring/text-content inference at sites
# Source assertion on server.py routes
# ═══════════════════════════════════════════════════════════════════════════

standup_src = inspect.getsource(app.view_functions["standup_api"])
council_src = inspect.getsource(app.view_functions["council_api"])
# For scribe, reload fresh because we restored threading; recreate app
srv2 = types.SimpleNamespace(Thread=_SyncThread, Event=__import__("threading").Event,
                            Lock=__import__("threading").Lock)
import types as _t
del srv.threading
srv.threading = srv2
app_scribe = srv.create_app(cfg)
scribe_src = inspect.getsource(app_scribe.view_functions["scribe_api"])

# Check each handler source: must contain set_last_result with "error" tone
chk("AC5a: standup_api has set_last_result(..., 'error', ...)",
    'set_last_result(None, "error"' in standup_src,
    "missing error set_last_result in standup")
chk("AC5b: council_api has set_last_result(..., 'error', ...)",
    'set_last_result(None, "error"' in council_src,
    "missing error set_last_result in council")
chk("AC5c: scribe_api has set_last_result(..., 'error', ...)",
    'set_last_result(None, "error"' in scribe_src,
    "missing error set_last_result in scribe")

# No bare _state["last_msg"] in any of the three routes
has_sm_st = '_state["last_msg"]' in standup_src or "_state['last_msg']" in standup_src
has_sm_co = '_state["last_msg"]' in council_src or "_state['last_msg']" in council_src
has_sm_sb = '_state["last_msg"]' in scribe_src or "_state['last_msg']" in scribe_src
chk("AC5d: no bare _state[last_msg] in standup_api", not has_sm_st)
chk("AC5e: no bare _state[last_msg] in council_api", not has_sm_co)
chk("AC5f: no bare _state[last_msg] in scribe_api", not has_sm_sb)


# ═══════════════════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════════════════
print("\n============ EU-662 Tone Migration Integration QA ======")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------")
print(f"  {passed}/{len(results)} passed")
if passed != len(results):
    print(f"  RESULT: {len(results)-passed} FAIL")
    sys.exit(1)
else:
    print("  RESULT: ALL GREEN")
    sys.exit(0)
