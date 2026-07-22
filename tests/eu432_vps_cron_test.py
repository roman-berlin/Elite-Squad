"""EU-432 — VPS cron is 3h off and 2 of its 6 jobs have never once succeeded.

Three independent defects in the VPS crontab, all silent, plus no failure signal and an
unrotated, timestamp-less log. This harness pins every acceptance criterion offline:

  AC1 — schedules fire at intended LOCAL times: the installer drops the no-op CRON_TZ and
        states times in UTC (the VPS clock is Etc/UTC; Ubuntu cron ignores CRON_TZ). The daily
        08:30-IDT brief becomes 05:30 UTC; the weekly council 09:30-IDT becomes 06:30 UTC.
  AC2 — patrol: a job that cannot succeed must not stay scheduled. On the SERVER host there is
        no real product repo (automatixy.repo_path is a placeholder pointing at the orchestrator's
        own source), so `patrol automatixy` would file AUTO tickets against the product backlog
        from the orchestrator's code. The server installer must NOT schedule patrol.
  AC3 — swebench: the server's role is "NEVER builds" (config.server.example.yaml). The weekly
        swebench builder job (bare python3, no venv SDK, no datasets/swebench) cannot run there and
        must not be scheduled. PIN: a build with no configured gate is REFUSED, not passed —
        gate.run_gate on an app with no gate_commands returns passed=False.
  AC4 — a cron job that exits non-zero surfaces to Telegram on repeated failure. cron_guard
        raises EXACTLY ONE alert per N consecutive failures (not N alerts, not zero).
  AC5 — rotate council/cron.log and add timestamps to its lines.

All offline — SDK stubbed; no network, no real models, no real Telegram.
"""
import sys
import tempfile
import types
from pathlib import Path

# ── SDK stub (no real model calls) ──────────────────────────────────────────────
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k):
        self.__dict__.update(k)

    def __call__(self, *a, **k):
        return self


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

ROOT = Path(__file__).resolve().parent.parent

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# ══════════════════════════════════════════════════════════════════════════════
# Helper: pull the installed crontab block out of scripts/install-server-cron.sh
# (the lines between `cat >> "$TMP" <<'CRON'` and the closing `CRON`).
# ══════════════════════════════════════════════════════════════════════════════
def _cron_block() -> str:
    text = (ROOT / "scripts" / "install-server-cron.sh").read_text(encoding="utf-8")
    lines = text.splitlines()
    # The opener line carries `cat >> "$TMP" <<'CRON'`; the closer is a line that is exactly `CRON`.
    # (A naive `\nCRON` search falsely matches `\nCRON_TZ=…` and yields an empty block, which would
    # make every `… not in block` assertion pass vacuously — so parse line-by-line with teeth.)
    start = next(i for i, ln in enumerate(lines) if "<<'CRON'" in ln)
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "CRON")
    return "\n".join(lines[start + 1:end])


# ══════════════════════════════════════════════════════════════════════════════
# AC1 — times are UTC; CRON_TZ dropped; comment honest
# ══════════════════════════════════════════════════════════════════════════════
block = _cron_block()
_script_text = (ROOT / "scripts" / "install-server-cron.sh").read_text(encoding="utf-8")
chk("AC1: no CRON_TZ=Asia/Jerusalem setting in the installed block (Ubuntu cron ignores it)",
    "CRON_TZ=Asia" not in block)
chk("AC1: header comment names UTC honestly", "UTC" in _script_text)
chk("AC1: the false 'LOCAL via CRON_TZ' claim is gone from the comment",
    "LOCAL via CRON_TZ" not in _script_text)
# daily brief 08:30 IDT (UTC+3) -> 05:30 UTC
chk("AC1: daily stand-up scheduled at 05:30 UTC (= 08:30 IDT)",
    "30 5 * * *" in block and "30 8 * * *" not in block)
# weekly council Mon 09:30 IDT -> 06:30 UTC
chk("AC1: weekly council scheduled Mon 06:30 UTC (= 09:30 IDT)",
    "30 6 * * 1" in block and "30 9 * * 1" not in block)

# ══════════════════════════════════════════════════════════════════════════════
# AC2 — patrol not scheduled on the server (no real product target)
# ══════════════════════════════════════════════════════════════════════════════
chk("AC2: patrol is NOT scheduled on the server (no real target; would self-patrol)",
    "patrol" not in block)

# ══════════════════════════════════════════════════════════════════════════════
# AC3 — swebench not scheduled on the server (host never builds); gate refuses empty
# ══════════════════════════════════════════════════════════════════════════════
chk("AC3: swebench builder is NOT scheduled on the server (host role = never builds)",
    "swebench" not in block)

from orchestrator import gate                                  # noqa: E402
from orchestrator.config import AppConfig                      # noqa: E402

_empty = AppConfig(name="server-app", repo_path="/tmp", base_branch="DEV",
                   protected_branch="MAIN", backlog_backend="none", gate_commands=[])
_refused = gate.run_gate(_empty)
chk("AC3 PIN: empty gate_commands is REFUSED (not passed vacuously)", not _refused.passed, _refused.report)
chk("AC3 PIN: refusal report says REFUSED and cites EU-432",
    "REFUSED" in _refused.report and "EU-432" in _refused.report, _refused.report)

# ══════════════════════════════════════════════════════════════════════════════
# AC4 — cron_guard: exactly one alert per N consecutive failures
# ══════════════════════════════════════════════════════════════════════════════
from orchestrator import cron_guard                            # noqa: E402

# Pure decision function: fires once when streak crosses each multiple of the threshold.
last = 0
alerts = []
for streak in range(1, 11):           # 10 consecutive failures, threshold 3
    fire, last = cron_guard.decide_alert(streak, threshold=3, last_alert_blocks=last)
    if fire:
        alerts.append(streak)
chk("AC4: with threshold 3, alerts fire at streak 3 and 6 and 9 (one per N)", alerts == [3, 6, 9],
    str(alerts))
chk("AC4: never more than one alert per threshold block",
    cron_guard.decide_alert(3, 3, 0)[0] and not cron_guard.decide_alert(4, 3, 1)[0]
    and not cron_guard.decide_alert(5, 3, 1)[0])

# End-to-end: a failing command run through run_guarded alerts exactly once at the threshold,
# a succeeding run resets the streak, and a re-failure alerts again only after another N.
tmp = Path(tempfile.mkdtemp())
log = tmp / "cron.log"
state = tmp / "cron_health.json"
sent: list[str] = []


def fake_notifier(msg: str) -> None:
    sent.append(msg)


fail = [sys.executable, "-c", "import sys; print('boom'); sys.exit(2)"]
ok = [sys.executable, "-c", "print('all good')"]

for _ in range(3):                   # 3 consecutive failures -> 1 alert (threshold default 3)
    cron_guard.run_guarded("daily", fail, log_path=log, state_path=state,
                           notifier=fake_notifier, now=lambda: 1_700_000_000.0)
chk("AC4 e2e: 3 consecutive failures -> exactly ONE Telegram alert", len(sent) == 1, str(sent))
chk("AC4 e2e: the alert names the failing job", "daily" in sent[0], sent[0])

cron_guard.run_guarded("daily", ok, log_path=log, state_path=state,
                       notifier=fake_notifier, now=lambda: 1_700_000_100.0)
chk("AC4 e2e: a success does NOT alert", len(sent) == 1, str(sent))

for _ in range(3):                   # another 3 failures after recovery -> 1 more alert
    cron_guard.run_guarded("daily", fail, log_path=log, state_path=state,
                           notifier=fake_notifier, now=lambda: 1_700_000_200.0)
chk("AC4 e2e: after reset, N more failures -> exactly one MORE alert", len(sent) == 2, str(sent))

# run_guarded returns the wrapped command's exit code (so the guard never masks a failure).
rc = cron_guard.run_guarded("daily", fail, log_path=tmp / "l2.log", state_path=tmp / "s2.json",
                            notifier=fake_notifier, now=lambda: 1.0)
chk("AC4 e2e: run_guarded returns the wrapped command's non-zero exit code", rc == 2, str(rc))

# ══════════════════════════════════════════════════════════════════════════════
# AC5 — timestamps on every line + size-based rotation
# ══════════════════════════════════════════════════════════════════════════════
line = cron_guard.stamp_line("sync", "pulled=1 pushed=0\n", now=0)
chk("AC5: stamp_line prefixes an ISO-8601 UTC timestamp", line.startswith("[1970-01-01T00:00:00Z]"), line)
chk("AC5: stamp_line tags the job", "[sync]" in line, line)
chk("AC5: stamp_line strips the trailing newline", not line.endswith("\n"), repr(line))

# Rotation: a log over the size cap is rolled to .1, .2, … and capped at `keep`.
big = tmp / "rotate.log"
big.write_text("x" * 500, encoding="utf-8")
cron_guard.rotate_log(big, max_bytes=100, keep=2)
chk("AC5: oversized log is rotated away (path no longer holds the old blob)",
    not big.exists() or big.stat().st_size < 100, str(big.exists()))
chk("AC5: rotated copy lands at .1", (tmp / "rotate.log.1").exists())

# Multi-generation rotation + cap: build up 4 generations with keep=2, assert only .1/.2 survive.
rlog = tmp / "gen.log"
for i in range(4):
    rlog.write_text(f"gen{i} " + "y" * 200, encoding="utf-8")
    cron_guard.rotate_log(rlog, max_bytes=100, keep=2)
chk("AC5: rotation honours the keep cap (no .3/.4 leak beyond keep=2)",
    (tmp / "gen.log.2").exists() and not (tmp / "gen.log.3").exists())

# End-to-end timestamping: lines the guard writes to the log all carry a timestamp.
tl = tmp / "ts.log"
cron_guard.run_guarded("daily", ok, log_path=tl, state_path=tmp / "ts.json",
                       notifier=fake_notifier, now=lambda: 1_700_010_000.0)
written = tl.read_text(encoding="utf-8")
chk("AC5 e2e: every line written to the log carries a timestamp prefix",
    all(ln.startswith("[") for ln in written.splitlines() if ln.strip()), written)

# ══════════════════════════════════════════════════════════════════════════════
# Report
# ══════════════════════════════════════════════════════════════════════════════
print("\n===================== EU-432 VPS CRON QA =====================")
passed = sum(1 for _, ok_, _ in results if ok_)
for name, ok_, det in results:
    print(f"  [{'PASS' if ok_ else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok_ else ""))
print("-------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
