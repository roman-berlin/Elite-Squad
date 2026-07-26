"""EU-667: QA verdict uses set_last_result with explicit ok/error tone.

Covers all terminal paths of ``qa_api`` by patching patrol and ship_review stubs
that raise or succeed inline (via _SyncThread).  Assertions are keyed off
``last_result_record["tone"]``, never on message substrings.
"""
import sys
import tempfile
import threading as _threading
import types
from pathlib import Path

sys.path.insert(0, ".")

# ── Stub modules ────────────────────────────────────────────────────────────
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)

req = types.ModuleType("requests")
req.Session = lambda *a, **k: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules.setdefault("requests", req)

# ── Sync-thread for deterministic execution ────────────────────────────────
class _SyncThread:
    """Runs target inline on .start() — no daemon thread, deterministic visibility."""
    def __init__(self, target=None, daemon=None, **kw):
        self.t = target
    def start(self):
        if self.t:
            self.t()

results: list[tuple[str, bool, str]] = []

def chk(n: str, c: bool, d: str = "") -> None:
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
AUDIT = tmp / "audit.jsonl"
AUDIT.write_text("", encoding="utf-8")

from orchestrator.config import AppConfig, Config  # noqa: E402
from orchestrator.cockpit_state import get_state, reset_run_state  # noqa: E402
import orchestrator.server as srv  # noqa: E402
import inspect  # noqa: E402
from orchestrator import council as _council_mod  # noqa: E402
from orchestrator import patrol as _patrol_mod  # noqa: E402

srv.health.summary = lambda c: {"healthy": True, "checks": []}
srv.threading = types.SimpleNamespace(
    Thread=_SyncThread, Event=_threading.Event, Lock=_threading.Lock)

cfg = Config(
    apps=[AppConfig(name="alpha", repo_path=str(tmp), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(AUDIT), use_worktree=False,
)
_flask_app = srv.create_app(cfg)
client = _flask_app.test_client()


def _reset():
    for k in ("standuping", "councilling", "scribing", "meeting",
              "patrolling", "grouping", "shipreview", "qa"):
        srv._state.pop(k, None)
    srv._state.pop("last_result", None)
    srv._state.pop("last_result_record", None)
    srv._state.pop("last_msg", None)
    srv._state.pop("qa_started", None)
    srv._state.pop("qa_phase", None)
    srv._state.pop("qa_error_phase", None)
    srv._state.pop("qa_findings", None)
    srv._state.pop("qa_verdict", None)
    srv._state.pop("qa_dismissed", None)
    srv._state.pop("qa_app", None)
    srv._state.pop("patrolling", None)
    srv._state.pop("shipreview", None)
    reset_run_state()


def _rec():
    return get_state(None).get("last_result_record", {})


# ═══════════════════════════════════════════════════════════════════════════
# PATH 1: patrol failure → tone == "error"
# ═══════════════════════════════════════════════════════════════════════════
_reset()

async def _patrol_boom(*a, **kw):
    raise RuntimeError("recon connection refused")

async def _ship_skipped(_cfg, _app_name, audit=None):
    # Should not be called because patrol raises first
    return "never runs"

_patrol_mod.patrol = _patrol_boom
_council_mod.ship_review = _ship_skipped

r = client.post("/api/qa", data={"app": "alpha"})
chk("PATH 1: POST /api/qa redirects", r.status_code in (302, 303), f"got {r.status_code}")
chk("PATH 1b: tone == 'error'", _rec().get("tone") == "error", repr(_rec().get("tone")))
chk("PATH 1c: text starts with 'QA failed:'",
    str(_rec().get("text", "")).startswith("QA failed:"), repr(_rec().get("text")))

# Restore originals
_patrol_mod.patrol = None
_council_mod.ship_review = None

# ═══════════════════════════════════════════════════════════════════════════
# PATH 2: ship-review failure → tone == "error" (patrol succeeds)
# ═══════════════════════════════════════════════════════════════════════════
_reset()

class _PS:
    def __init__(s): s._p = None; s._s = None
    def setup(s):
        async def ok_patrol(*a, **kw):
            return types.SimpleNamespace(filed=["EU-910"])
        async def boom_ship(cfg, app_name, audit=None):
            raise RuntimeError("ship verdict LLM error")
        s._p = _patrol_mod.patrol
        s._s = _council_mod.ship_review
        _patrol_mod.patrol = ok_patrol
        _council_mod.ship_review = boom_ship
    def teardown(s):
        if s._p is not None: _patrol_mod.patrol = s._p
        if s._s is not None: _council_mod.ship_review = s._s

_ps = _PS(); _ps.setup()

r2 = client.post("/api/qa", data={"app": "alpha"})
chk("PATH 2: POST /api/qa redirects after success→fail",
    r2.status_code in (302, 303), f"got {r2.status_code}")
chk("PATH 2b: tone == 'error' (ship_review fails)",
    _rec().get("tone") == "error", repr(_rec().get("tone")))
chk("PATH 2c: text contains 'QA failed:'",
    "QA failed:" in str(_rec().get("text", "")), repr(_rec().get("text")))

_ps.teardown()

# ═══════════════════════════════════════════════════════════════════════════
# PATH 3: both phases succeed → tone == "ok"
# ═══════════════════════════════════════════════════════════════════════════
_reset()

class _PO:
    def __init__(s): s._p = None; s._s = None
    def setup(s):
        async def ok_patrol(*a, **kw):
            return types.SimpleNamespace(filed=["EU-910", "EU-911"])
        async def ok_ship(_cfg, _app_name, audit=None):
            return "GO — clean dev"
        s._p = _patrol_mod.patrol
        s._s = _council_mod.ship_review
        _patrol_mod.patrol = ok_patrol
        _council_mod.ship_review = ok_ship
    def teardown(s):
        if s._p is not None: _patrol_mod.patrol = s._p
        if s._s is not None: _council_mod.ship_review = s._s

_po = _PO(); _po.setup()

r3 = client.post("/api/qa", data={"app": "alpha"})
chk("PATH 3: POST /api/qa redirects on success",
    r3.status_code in (302, 303), f"got {r3.status_code}")
chk("PATH 3b: tone == 'ok' (both phases ok)",
    _rec().get("tone") == "ok", repr(_rec().get("tone")))
chk("PATH 3c: text starts with '✓ QA finished'",
    str(_rec().get("text", "")).startswith("✓ QA finished"),
    repr(_rec().get("text")))

_po.teardown()

# ═══════════════════════════════════════════════════════════════════════════
# REGRESSION: distinct tones must differ; source check
# ═══════════════════════════════════════════════════════════════════════════
_reset()
try:
    r4 = client.post("/api/qa", data={"app": "alpha"})
    chk("REG: no crash on minimal request", r4.status_code in (302, 303), f"got {r4.status_code}")
except Exception as e:
    chk("REG: no crash on minimal request", False, f"raised {e!r}")

# Source check: qa_api handler must use set_last_result with explicit tones
src = ""
try:
    src = inspect.getsource(_flask_app.view_functions["qa_api"])
except Exception as e:
    chk("REG: source inspection failed", False, str(e))

if src:
    chk("REG: qa_api has set_last_result(..., 'error', ...)",
        'set_last_result(None, "error"' in src or "set_last_result(None, 'error'" in src,
        "missing error set_last_result in qa_api")
    chk("REG: qa_api has set_last_result(..., 'ok', ...)",
        'set_last_result(None, "ok"' in src or "set_last_result(None, 'ok'" in src,
        "missing ok set_last_result in qa_api")


# ── summary ──────────────────────────────────────────────────────────────
print("\n======== EU-667 QA VERDICT TONE QA ========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {name}"
    if det and not ok:
        line += f"  ({det})"
    print(line)
print("-------------------------------------------")
print(f"  {passed}/{len(results)} passed")
if passed != len(results):
    print(f"  RESULT: {len(results) - passed} FAIL")
    sys.exit(1)
else:
    print("  RESULT: ALL GREEN")
    sys.exit(0)
