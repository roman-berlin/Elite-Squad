"""EU-403 — out-of-process watchdog QA.

The unit's every heartbeat lives INSIDE the serve process, so a wedged-but-alive process
(deadlock, hung HTTP, stuck event loop, or a machine that slept) is invisible until a human
opens the cockpit — the 2026-07-20 16:23→23:05 gap produced zero alerts. EU-403 adds a DUMB
external watchdog: a bash+curl script run by a separate launchd agent (or cron) that probes
the cockpit health endpoint and the audit log's freshness, alerting via Telegram on 2
consecutive failures and sending a recovery notice when it comes back.

This harness drives scripts/watchdog.sh as a black box with controlled inputs: a live or dead
cockpit endpoint, a RUNNING/STOPPED drain-intent file, a stale or fresh audit.jsonl, and a
LOCAL capture server standing in for Telegram (via the TELEGRAM_API_URL seam). It pins:
  • 1 failed probe → NO alert; 2 consecutive → ONE alert (the AC's ×2 rule).
  • a recovery notice on the first OK probe after an alert (the kill -CONT contract).
  • a freshness failure (drain RUNNING + stale audit) alerts even when HTTP is 200.
  • an IDLE serve (no drain RUNNING) NEVER false-alarms on a stale audit.
  • consecutive-fail state persists across runs in state/watchdog_state.
  • the re-alert cadence re-sends after RE_ALERT_EVERY more failures (a missed 3am ping isn't fatal).
  • tripwires: the launchd installer exists and is well-formed; the script is DUMB (curl +
    --max-time, and shares NOTHING with the cockpit — the word "orchestrator" appears nowhere);
    the installer is allow-listed by the scheduler single-source guard; DEPLOYMENT.md documents it.

Connection-refused (a dead port) stands in for a STOP'd/wedged serve: both make curl return a
non-200 code, exercising the identical fail→count→alert logic. The real kill -STOP→hang timing
is the MANUAL TEST contract (curl's --max-time is what bounds the hang).
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
WD = ROOT / "scripts" / "watchdog.sh"
INSTALLER = ROOT / "scripts" / "install-mac-watchdog-daemon.sh"
SCHEDULER_TEST = ROOT / "tests" / "scheduler_single_source_test.py"

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ── tiny HTTP fixtures ───────────────────────────────────────────────────────
class _Capture(BaseHTTPRequestHandler):
    """Stands in for api.telegram.org: records the URL-decoded `text` of each POST."""
    captured: list[str] = []

    def do_POST(self):  # noqa: N802 - http.server contract
        n = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        _Capture.captured.append(parse_qs(raw).get("text", [""])[0])
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence
        pass


class _HealthOK(BaseHTTPRequestHandler):
    """A healthy cockpit: 200 with a healthy body."""

    def do_GET(self):  # noqa: N802
        body = b'{"healthy": true, "problems": 0, "warnings": 0, "is_mac": true}'
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


def _dead_port() -> int:
    """A free port with nothing listening → connection refused (the dead-cockpit stand-in)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── run the watchdog with a fully-controlled environment ─────────────────────
def _run(tmp_state: Path, *, cockpit_url: str, intent: str, audit_age_s: int | None,
         cap_port: int, **extra_env) -> subprocess.CompletedProcess:
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "TMPDIR": tempfile.gettempdir(),
        "GENERAL_DIR": str(tmp_state),
        "STATE_DIR": str(tmp_state),
        "AUDIT_FILE": str(tmp_state / "audit.jsonl"),
        "INTENT_FILE": str(tmp_state / "autopilot_intent.json"),
        "COCKPIT_URL": cockpit_url,
        "HEALTH_TIMEOUT": "1",
        "STALE_SEC": "300",
        "FAIL_THRESHOLD": "2",
        "RE_ALERT_EVERY": "4",
        "TELEGRAM_API_URL": f"http://127.0.0.1:{cap_port}",
        "TELEGRAM_BOT_TOKEN": "fake-token",
        "TELEGRAM_CHAT_ID": "123456",
    }
    env.update({k: str(v) for k, v in extra_env.items()})
    # write the intent + audit fixtures the script reads
    (tmp_state / "autopilot_intent.json").write_text(intent, encoding="utf-8")
    audit = tmp_state / "audit.jsonl"
    audit.write_text(
        '{"ts": "2026-07-21T03:00:00+0000", "event": "agent_call", "ticket_id": "AUTO-1"}\n',
        encoding="utf-8")
    if audit_age_s is not None:
        when = time.time() - audit_age_s
        os.utime(audit, (when, when))
    return subprocess.run(["bash", str(WD)], env=env, capture_output=True,
                          text=True, timeout=60)


RUNNING = '{"": {"state": "RUNNING", "armed_ts": 1700000000.0, "pid": 1}}'
STOPPED = '{"": {"state": "STOPPED", "stopped_ts": 1700000000.0, "reason": "stand-down"}}'


# ════════════════════════════════════════════════════════════════════════════
# F) tripwires first (no live runs needed)
# ════════════════════════════════════════════════════════════════════════════
chk("watchdog script exists", WD.exists(), str(WD))
chk("launchd installer exists", INSTALLER.exists(), str(INSTALLER))

wd_text = WD.read_text(encoding="utf-8") if WD.exists() else ""
ins_text = INSTALLER.read_text(encoding="utf-8") if INSTALLER.exists() else ""

chk("watchdog probes via curl with a --max-time bound",
    "curl" in wd_text and "--max-time" in wd_text,
    "needs curl ... --max-time (the hang bound for a STOP'd serve)")
chk("watchdog is DUMB — shares nothing with the cockpit (no 'orchestrator' anywhere)",
    "orchestrator" not in wd_text.lower(),
    "the watchdog must not import/reference the cockpit's code so it can't share its failure modes")
chk("watchdog never exits non-zero (timer-driven; reports via Telegram, not via exit code)",
    'exit 0' in wd_text and 'set -e' not in wd_text,
    "a failed probe is a reportable condition, not a script abort")

chk("installer generates the plist at runtime (no committed *.plist under scripts/)",
    "cat >" in ins_text and ".plist" in ins_text,
    "must write the plist into ~/Library/LaunchAgents at install, like the cockpit daemon installer")
chk("installer carries the watchdog launchd label",
    "com.roman.general.watchdog" in ins_text, "label missing")
chk("installer sets a StartInterval timer (periodic, not KeepAlive respawn)",
    "StartInterval" in ins_text and "<integer>" in ins_text, "no StartInterval")
chk("installer invokes the watchdog script",
    "watchdog.sh" in ins_text, "doesn't reference scripts/watchdog.sh")

sched_text = SCHEDULER_TEST.read_text(encoding="utf-8") if SCHEDULER_TEST.exists() else ""
chk("scheduler single-source guard allow-lists the new installer",
    "install-mac-watchdog-daemon.sh" in sched_text,
    "add scripts/install-mac-watchdog-daemon.sh to SRC_ALLOW or that guard will red on the new launchd tokens")

deploy = (ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8") if (ROOT / "DEPLOYMENT.md").exists() else ""
chk("DEPLOYMENT.md documents the watchdog install",
    "install-mac-watchdog-daemon.sh" in deploy and "watchdog" in deploy.lower(),
    "no install instructions for the watchdog")


# ════════════════════════════════════════════════════════════════════════════
# Live behaviour: stand up the capture + healthy-cockpit servers
# ════════════════════════════════════════════════════════════════════════════
cap_srv = _serve(_Capture)
cap_port = cap_srv.server_address[1]
ok_srv = _serve(_HealthOK)
ok_port = ok_srv.server_address[1]
dead_url = f"http://127.0.0.1:{_dead_port()}/api/health"
ok_url = f"http://127.0.0.1:{ok_port}/api/health"

try:
    # ── A) HTTP failure: 1 probe → no alert; 2nd → ONE alert ─────────────────
    stateA = Path(tempfile.mkdtemp(prefix="wdA-"))
    (stateA / "watchdog_state").unlink(missing_ok=True)   # fresh
    _Capture.captured = []
    _run(stateA, cockpit_url=dead_url, intent=STOPPED, audit_age_s=60, cap_port=cap_port)
    chk("A: 1 failed probe sends NO alert", len(_Capture.captured) == 0,
        f"captured={_Capture.captured}")
    st = (stateA / "watchdog_state").read_text() if (stateA / "watchdog_state").exists() else ""
    chk("A: consecutive-fail state persists (fail_count=1 after one fail)",
        "fail_count=1" in st, f"state={st!r}")
    _run(stateA, cockpit_url=dead_url, intent=STOPPED, audit_age_s=60, cap_port=cap_port)
    chk("A: 2nd consecutive fail sends exactly ONE alert",
        len(_Capture.captured) == 1, f"captured={len(_Capture.captured)}")
    chk("A: the alert names the unreachable cockpit and carries audit context",
        _Capture.captured and "WATCHDOG" in _Capture.captured[0]
        and ("unreachable" in _Capture.captured[0] or "wedged" in _Capture.captured[0]),
        f"captured={_Capture.captured[:1]}")

    # ── B) recovery: first OK after an alert → recovery notice ───────────────
    _Capture.captured = []
    _run(stateA, cockpit_url=ok_url, intent=STOPPED, audit_age_s=60, cap_port=cap_port)
    chk("B: first OK after an alert sends a recovery notice",
        len(_Capture.captured) == 1 and "recover" in _Capture.captured[0].lower(),
        f"captured={_Capture.captured[:1]}")
    _st = stateA / "watchdog_state"
    st = _st.read_text() if _st.exists() else ""
    chk("B: recovery resets fail_count to 0 and clears the alerted flag",
        "fail_count=0" in st and "alerted=0" in st, f"state={st!r}")

    # ── C) freshness: drain RUNNING + stale audit + HTTP 200 → alert ─────────
    stateC = Path(tempfile.mkdtemp(prefix="wdC-"))
    (stateC / "watchdog_state").unlink(missing_ok=True)
    _Capture.captured = []
    _run(stateC, cockpit_url=ok_url, intent=RUNNING, audit_age_s=7200, cap_port=cap_port)
    _run(stateC, cockpit_url=ok_url, intent=RUNNING, audit_age_s=7200, cap_port=cap_port)
    chk("C: drain RUNNING + stale audit alerts even with HTTP 200 (the wedged-loop case)",
        len(_Capture.captured) == 1 and "stale" in _Capture.captured[0].lower(),
        f"captured={_Capture.captured[:1]}")

    # ── D) idle: NO drain RUNNING + stale audit + HTTP 200 → NO false alarm ──
    stateD = Path(tempfile.mkdtemp(prefix="wdD-"))
    (stateD / "watchdog_state").unlink(missing_ok=True)
    _Capture.captured = []
    _run(stateD, cockpit_url=ok_url, intent=STOPPED, audit_age_s=7200, cap_port=cap_port)
    _run(stateD, cockpit_url=ok_url, intent=STOPPED, audit_age_s=7200, cap_port=cap_port)
    chk("D: an IDLE serve (no drain) never false-alarms on a stale audit",
        len(_Capture.captured) == 0, f"captured={_Capture.captured}")

    # ── G) re-alert cadence: a missed first ping is repeated ─────────────────
    stateG = Path(tempfile.mkdtemp(prefix="wdG-"))
    (stateG / "watchdog_state").unlink(missing_ok=True)
    _Capture.captured = []
    # FAIL_THRESHOLD=2, RE_ALERT_EVERY=4 → over 5 consecutive fails the alert fires at
    # fail_count 2 (first) and 4 (re-alert), not at 1/3/5 → exactly 2 messages.
    for _ in range(5):
        _run(stateG, cockpit_url=dead_url, intent=STOPPED, audit_age_s=60, cap_port=cap_port,
             FAIL_THRESHOLD="2", RE_ALERT_EVERY="4")
    chk("G: re-alert cadence re-sends (2 alerts over 5 fails: first + one re-alert)",
        len(_Capture.captured) == 2, f"captured={len(_Capture.captured)}")
finally:
    cap_srv.shutdown()
    ok_srv.shutdown()


# ── verdict ──────────────────────────────────────────────────────────────────
passed = sum(1 for _, ok, _ in results if ok)
print(f"\neu403_watchdog_test: {passed}/{len(results)} passed")
for n, ok, det in results:
    if not ok:
        print(f"  FAIL: {n}" + (f" — {det}" if det else ""))
sys.exit(0 if passed == len(results) else 1)
