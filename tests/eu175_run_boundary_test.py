"""EU-175 — every run path closes its run boundary, so a crash is always diagnosable.

The Jul-1 crash left GHOST sessions: autopilot recorded ``autopilot_stop`` AFTER its finally (so any
non-graceful exit skipped it), and three run_loop call sites (cockpit /api/run, /api/run-selected, and
the Telegram/decision-resume ``decisions._run_bg``) recorded NO run boundary at all. The fix:
autopilot_stop fires from INSIDE finally BUT gated on a ``started`` flag (so a setup failure BEFORE the
paired autopilot_start never records an UNPAIRED stop — the mirror-image invariant break), and each
run_loop call is bracketed with run_start (before) / run_end (from the finally). Invariants:
count(run_start) == count(run_end) and count(autopilot_start) == count(autopilot_stop), ALWAYS.

Written behaviourally where it counts: §1 drives decisions._run_bg through raise + success; §2 drives
the REAL autopilot() with a setup that explodes before the start, proving no unpaired stop is written
(the PID file is redirected to a tmp path so the machine-global daemon handshake is untouched). §3
pins both cockpit routes structurally — but by asserting run_end sits INSIDE the finally, not merely
that it exists (a run_end moved out of the finally is the exact ghost-session regression).

Offline — the SDK + run_loop are stubbed; no models, no network.
"""
import asyncio
import inspect
import json
import re
import sys
import tempfile
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import decisions, cockpit_state, notify
from orchestrator import loop as _loop
from orchestrator import autopilot as _autopilot
from orchestrator.config import Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


class _Audit:
    def __init__(s): s.events = []
    def record(s, e, **k): s.events.append(e)


class _SyncThread:
    """Run the target synchronously so the test can assert on the events the worker recorded."""
    def __init__(s, target=None, daemon=None, **k): s._t = target
    def start(s):
        if s._t:
            s._t()


async def _boom(*a, **k):
    raise RuntimeError("model down")


async def _ok(*a, **k):
    return []


def _events(path):
    try:
        return [json.loads(ln).get("event") for ln in Path(path).read_text().splitlines() if ln.strip()]
    except Exception:  # noqa: BLE001
        return []


# ── 1. decisions._run_bg — run_start/run_end stay paired, even when run_loop raises ──
_cfg = types.SimpleNamespace(dry_run=False)
_orig = (decisions.threading.Thread, _loop.run, cockpit_state.claim_run,
         cockpit_state.release_run, notify.send)
decisions.threading.Thread = _SyncThread
cockpit_state.claim_run = lambda *a, **k: True
cockpit_state.release_run = lambda *a, **k: None
notify.send = lambda *a, **k: None
try:
    _loop.run = _boom
    a1 = _Audit()
    decisions._run_bg(_cfg, a1, [])
    chk("run raises: run_start recorded", a1.events.count("run_start") == 1, str(a1.events))
    chk("run raises: run_end STILL recorded (from finally)", a1.events.count("run_end") == 1, str(a1.events))
    chk("run raises: run_start/run_end stay paired",
        a1.events.count("run_start") == a1.events.count("run_end") == 1)

    _loop.run = _ok
    a2 = _Audit()
    decisions._run_bg(_cfg, a2, [])
    chk("run ok: run_start + run_end both recorded",
        a2.events.count("run_start") == 1 and a2.events.count("run_end") == 1, str(a2.events))
finally:
    (decisions.threading.Thread, _loop.run, cockpit_state.claim_run,
     cockpit_state.release_run, notify.send) = _orig


# ── 2. autopilot_stop is PAIRED with autopilot_start — behavioural ──
# A setup failure BEFORE autopilot_start must record NEITHER a start NOR a (now unpaired) stop.
# _alert_unclean_restart is the first executable statement in the try, before autopilot_start — stub it
# to raise. Redirect the module-global PID path to tmp so the real daemon handshake is untouched.
_tmp = Path(tempfile.mkdtemp())
_cfg_ap = Config(apps=[], audit_path=str(_tmp / "audit.jsonl"))
_orig_pid = _autopilot._PID_FILE
_orig_alert = _autopilot._alert_unclean_restart
_autopilot._PID_FILE = _tmp / "ap.pid"
def _boom_setup(_audit):
    raise RuntimeError("setup exploded before autopilot_start")
_autopilot._alert_unclean_restart = _boom_setup
_raised = False
try:
    asyncio.run(_autopilot.autopilot(_cfg_ap, once=True))
except RuntimeError:
    _raised = True
finally:
    _autopilot._PID_FILE = _orig_pid
    _autopilot._alert_unclean_restart = _orig_alert
_ev = _events(_cfg_ap.audit_path)
chk("setup-failure: the exception propagates (autopilot did not swallow it)", _raised)
chk("setup-failure: NO autopilot_start recorded (never reached it)", _ev.count("autopilot_start") == 0, str(_ev))
chk("setup-failure: NO UNPAIRED autopilot_stop (the started-gate holds)", _ev.count("autopilot_stop") == 0, str(_ev))

# structural backstop: the stop is inside a finally AND gated on `started` (paired-only).
_ap_src = inspect.getsource(_autopilot.autopilot)
_fin_indent = None
_stop_inside_finally = False
for ln in _ap_src.splitlines():
    if ln.strip() == "finally:":
        _fin_indent = len(ln) - len(ln.lstrip())
    # EU-232: autopilot_stop now also carries reason=stop_reason or "once-complete" — match either
    # the bare call or the reasoned one, so this stays a pure "inside finally" structural check.
    if re.match(r'\s*audit\.record\("autopilot_stop"', ln):
        _stop_inside_finally = _fin_indent is not None and (len(ln) - len(ln.lstrip())) > _fin_indent
chk("autopilot_stop is inside the finally", _stop_inside_finally)
chk("autopilot_stop is gated by `if started:`",
    bool(re.search(r'if started:\s*\n\s+audit\.record\("autopilot_stop"', _ap_src)))


# ── 3. EVERY cockpit run route brackets run_loop, with run_end INSIDE the finally ──
# EU-361 (2026-07-16 audit) widened this contract from two routes to three: report_api ran a
# run_loop with no boundary at all, so a bug reported through '/report' opened a session forensics
# could never close — the same ghost-session class EU-175 closed for /api/run and /api/run-selected.
# The count is the point of the check: a NEW cockpit route that runs run_loop without bracketing it
# must fail here rather than quietly reopen the hole.
_COCKPIT_RUN_ROUTES = 3   # run_api, run_selected_api, report_api
_server_src = Path("orchestrator/server.py").read_text(encoding="utf-8")
chk("server: every run route records run_start (guarded)",
    _server_src.count('audit.record("run_start"') == _COCKPIT_RUN_ROUTES,
    f"found {_server_src.count('audit.record(\"run_start\"')}/{_COCKPIT_RUN_ROUTES}")
# run_end must sit INSIDE the finally (guarded), not merely exist — a run_end moved out of the finally
# is the exact ghost-session regression (skipped on exception). Assert the finally→guard→run_end shape
# for EVERY route. Comment lines between `finally:` and the guard are tolerated: this is a check on
# the code's shape, and a house comment explaining WHY the record sits there must not fail it.
_C = r"(?:[ \t]*#[^\n]*\n)*"
_finally_runend = re.findall(
    rf'finally:[ \t]*\n{_C}\s+if audit is not None:[ \t]*\n{_C}\s+audit\.record\("run_end"', _server_src)
chk("server: every route's run_end is the guarded first statement of the finally",
    len(_finally_runend) == _COCKPIT_RUN_ROUTES,
    f"matched {len(_finally_runend)}/{_COCKPIT_RUN_ROUTES}")
# Pairwise: the Nth run_start must precede the Nth run_loop (a route that records its boundary
# AFTER the run has already started still leaves the window EU-175 exists to close).
_start_at = [m.start() for m in re.finditer(r'audit\.record\("run_start"', _server_src)]
_loop_at = [m.start() for m in re.finditer(r"run_loop\(rcfg", _server_src)]
chk("server: run_start precedes run_loop in each run route",
    len(_start_at) == len(_loop_at) == _COCKPIT_RUN_ROUTES
    and all(s < l for s, l in zip(_start_at, _loop_at)),
    f"starts={_start_at} loops={_loop_at}")


passed = [n for n, ok, _ in results if ok]
failed = [(n, d) for n, ok, d in results if not ok]
print(f"\neu175_run_boundary_test: {len(passed)}/{len(results)} passed")
for n, d in failed:
    print(f"  FAIL: {n}" + (f" — {d}" if d else ""))
if failed:
    sys.exit(1)
