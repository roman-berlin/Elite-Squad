"""EU-385 (EU-224a) — persisted drain-arm intent + serve-boot auto-resume.

The live gap (2026-07-17): a 13:11 crash-respawn left a drain dead for 66+ minutes because
nothing remembered it had been RUNNING. The fix under test: a continuous LIVE drain persists
its arm to ``state/autopilot_intent.json`` (``record_drain_intent``) as ``state=RUNNING``;
EVERY clean stand-down (the drain's finally — cockpit-stop, sigterm, budget, exception)
retires it to ``STOPPED`` with the stop reason; only an abrupt process death (finally never
ran) leaves ``RUNNING`` behind. At serve boot, ``resume_armed_drains()`` re-starts exactly
those RUNNING drains through the same claim_run/_bg shape a cockpit Start uses — UNLESS the
audit shows a Commander stop order (``autopilot_stop_requested``, EU-356) at/after the arm:
an explicitly-stopped drain must NEVER be resurrected, even when the crash beat the drain's
finally to the intent file (the conservative discriminator).

Offline — the SDK is stubbed; no models, no network, PID file redirected to tmp.
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

# Hermetic even when run standalone (run_all already scrubs these from harness env).
for _k in list(os.environ):
    if _k.startswith(("TELEGRAM_", "JIRA_")):
        os.environ.pop(_k, None)

from orchestrator import autopilot as ap
from orchestrator import cockpit_state, health, notify
from orchestrator.audit import AuditLog
from orchestrator.config import AppConfig, Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


def _events(path):
    try:
        return [json.loads(ln) for ln in Path(path).read_text().splitlines() if ln.strip()]
    except OSError:
        return []


def _intent(cfg):
    p = Path(cfg.audit_path).with_name("autopilot_intent.json")
    try:
        return json.loads(p.read_text())
    except OSError:
        return None


_orig_pid = ap._PID_FILE
_orig_send = notify.send
_orig_health = health.summary
notify.send = lambda *a, **k: False
health.summary = lambda cfg: {"healthy": True}

try:
    # ── §1 record / clear round-trip (unit) ──────────────────────────────────────────────
    tmp1 = Path(tempfile.mkdtemp())
    cfg1 = Config(apps=[], audit_path=str(tmp1 / "audit.jsonl"))
    ap.record_drain_intent(cfg1, "a1")
    ap.record_drain_intent(cfg1, None)          # unit-wide drain → the "" key
    doc = _intent(cfg1) or {}
    chk("record: per-app entry is RUNNING", (doc.get("a1") or {}).get("state") == "RUNNING", str(doc))
    chk("record: unit-wide entry keys under ''", (doc.get("") or {}).get("state") == "RUNNING", str(doc))
    chk("record: armed_ts is stamped", isinstance((doc.get("a1") or {}).get("armed_ts"), (int, float)), str(doc))
    ap.clear_drain_intent(cfg1, "a1", "cockpit-stop")
    doc = _intent(cfg1) or {}
    chk("clear: entry flips to STOPPED with the reason",
        (doc.get("a1") or {}).get("state") == "STOPPED"
        and (doc.get("a1") or {}).get("reason") == "cockpit-stop", str(doc))
    chk("clear: armed_ts survives the stop (forensics)",
        isinstance((doc.get("a1") or {}).get("armed_ts"), (int, float)), str(doc))
    chk("clear: the OTHER entry is untouched", (doc.get("") or {}).get("state") == "RUNNING", str(doc))

    # ── §2 the drain arms at start and retires on a clean stand-down (behavioural) ──────
    tmp2 = Path(tempfile.mkdtemp())
    cfg2 = Config(apps=[], audit_path=str(tmp2 / "audit.jsonl"))
    ap._PID_FILE = tmp2 / "ap.pid"
    ev = threading.Event()
    ev.set()                                    # loop breaks at its first check → "cockpit-stop"
    asyncio.run(ap.autopilot(cfg2, None, once=False, stop_event=ev))
    doc = _intent(cfg2) or {}
    chk("continuous live drain: intent recorded, then retired as STOPPED/cockpit-stop",
        (doc.get("") or {}).get("state") == "STOPPED"
        and (doc.get("") or {}).get("reason") == "cockpit-stop", str(doc))
    chk("continuous live drain: armed_ts present (proves the arm happened mid-flight)",
        isinstance((doc.get("") or {}).get("armed_ts"), (int, float)), str(doc))

    tmp2b = Path(tempfile.mkdtemp())
    cfg2b = Config(apps=[], audit_path=str(tmp2b / "audit.jsonl"))
    ap._PID_FILE = tmp2b / "ap.pid"
    asyncio.run(ap.autopilot(cfg2b, None, once=True))
    chk("--once cycle: never arms (no standing intent)", _intent(cfg2b) is None, str(_intent(cfg2b)))

    tmp2c = Path(tempfile.mkdtemp())
    cfg2c = Config(apps=[], audit_path=str(tmp2c / "audit.jsonl"))
    cfg2c.dry_run = True
    ap._PID_FILE = tmp2c / "ap.pid"
    ev = threading.Event()
    ev.set()
    asyncio.run(ap.autopilot(cfg2c, None, once=False, stop_event=ev))
    chk("dry-run drain: never arms (a preview is not a standing intent)",
        _intent(cfg2c) is None, str(_intent(cfg2c)))

    # ── §3 serve-boot resume: a RUNNING intent re-starts the REAL drain ─────────────────
    tmp3 = Path(tempfile.mkdtemp())
    cfg3 = Config(apps=[], audit_path=str(tmp3 / "audit.jsonl"))
    ap._PID_FILE = tmp3 / "ap.pid"
    (tmp3 / "autopilot_intent.json").write_text(json.dumps(
        {"": {"state": "RUNNING", "armed_ts": time.time() - 30, "pid": 999999}}))
    resumed = ap.resume_armed_drains(cfg3)
    chk("resume: the RUNNING unit-wide intent is resumed", resumed == [""], str(resumed))
    deadline = time.time() + 20
    while time.time() < deadline:
        if any(e.get("event") == "autopilot_start" for e in _events(cfg3.audit_path)):
            break
        time.sleep(0.05)
    evs = [e.get("event") for e in _events(cfg3.audit_path)]
    chk("resume: process_start + autopilot_start audit pair emitted (the EU-385 AC)",
        "process_start" in evs and "autopilot_start" in evs, str(evs))
    chk("resume: an autopilot_resume forensic event is recorded", "autopilot_resume" in evs, str(evs))
    stop_ev = cockpit_state.get_state(None).get("stop_event")
    chk("resume: the drain's stop_event is reachable from cockpit state (same path as a manual start)",
        stop_ev is not None)
    if stop_ev is not None:
        stop_ev.set()                           # the cockpit Stop path — drain must retire its arm
    deadline = time.time() + 20
    while time.time() < deadline:
        d = _intent(cfg3) or {}
        if (d.get("") or {}).get("state") == "STOPPED":
            break
        time.sleep(0.05)
    doc = _intent(cfg3) or {}
    chk("resume→stop: the resumed drain retires its own intent (STOPPED/cockpit-stop)",
        (doc.get("") or {}).get("state") == "STOPPED"
        and (doc.get("") or {}).get("reason") == "cockpit-stop", str(doc))

    # ── §4 the discriminator: a Commander stop order at/after the arm blocks resume ─────
    tmp4 = Path(tempfile.mkdtemp())
    cfg4 = Config(apps=[AppConfig(name="a1", repo_path=str(tmp4), base_branch="DEV",
                                  backlog_backend="none"),
                        AppConfig(name="a2", repo_path=str(tmp4), base_branch="DEV",
                                  backlog_backend="none")],
                  audit_path=str(tmp4 / "audit.jsonl"))
    ap._PID_FILE = tmp4 / "ap.pid"
    (tmp4 / "autopilot_intent.json").write_text(json.dumps({
        "a1": {"state": "RUNNING", "armed_ts": time.time() - 60, "pid": 999999},
        "a2": {"state": "RUNNING", "armed_ts": time.time() - 60, "pid": 999999},
    }))
    # The EU-356 stop-request trail: the Commander clicked Stop for a1 AFTER it armed; the
    # crash beat the drain's finally, so the intent file still says RUNNING. Resume must not
    # resurrect it.
    AuditLog(cfg4.audit_path).record("autopilot_stop_requested", action="stop", app="a1",
                                     event_reachable=True)
    calls = []
    async def _stub_ap(c, app_name=None, once=False, interval=60, stop_event=None):
        calls.append((app_name, once))
    _real_ap = ap.autopilot
    ap.autopilot = _stub_ap
    try:
        resumed4 = ap.resume_armed_drains(cfg4, wait_s=10)
    finally:
        ap.autopilot = _real_ap
    chk("discriminator: the commander-stopped app is NOT resumed", resumed4 == ["a2"], str(resumed4))
    chk("discriminator: only the un-stopped app's drain was spawned",
        calls == [("a2", False)], str(calls))
    doc = _intent(cfg4) or {}
    chk("discriminator: the blocked intent is retired so it can never resurrect later",
        (doc.get("a1") or {}).get("state") == "STOPPED", str(doc))
    evs4 = _events(cfg4.audit_path)
    chk("discriminator: the skip is auditable (autopilot_resume_skipped, app=a1)",
        any(e.get("event") == "autopilot_resume_skipped" and e.get("app") == "a1" for e in evs4),
        str([e.get("event") for e in evs4]))
    chk("discriminator: a2 still RUNNING→resumed is auditable (autopilot_resume, app=a2)",
        any(e.get("event") == "autopilot_resume" and e.get("app") == "a2" for e in evs4),
        str([e.get("event") for e in evs4]))

    # ── §5 a STOPPED intent never resumes ───────────────────────────────────────────────
    tmp5 = Path(tempfile.mkdtemp())
    cfg5 = Config(apps=[], audit_path=str(tmp5 / "audit.jsonl"))
    ap._PID_FILE = tmp5 / "ap.pid"
    (tmp5 / "autopilot_intent.json").write_text(json.dumps(
        {"": {"state": "STOPPED", "armed_ts": time.time() - 60, "reason": "cockpit-stop"}}))
    chk("a STOPPED intent is left alone (deliberate stop does not restart on boot)",
        ap.resume_armed_drains(cfg5) == [])
finally:
    ap._PID_FILE = _orig_pid
    notify.send = _orig_send
    health.summary = _orig_health

passed = [n for n, ok, _ in results if ok]
failed = [(n, d) for n, ok, d in results if not ok]
print(f"\neu385_autoresume_test: {len(passed)}/{len(results)} passed")
for n, d in failed:
    print(f"  FAIL: {n}" + (f" — {d}" if d else ""))
if failed:
    sys.exit(1)
