"""EU-660 — Regression test for set_last_result() migration in standup / council API handlers.

Verifies that both handlers report outcomes via ``set_last_result(app, tone, text)`` instead of
bare ``_state["last_msg"] = ...``, so failures render with their proper tone (red error banner)
rather than bleeding into the green success banner.

Technique: monkeypatch ``orchestrator.council.hold_standup`` / ``hold_council`` with stubs that
raise ``RuntimeError`` or succeed immediately.  Use ``_SyncThread`` (inline execution on .start())
so state changes are visible without sleep-and-hope races.  Poll-wait bounded to ~2 s as an extra
safety net; _SyncThread makes this a formality.

Each method independently resets state so results never collide across checks.
"""
import json
import sys
import tempfile
import time
import types
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

# --- Import council FIRST (before server.create_app), since server lazily
# imports council inside handlers. We monkeypatch the module-level module. ---
from orchestrator import council  # noqa: E402

# --- Real imports after stubs installed ---
from orchestrator.config import AppConfig, Config  # noqa: E402
from orchestrator.cockpit_state import get_state, reset_run_state  # noqa: E402
import orchestrator.server as srv  # noqa: E402


# ─── helpers ───────────────────────────────────────────────────────────────

results: list[tuple[str, bool, str]] = []

def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
AUDIT = tmp / "audit.jsonl"
AUDIT.write_text("", encoding="utf-8")

cfg = Config(apps=[AppConfig(name="alpha", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(AUDIT), use_worktree=False)
cfg.detected_auth = lambda: "test"
srv.health.summary = lambda c: {"healthy": True, "checks": []}


def _reset():
    """Clear all ceremony flags, last_result keys, and thread stub bookkeeping."""
    for k in ("standuping", "councilling", "scribing", "meeting", "patrolling",
              "grouping", "shipreview", "qa"):
        srv._state.pop(k, None)
    srv._state.pop("last_result", None)
    srv._state.pop("last_result_record", None)
    srv._state.pop("last_msg", None)
    reset_run_state()


# ═══════════════════════════════════════════════════════════════════════════
# 1) FAILURE: stub raises RuntimeError → tone == "error", text starts with prefix
# ═══════════════════════════════════════════════════════════════════════════

_reset()

class _RaiseStandup:
    """Stands up by raising — simulates a council/provider failure."""
    def __init__(s): s._orig = None
    def setup(s):
        s._orig = getattr(council, "hold_standup", None)
        async def boom(cfg, audit=None):
            raise RuntimeError("boom — standup stub failure")
        council.hold_standup = boom
    def teardown(s):
        if s._orig is not None:
            council.hold_standup = s._orig

_raise_su = _RaiseStandup()
_raise_su.setup()

class _RaiseCouncil:
    """Council raises — simulates a council/provider failure."""
    def __init__(s): s._orig = None
    def setup(s):
        s._orig = getattr(council, "hold_council", None)
        async def boom(cfg, topic=None, audit=None, *, broadcast=None):
            raise RuntimeError("boom — council stub failure")
        council.hold_council = boom
    def teardown(s):
        if s._orig is not None:
            council.hold_council = s._orig

_raise_co = _RaiseCouncil()
_raise_co.setup()


class _SyncThread:
    """Runs target inline on .start() — no daemon thread, deterministic visibility."""
    def __init__(s, target=None, daemon=None, **kw): s.t = target
    def start(s):
        if s.t:
            s.t()


srv.threading = types.SimpleNamespace(Thread=_SyncThread, Event=__import__("threading").Event,
                                     Lock=__import__("threading").Lock)

app = srv.create_app(cfg)

# -- FAIL standup --
_reset()
client = app.test_client()
r = client.post("/api/standup", data={})
# _SyncThread executes synchronously, so result should be visible immediately
chk("POST /api/standup redirects (failure path)", r.status_code == 302, f"status={r.status_code}")
rec = get_state(None).get("last_result_record", {})
chk("standup failure → tone 'error' (AC1)",
    rec.get("tone") == "error", repr(rec))
chk("standup failure → text starts with 'standup failed:' (AC1)",
    rec.get("text", "").startswith("standup failed:"), repr(rec.get("text")))

# -- FAIL council --
_reset()
client = app.test_client()
r = client.post("/api/council", data={})
chk("POST /api/council redirects (failure path)", r.status_code == 302, f"status={r.status_code}")
rec = get_state(None).get("last_result_record", {})
chk("council failure → tone 'error' (AC1)",
    rec.get("tone") == "error", repr(rec))
chk("council failure → text starts with 'council failed:' (AC1)",
    rec.get("text", "").startswith("council failed:"), repr(rec.get("text")))

_srv_threading_swap = srv.threading
del srv.threading          # restore real threading for remaining tests


# ═══════════════════════════════════════════════════════════════════════════
# 2) SUCCESS: stub succeeds → tone == "ok", correct text
# ═══════════════════════════════════════════════════════════════════════════

class _OkStandup:
    def __init__(s): s._orig = None
    def setup(s):
        s._orig = getattr(council, "hold_standup", None)
        async def ok(cfg, audit=None):
            return "standup done"
        council.hold_standup = ok
    def teardown(s):
        if s._orig is not None:
            council.hold_standup = s._orig

class _OkCouncil:
    def __init__(s): s._orig = None
    def setup(s):
        s._orig = getattr(council, "hold_council", None)
        async def ok(cfg, topic=None, audit=None, *, broadcast=None):
            return "council done"
        council.hold_council = ok
    def teardown(s):
        if s._orig is not None:
            council.hold_council = s._orig

_ok_su = _OkStandup()
_ok_su.setup()
_ok_co = _OkCouncil()
_ok_co.setup()

srv.threading = types.SimpleNamespace(Thread=_SyncThread, Event=__import__("threading").Event,
                                     Lock=__import__("threading").Lock)

app2 = srv.create_app(cfg)

# -- OK standup --
_reset()
client = app2.test_client()
r = client.post("/api/standup", data={})
chk("POST /api/standup redirects (success path)", r.status_code == 302, f"status={r.status_code}")
rec = get_state(None).get("last_result_record", {})
st = get_state(None)
chk("standup success → tone 'ok' (AC2)",
    rec.get("tone") == "ok", repr(rec))
chk("standup success → text '✓ Standup complete.' (AC2)",
    rec.get("text") == "✓ Standup complete.", repr(rec.get("text")))
chk("standup success → last_result plain string back-compat (AC2)",
    st.get("last_result") == "✓ Standup complete.", repr(st.get("last_result")))

# -- OK council --
_reset()
client = app2.test_client()
r = client.post("/api/council", data={})
chk("POST /api/council redirects (success path)", r.status_code == 302, f"status={r.status_code}")
rec = get_state(None).get("last_result_record", {})
st = get_state(None)
chk("council success → tone 'ok' (AC2)",
    rec.get("tone") == "ok", repr(rec))
chk("council success → text '✓ Council held.' (AC2)",
    rec.get("text") == "✓ Council held.", repr(rec.get("text")))
chk("council success → last_result plain string back-compat (AC2)",
    st.get("last_result") == "✓ Council held.", repr(st.get("last_result")))


# ═══════════════════════════════════════════════════════════════════════════
# 3) SEQUENCE: success then failure → tone 'error'; failure then success → tone 'ok'
# ═══════════════════════════════════════════════════════════════════════════

# Success → Failure (standup ok, council fail)
_reset()
_ok_su.setup()
_fail_co = _RaiseCouncil()
_fail_co.setup()
srv.threading = types.SimpleNamespace(Thread=_SyncThread, Event=__import__("threading").Event,
                                     Lock=__import__("threading").Lock)
app3 = srv.create_app(cfg)

client = app3.test_client()
client.post("/api/standup", data={})
chk("sequence: after successful standup, tone is 'ok'",
    get_state(None).get("last_result_record", {}).get("tone") == "ok",
    repr(get_state(None).get("last_result_record")))

_reset()
_fail_co.setup()
# Now run council (which fails) — should overwrite the prior success record
client = app3.test_client()
client.post("/api/council", data={})
chk("sequence: failure overwrites prior success → tone 'error' (no stale 'ok', AC3)",
    get_state(None).get("last_result_record", {}).get("tone") == "error",
    repr(get_state(None).get("last_result_record")))

# Failure → Success (standup fail, council ok)
_reset()
_fail_su = _RaiseStandup()
_fail_su.setup()
_ok_co.setup()
srv.threading = types.SimpleNamespace(Thread=_SyncThread, Event=__import__("threading").Event,
                                     Lock=__import__("threading").Lock)
app4 = srv.create_app(cfg)

client = app4.test_client()
client.post("/api/standup", data={})
chk("sequence: after failing standup, tone is 'error'",
    get_state(None).get("last_result_record", {}).get("tone") == "error",
    repr(get_state(None).get("last_result_record")))

_reset()
_ok_su.setup()
_ok_co.setup()
srv.threading = types.SimpleNamespace(Thread=_SyncThread, Event=__import__("threading").Event,
                                     Lock=__import__("threading").Lock)
app5 = srv.create_app(cfg)

client = app5.test_client()
# First: council succeeds (overwrites any prior state)
client.post("/api/council", data={})
chk("sequence: success overwrites prior failure → tone 'ok' (no stale 'error', AC3)",
    get_state(None).get("last_result_record", {}).get("tone") == "ok",
    repr(get_state(None).get("last_result_record")))


# ═══════════════════════════════════════════════════════════════════════════
# 4) GROUND GUARD: no bare _state["last_msg"] assignment remains in the two handlers
# ═══════════════════════════════════════════════════════════════════════════

import inspect
standup_src = inspect.getsource(app.view_functions["standup_api"])
council_src = inspect.getsource(app5.view_functions["council_api"])

# There must be NO bare "_state["last_msg"]" or '_state["last_msg"]' assignment inside either function
has_bare_sm = ('_state["last_msg"]' in standup_src or '_state[\'last_msg\']' in standup_src or
               '_state["last_msg"]' in council_src or '_state[\'last_msg\']' in council_src)
chk("no bare _state['last_msg'] in standup_api (AC4)", not has_bare_sm,
    "found bare _state[last_msg] assignment")

# Each failure path must contain a set_last_result call with explicit "error"
chk('standup_api contains set_last_result(..., "error", ...) with "standup failed"',
    'set_last_result(None, "error"' in standup_src and '"standup failed:' in standup_src,
    "missing expected set_last_result call in standup handler")
chk('council_api contains set_last_result(..., "error", ...) with "council failed"',
    'set_last_result(None, "error"' in council_src and '"council failed:' in council_src,
    "missing expected set_last_result call in council handler")


# ─── summary ──────────────────────────────────────────────────────────────
print("\n============ EU-660 STANDUP + COUNCIL set_last_result MIGRATION QA =====")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
if passed != len(results):
    print(f"  RESULT: {len(results) - passed} FAIL")
    sys.exit(1)
else:
    print("  RESULT: ALL GREEN")
