"""EU-428 AC2 — the VPS peer watcher: alert ONLY when the Mac goes dark WITH WORK IN FLIGHT.

The EU-403 watchdog is a launchd agent on the MAC — when the Mac sleeps, the watchdog sleeps with
it, so by construction it can never report "this host is down". This watcher runs on the VPS (the
one host that is always up) and watches the SYNCED peer audit (shared/mac.jsonl). It pages ONLY when
the peer went quiet while it had work in flight, so a deliberately-closed laptop with an idle drain
never pages (Roman closes it nightly; a nightly false alarm trains him to ignore the channel).

Pins (driven as a black box against scripts/peer-watchdog.sh):
  • stale + work-in-flight (drain RUNNING via autopilot_start w/o stop, OR ticket_start w/o terminal)
    -> ONE alert after FAIL_THRESHOLD consecutive checks.
  • stale + IDLE (drain STOPPED / nothing in flight) -> SILENT (never pages).
  • fresh -> SILENT regardless of in-flight state.
  • recovery -> exactly ONE notice on the first fresh check after an alert.
  • tripwires: the script is DUMB (curl + --max-time, shares nothing with the cockpit — no
    'orchestrator'/'python'), never exits non-zero, reads the EVENT age (not file mtime), and a
    closed-laptop-with-idle-drain is the canonical must-not-page case.

The age is parsed from the ts INSIDE the peer file (a no-op sync refreshes mtime while the event
inside stays ancient — the EU-428 defect), so we plant the ts as a JSON value, not via file mtime.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent.parent
WD = ROOT / "scripts" / "peer-watchdog.sh"

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ── tiny HTTP fixtures (capture = Telegram stand-in) ──────────────────────────────────
class _Capture(BaseHTTPRequestHandler):
    captured: list[str] = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        _Capture.captured.append(parse_qs(raw).get("text", [""])[0])
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _serve(handler_cls) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime(epoch))


def _run(tmp_peer_dir: Path, *, peer_lines: list[str], cap_port: int, **extra_env) \
        -> subprocess.CompletedProcess:
    """Run the watcher with one peer file (mac.jsonl) carrying peer_lines, fresh state."""
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "TMPDIR": tempfile.gettempdir(),
        "GENERAL_DIR": str(tmp_peer_dir.parent),
        "PEER_DIR": str(tmp_peer_dir),
        "STATE_DIR": str(tmp_peer_dir / "state"),
        "PEER_STALE_SEC": "300",       # 5 min — test threshold
        "FAIL_THRESHOLD": "2",
        "RE_ALERT_EVERY": "4",
        "TELEGRAM_API_URL": f"http://127.0.0.1:{cap_port}",
        "TELEGRAM_BOT_TOKEN": "fake-token",
        "TELEGRAM_CHAT_ID": "123456",
    }
    env.update({k: str(v) for k, v in extra_env.items()})
    # NOTE: do NOT reset peer_watchdog_state here — each scenario uses a fresh tmp dir (clean state),
    # and the 2-consecutive-failure accumulation must persist ACROSS the two _run calls in a scenario.
    os.makedirs(tmp_peer_dir / "state", exist_ok=True)
    (tmp_peer_dir / "mac.jsonl").write_text("\n".join(peer_lines) + "\n", encoding="utf-8")
    return subprocess.run(["bash", str(WD)], env=env, capture_output=True, text=True, timeout=60)


now = time.time()
OLD = _iso(now - 7200)        # 2h ago — past the 5-min threshold
FRESH = _iso(now - 60)        # 1m ago — fresh


# ════════════════════════════════════════════════════════════════════════════
# tripwires first
# ════════════════════════════════════════════════════════════════════════════
chk("peer-watchdog script exists", WD.exists(), str(WD))
wd_text = WD.read_text(encoding="utf-8") if WD.exists() else ""
chk("watcher is DUMB — pure shell + curl, shares nothing with the cockpit (no 'orchestrator')",
    "orchestrator" not in wd_text.lower(), "must not reference the cockpit's code")
chk("watcher is DUMB — no Python (no 'python' invocation)",
    "python" not in wd_text.lower(), "AC2: plain shell + curl, no Python")
chk("watcher pages via curl with a --max-time bound",
    "curl" in wd_text and "--max-time" in wd_text, "needs curl ... --max-time")
chk("watcher never exits non-zero (timer-driven; reports via Telegram, not via exit code)",
    "exit 0" in wd_text and "set -e" not in wd_text, "a stale peer is a reportable condition, not an abort")
chk("watcher threshold is configurable via PEER_STALE_SEC",
    "PEER_STALE_SEC" in wd_text, "PEER_STALE_SEC knob missing")
chk("watcher default threshold >= 2h (2× sync interval + builder ceiling)",
    "7200" in wd_text, "default PEER_STALE_SEC should be 7200 (2h)")
chk("watcher reads the EVENT age inside the peer file (not file mtime)",
    "ts" in wd_text, "must parse the newest event ts, not stat the file mtime")

cap_srv = _serve(_Capture)
cap_port = cap_srv.server_address[1]

try:
    # ── A) stale + drain RUNNING (autopilot_start, no stop) -> ALERT after 2 ─────────
    baseA = Path(tempfile.mkdtemp(prefix="pwA-"))
    pdA = baseA / "shared"
    pdA.mkdir(parents=True)
    _Capture.captured = []
    peer_drain_run = [
        '{"ts": "%s", "event": "agent_call"}' % _iso(now - 8000),
        '{"ts": "%s", "event": "autopilot_start", "mode": "live", "app": "automatixy"}' % OLD,
    ]
    _run(pdA, peer_lines=peer_drain_run, cap_port=cap_port)
    chk("A: 1st stale+drain-running check sends NO alert (under threshold)", len(_Capture.captured) == 0,
        f"captured={_Capture.captured}")
    _run(pdA, peer_lines=peer_drain_run, cap_port=cap_port)
    chk("A: 2nd stale+drain-running check sends exactly ONE alert",
        len(_Capture.captured) == 1, f"captured={len(_Capture.captured)}")
    chk("A: the alert names the peer/watchdog and the work-in-flight",
        _Capture.captured and "PEER" in _Capture.captured[0].upper()
        and ("drain" in _Capture.captured[0].lower() or "in flight" in _Capture.captured[0].lower()),
        f"captured={_Capture.captured[:1]}")

    # ── B) stale + IDLE (drain STOPPED) -> SILENT (the closed-laptop case) ──────────
    baseB = Path(tempfile.mkdtemp(prefix="pwB-"))
    pdB = baseB / "shared"
    pdB.mkdir(parents=True)
    _Capture.captured = []
    peer_idle = [
        '{"ts": "%s", "event": "autopilot_start", "mode": "live", "app": "automatixy"}' % _iso(now - 9000),
        '{"ts": "%s", "event": "autopilot_stop", "reason": "stand-down"}' % OLD,
    ]
    _run(pdB, peer_lines=peer_idle, cap_port=cap_port)
    _run(pdB, peer_lines=peer_idle, cap_port=cap_port)
    chk("B: stale + IDLE (drain stopped) NEVER pages — the nightly closed-laptop case",
        len(_Capture.captured) == 0, f"captured={_Capture.captured}")

    # ── C) stale + open ticket (ticket_start, no terminal) -> ALERT ─────────────────
    baseC = Path(tempfile.mkdtemp(prefix="pwC-"))
    pdC = baseC / "shared"
    pdC.mkdir(parents=True)
    _Capture.captured = []
    peer_open_ticket = [
        '{"ts": "%s", "event": "autopilot_stop", "reason": "once"}' % _iso(now - 9000),
        '{"ts": "%s", "event": "ticket_start", "ticket_id": "EU-9", "app": "EU"}' % OLD,
        '{"ts": "%s", "event": "build", "ticket_id": "EU-9", "iteration": 1}' % OLD,
    ]
    _run(pdC, peer_lines=peer_open_ticket, cap_port=cap_port)
    _run(pdC, peer_lines=peer_open_ticket, cap_port=cap_port)
    chk("C: stale + open ticket (ticket_start with no terminal) alerts",
        len(_Capture.captured) == 1, f"captured={len(_Capture.captured)}")

    # ── D) FRESH (drain running, but recent) -> SILENT ──────────────────────────────
    baseD = Path(tempfile.mkdtemp(prefix="pwD-"))
    pdD = baseD / "shared"
    pdD.mkdir(parents=True)
    _Capture.captured = []
    peer_fresh = [
        '{"ts": "%s", "event": "autopilot_start", "mode": "live", "app": "automatixy"}' % FRESH,
    ]
    _run(pdD, peer_lines=peer_fresh, cap_port=cap_port)
    _run(pdD, peer_lines=peer_fresh, cap_port=cap_port)
    chk("D: fresh peer (recent event) is SILENT even with a drain running",
        len(_Capture.captured) == 0, f"captured={_Capture.captured}")

    # ── E) recovery: first FRESH check after an alert -> exactly ONE notice ─────────
    baseE = Path(tempfile.mkdtemp(prefix="pwE-"))
    pdE = baseE / "shared"
    pdE.mkdir(parents=True)
    _Capture.captured = []
    stale_lines = [
        '{"ts": "%s", "event": "autopilot_start", "mode": "live", "app": "EU"}' % OLD]
    _run(pdE, peer_lines=stale_lines, cap_port=cap_port)
    _run(pdE, peer_lines=stale_lines, cap_port=cap_port)   # -> alert
    chk("E: setup alerted (precondition)", len(_Capture.captured) == 1, f"{_Capture.captured}")
    _Capture.captured = []
    _run(pdE, peer_lines=[f'{{"ts": "{FRESH}", "event": "build", "ticket_id": "EU-9"}}'],
         cap_port=cap_port)
    chk("E: first fresh check after an alert sends exactly ONE recovery notice",
        len(_Capture.captured) == 1 and "recover" in _Capture.captured[0].lower(),
        f"captured={_Capture.captured[:1]}")
    # a second fresh check must NOT re-notify
    _Capture.captured = []
    _run(pdE, peer_lines=[f'{{"ts": "{FRESH}", "event": "build", "ticket_id": "EU-9"}}'],
         cap_port=cap_port)
    chk("E: a second fresh check sends no further notice (one recovery, not a stream)",
        len(_Capture.captured) == 0, f"captured={_Capture.captured}")

    # ── F) no peer data at all -> SILENT (stand-alone / not-yet-synced host) ────────
    baseF = Path(tempfile.mkdtemp(prefix="pwF-"))
    pdF = baseF / "shared"
    pdF.mkdir(parents=True)
    _Capture.captured = []
    _run(pdF, peer_lines=[""], cap_port=cap_port)
    _run(pdF, peer_lines=[""], cap_port=cap_port)
    chk("F: a peer file with no parseable event is SILENT (no false alarm on empty)",
        len(_Capture.captured) == 0, f"captured={_Capture.captured}")
finally:
    cap_srv.shutdown()

# ── verdict ──────────────────────────────────────────────────────────────────
passed = sum(1 for _, ok, _ in results if ok)
print(f"\neu428_peer_watchdog_test: {passed}/{len(results)} passed")
for n, ok, det in results:
    if not ok:
        print(f"  FAIL: {n}" + (f" — {det}" if det else ""))
sys.exit(0 if passed == len(results) else 1)
