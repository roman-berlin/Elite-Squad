"""Regression tests for EU-665: report_api dual-writes set_last_result with explicit ok/error tone.

Covers all five terminal paths of ``report_api`` — every exit point calls
set_last_result(app_name or None, tone, text) where tone is strictly "ok"
on success and "error" on any failure.  Assertions are on
``last_result_record["tone"]``, never on message substrings.
"""
import asyncio
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

# --- Real imports after stubs installed ---
from orchestrator.cockpit_state import get_state, reset_workspaces  # noqa: E402
import orchestrator.server as srv  # noqa: E402
from orchestrator.config import AppConfig, Config  # noqa: E402

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

# ──────────────────────────────────────────────────────────────────────────
# PATH 1: health-check-fails → tone == "error"
# ──────────────────────────────────────────────────────────────────────────
_reset()
srv.health.summary = lambda c: {"healthy": False, "checks": []}

r = client.post("/api/report", data={"app": "alpha", "text": "health down"})
chk("PATH 1: health fail redirects (302)", r.status_code == 302, f"{r.status_code}")
rec = get_state("alpha").get("last_result_record", {})
chk("PATH 1b: health fail tone='error'", rec.get("tone") == "error", repr(rec.get("tone")))
chk("PATH 1c: health fail text mentions 'blocked'",
    "blocked" in str(rec.get("text", "")), repr(rec.get("text")))

srv.health.summary = lambda c: {"healthy": True, "checks": []}

# ──────────────────────────────────────────────────────────────────────────
# PATH 2: backend-resolution-error → tone == "error"
# ──────────────────────────────────────────────────────────────────────────
_reset()
orig_resolve = srv._resolve_run_backend
async def _fake_loop(rcfg, worklist, audit=None, stop_event=None):
    return []
srv.run_loop = _fake_loop
srv.intake.from_text = lambda rcfg, app_name, title, keys, description=None: [
    types.SimpleNamespace(key="ALPHA-9", title=title)]

def _backend_error(_rcfg, _name):
    return "no backend configured for this project"
srv._resolve_run_backend = _backend_error

r = client.post("/api/report", data={"app": "alpha", "text": "do a thing"})
chk("PATH 2: backend error redirects (302)", r.status_code == 302, f"{r.status_code}")
rec = get_state("alpha").get("last_result_record", {})
chk("PATH 2b: backend error tone='error'", rec.get("tone") == "error", repr(rec.get("tone")))
chk("PATH 2c: backend error text matches error msg",
    "no backend configured" in str(rec.get("text", "")), repr(rec.get("text")))

srv._resolve_run_backend = orig_resolve

# ──────────────────────────────────────────────────────────────────────────
# PATH 3: intake exception handler → tone == "error"
# ──────────────────────────────────────────────────────────────────────────
_reset()
orig_from_text = srv.intake.from_text

def _intake_raises(*a, **k):
    raise ValueError("broken intake logic")
srv.intake.from_text = _intake_raises

r = client.post("/api/report", data={"app": "alpha", "text": "trigger intake crash"})
chk("PATH 3: intake exception redirects (302)", r.status_code == 302, f"{r.status_code}")
rec = get_state("alpha").get("last_result_record", {})
chk("PATH 3b: intake exception tone='error'", rec.get("tone") == "error", repr(rec.get("tone")))
chk("PATH 3c: intake exception text contains 'could not start'",
    "could not start" in str(rec.get("text", "")), repr(rec.get("text")))

srv.intake.from_text = orig_from_text

# ──────────────────────────────────────────────────────────────────────────
# PATH 4: background run_loop exception → tone == "error"
# ──────────────────────────────────────────────────────────────────────────
_reset()

def _loop_raises(*a, **k):
    raise RuntimeError("boom in loop")
srv.run_loop = _loop_raises
srv.intake.from_text = lambda rcfg, app_name, title, keys, description=None: [
    types.SimpleNamespace(key="ALPHA-9", title=title)]

r = client.post("/api/report", data={"app": "alpha", "text": "crash the loop"})
chk("PATH 4: crash-in-loop redirects (302)", r.status_code == 302, f"{r.status_code}")
# The _bg thread runs sync (we swapped threading), so state should be updated immediately.
rec = get_state("alpha").get("last_result_record", {})
chk("PATH 4b: loop crash tone='error'", rec.get("tone") == "error", repr(rec.get("tone")))
chk("PATH 4c: loop crash text contains error",
    "boom in loop" in str(rec.get("text", "")), repr(rec.get("text")))

# ──────────────────────────────────────────────────────────────────────────
# PATH 5: clean completion → tone == "ok"
# ──────────────────────────────────────────────────────────────────────────
_reset()

async def _fake_ok_loop(rcfg, worklist, audit=None, stop_event=None):
    return [{"key": "ALPHA-9"}]

srv.run_loop = _fake_ok_loop
srv.intake.from_text = lambda rcfg, app_name, title, keys, description=None: [
    types.SimpleNamespace(key="ALPHA-9", title=title)]

r = client.post("/api/report", data={"app": "alpha", "text": "clean run"})
chk("PATH 5: success redirects (302)", r.status_code == 302, f"{r.status_code}")
rec = get_state("alpha").get("last_result_record", {})
chk("PATH 5b: clean success tone='ok'", rec.get("tone") == "ok", repr(rec.get("tone")))
chk("PATH 5c: clean success text present",
    "report intake complete" in str(rec.get("text", "")), repr(rec.get("text")))

# ──────────────────────────────────────────────────────────────────────────
# REGRESSION: tone assertion only — no substring inference at all
# Two rapid different-toned outcomes must produce distinct records.
# ──────────────────────────────────────────────────────────────────────────
_reset()
srv.intake.from_text = lambda rcfg, app_name, title, keys, description=None: [
    types.SimpleNamespace(key="X-1")]
async def _loop_ok(*a, **k): return []
srv.run_loop = _loop_ok
client.post("/api/report", data={"app": "alpha", "text": "ok first"})
t1 = get_state("alpha").get("last_result_record", {}).get("tone")
chk("REG: first call tone='ok'", t1 == "ok", repr(t1))

_reset()
srv._resolve_run_backend = lambda *a: "always fails"
srv.intake.from_text = lambda *a, **k: [types.SimpleNamespace(key="X-1")]
async def _loop_ok2(*a, **k): return []
srv.run_loop = _loop_ok2
client.post("/api/report", data={"app": "alpha", "text": "err second"})
t2 = get_state("alpha").get("last_result_record", {}).get("tone")
chk("REG: second call tone='error'", t2 == "error", repr(t2))
chk("REG: tones differ (one ok, one error)", t1 == "ok" and t2 == "error",
    f"t1={t1}, t2={t2}")


# ──────────────────────────────────────────────────────────────────────────
# SUMMARY
# ──────────────────────────────────────────────────────────────────────────
print("\n============ EU-665 REPORT_API TONE QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
if passed != len(results):
    print(f"  RESULT: {len(results)-passed} FAIL")
    sys.exit(1)
else:
    print("  RESULT: ALL GREEN")
    sys.exit(0)
