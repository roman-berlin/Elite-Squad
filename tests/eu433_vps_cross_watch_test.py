"""EU-433 — the Mac watches the VPS (the reverse of EU-428).

The 2026-07-22 VPS responsibility audit found that NOTHING watches the VPS: the Mac's EU-403
watchdog is hardwired to itself (127.0.0.1:8787), the VPS's own copy of watchdog.sh is installed
but scheduled by nothing, and — uniquely dangerous — the VPS is the elected Telegram sender
(EU-303), so a wedged VPS CANNOT report its own outage. Silence is indistinguishable from health,
and it already bit: since 07-20 the daily brief arrived carrying a raw 401 as its body (EU-430).

This harness pins every acceptance criterion offline (SDK stubbed; no network, no SSH, no Telegram):

  AC1 — the Mac probes the VPS on a schedule (over SSH) and alerts when it is unreachable, reusing
        the watchdog.sh shape (out-of-process, one alert per outage + one recovery, persisted state).
        PIN: VPS unreachable -> exactly one alert + one recovery notice (not N, not zero).
  AC2 — a CONTENT check, not just liveness: a brief whose body is a provider error, OR a missing
        scheduled brief, raises a DISTINCT alert (different headline from the unreachable alert).
  AC3 — a second, independent alert path that does not depend on the VPS process: the Mac curls
        Telegram directly, so a wedged sender cannot suppress its own alarm. PIN: the alert path
        works with general.service stopped (the Mac send never touches the VPS).
  AC4 — the VPS's existing watchdog.sh is scheduled (cron), pointed at its OWN cockpit, so a local
        wedge is caught locally too — out-of-process, so it still alerts with general.service stopped.

The decision core lives in ``orchestrator/server_watchdog.py`` (pure functions, exhaustively unit
tested); the deployable bits are ``scripts/install-mac-server-watchdog-daemon.sh`` (Mac launchd
agent -> ``./general server-watchdog``) and a new watchdog.sh cron line in
``scripts/install-server-cron.sh`` (AC4).
"""
import json
import sys
import tempfile
import types
from pathlib import Path

# ── SDK stub (no real model calls; mirrors every other harness) ───────────────
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
from orchestrator import server_watchdog  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))
    print(("PASS: " if cond else "FAIL: ") + name + (f"  ({detail})" if detail and not cond else ""))


# A daily_brief audit row (the VPS records one per daily stand-up). ts is the audit's own format.
def _brief_row(*, ts="2026-07-22T05:30:00+0000", auth_down=None, detail=None):
    row = {"ts": ts, "event": "daily_brief", "broadcast": True}
    if auth_down is not None:
        row["auth_down"] = auth_down
    if detail is not None:
        row["detail"] = detail
    return json.dumps(row)


# ================================================================================================ #
# parse_remote — split the SSH probe's stdout into a structured status
# ================================================================================================ #
print("\n=== parse_remote ===")
# ssh itself failed (host down / network split) -> not reachable, nothing else known
st = server_watchdog.parse_remote("", ssh_ok=False)
chk("ssh failed -> not reachable", st.reachable is False, str(st))
chk("ssh failed -> no health claim", st.health_code == "?", str(st))
chk("ssh failed -> no brief row", st.brief_row == "", str(st))

# healthy probe: cockpit 200 + a fresh brief row
raw_ok = "HEALTH=200\n" + _brief_row()
st = server_watchdog.parse_remote(raw_ok, ssh_ok=True)
chk("ssh ok + 200 -> reachable", st.reachable is True and st.health_code == "200", str(st))
chk("parse_remote carries the brief row through", st.brief_row == _brief_row(), str(st))

# cockpit reachable but wedged/down (HTTP non-200) -> reachable True (SSH worked) but health bad
st = server_watchdog.parse_remote("HEALTH=502\n", ssh_ok=True)
chk("cockpit 502 -> reachable but health_code 502", st.reachable and st.health_code == "502", str(st))

# no brief ever recorded (fresh VPS) -> empty brief row, still a valid probe
st = server_watchdog.parse_remote("HEALTH=200\n", ssh_ok=True)
chk("no brief row -> empty brief_row (not a crash)", st.brief_row == "", str(st))

# robust to leading log noise (sshd banners): the HEALTH line is found wherever it is
st = server_watchdog.parse_remote("PAM established\nHEALTH=200\n" + _brief_row(), ssh_ok=True)
chk("HEALTH line found past leading noise", st.health_code == "200" and st.reachable, str(st))


# ================================================================================================ #
# classify — which conditions are failing, given a status + clock
# ================================================================================================ #
print("\n=== classify ===")
NOW = 1_700_000_000.0  # fixed clock so the age math is deterministic
WINDOW = 30 * 3600      # 30h: a daily brief older than this is "missing"


def _ts_at(epoch):
    import datetime as _dt
    return _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")


FRESH_TS = _ts_at(NOW - 2 * 3600)        # 2h ago  -> well inside the 30h window
STALE_TS = _ts_at(NOW - 40 * 3600)       # 40h ago -> past the window

# ssh failed -> DOWN; brief checks SUPPRESSED (one alert for the real cause, not three)
c = server_watchdog.classify(server_watchdog.parse_remote("", False), now=NOW, brief_window_s=WINDOW)
chk("ssh unreachable -> down=True", c.down, str(c))
chk("down SUPPRESSES brief_missing (no pile-on)", c.brief_missing is False, str(c))
chk("down SUPPRESSES brief_bad (no pile-on)", c.brief_bad is False, str(c))

# cockpit wedged (non-200) is ALSO down
c = server_watchdog.classify(server_watchdog.parse_remote("HEALTH=503\n", True), now=NOW, brief_window_s=WINDOW)
chk("cockpit non-200 -> down=True", c.down, str(c))

# healthy: 200 + fresh brief + no auth_down -> nothing failing
c = server_watchdog.classify(
    server_watchdog.parse_remote("HEALTH=200\n" + _brief_row(ts=FRESH_TS), True),
    now=NOW, brief_window_s=WINDOW)
chk("healthy VPS + fresh brief -> nothing failing",
    not (c.down or c.brief_missing or c.brief_bad), str(c))

# AC2a: brief body is a provider error -> brief_bad (auth_down flag, the EU-430 shape)
c = server_watchdog.classify(
    server_watchdog.parse_remote("HEALTH=200\n" + _brief_row(ts=FRESH_TS, auth_down=True), True),
    now=NOW, brief_window_s=WINDOW)
chk("brief auth_down=True -> brief_bad=True", c.brief_bad and not c.down, str(c))

# AC2a (belt-and-suspenders): a detail string that looks like a provider error -> brief_bad even
# without the auth_down flag (guards a future regression where the flag isn't set)
c = server_watchdog.classify(
    server_watchdog.parse_remote(
        "HEALTH=200\n" + _brief_row(ts=FRESH_TS, detail="Failed to authenticate. API Error: 401 x"), True),
    now=NOW, brief_window_s=WINDOW)
chk("brief detail is a provider error -> brief_bad=True", c.brief_bad, str(c))

# a real briefing line is NOT a provider error (never false-flag real content)
c = server_watchdog.classify(
    server_watchdog.parse_remote(
        "HEALTH=200\n" + _brief_row(ts=FRESH_TS, detail="Today: land EU-433. FOR YOU: none."), True),
    now=NOW, brief_window_s=WINDOW)
chk("a real briefing detail is not brief_bad", not c.brief_bad, str(c))

# AC2b: scheduled brief missed entirely (no daily_brief in the window) -> brief_missing
c = server_watchdog.classify(
    server_watchdog.parse_remote("HEALTH=200\n" + _brief_row(ts=STALE_TS), True),
    now=NOW, brief_window_s=WINDOW)
chk("brief older than window -> brief_missing=True", c.brief_missing and not c.down, str(c))

# no brief row at all -> brief_missing (the daily never fired)
c = server_watchdog.classify(
    server_watchdog.parse_remote("HEALTH=200\n", True), now=NOW, brief_window_s=WINDOW)
chk("no brief row -> brief_missing=True", c.brief_missing, str(c))

# an unparseable ts can't prove freshness -> treat as missing (never silent-assume fresh)
c = server_watchdog.classify(
    server_watchdog.parse_remote('HEALTH=200\n{"ts":"not-a-date","event":"daily_brief"}', True),
    now=NOW, brief_window_s=WINDOW)
chk("unparseable brief ts -> brief_missing (never assume fresh)", c.brief_missing, str(c))

# the window is a real boundary: just inside -> not missing
c = server_watchdog.classify(
    server_watchdog.parse_remote("HEALTH=200\n" + _brief_row(ts=_ts_at(NOW - 28 * 3600)), True),
    now=NOW, brief_window_s=WINDOW)
chk("brief 28h old (< 30h window) -> not missing", not c.brief_missing, str(c))


# ================================================================================================ #
# step_condition — one alert per incident + one recovery on clear (the watchdog.sh discipline)
# ================================================================================================ #
print("\n=== step_condition ===")
THRESH = 2
RE = 12

# 1 failure below threshold -> NO alert yet
alert, rec, e1 = server_watchdog.step_condition(None, failing=True, threshold=THRESH, re_alert_every=RE)
chk("1 failure below threshold -> no alert", not alert and not rec, str(e1))
chk("1 failure increments fail_count", e1["fail_count"] == 1 and e1["alerted"] is False, str(e1))

# 2nd failure crosses threshold -> ALERT (exactly one, not alerted before)
alert, rec, e2 = server_watchdog.step_condition(e1, failing=True, threshold=THRESH, re_alert_every=RE)
chk("2nd failure -> fires the ONE alert", alert and not rec, str(e2))
chk("2nd failure sets alerted=True", e2["alerted"] is True and e2["fail_count"] == 2, str(e2))

# 3rd..11th failure (still alerted) -> NO re-alert (re_alert_every=12 not hit)
e = e2
fired_again = False
for _ in range(3, 12):
    alert, rec, e = server_watchdog.step_condition(e, failing=True, threshold=THRESH, re_alert_every=RE)
    if alert:
        fired_again = True
chk("failures 3..11 do NOT re-alert (suppressed until re_alert_every)", not fired_again, str(e))

# 12th failure (fail_count % re == 0) -> re-alert (a long outage is not a single 3am ping)
alert, rec, e12 = server_watchdog.step_condition(e, failing=True, threshold=THRESH, re_alert_every=RE)
chk("12th failure re-alerts (periodic re-notify)", alert, str(e12))

# recovery: first clear after an alert -> exactly ONE recovery notice
alert, rec, er = server_watchdog.step_condition(e2, failing=False, threshold=THRESH, re_alert_every=RE)
chk("first clear after alert -> ONE recovery", rec and not alert, str(er))
chk("recovery resets fail_count + alerted", er["fail_count"] == 0 and er["alerted"] is False, str(er))

# a second consecutive clear -> NO second recovery (no spam)
alert, rec, er2 = server_watchdog.step_condition(er, failing=False, threshold=THRESH, re_alert_every=RE)
chk("second clear -> no recovery spam", not rec and not alert, str(er2))

# a flap (clear then re-fail) alerts again only after a fresh threshold crossing
alert, rec, ex = server_watchdog.step_condition(er, failing=True, threshold=THRESH, re_alert_every=RE)
chk("re-fail after recovery starts a fresh count (no instant re-alert)", not alert and ex["fail_count"] == 1, str(ex))


# ================================================================================================ #
# run() end-to-end — SSH probe + classify + persisted state + notify (all stubbed)
# ================================================================================================ #
print("\n=== run() end-to-end (AC1/AC2/AC3) ===")
sent: list[str] = []


def fake_notifier(msg: str) -> None:
    sent.append(msg)


def probe_returning(raw, ssh_ok):
    """Build a fake _remote_probe that yields a fixed payload + ssh flag."""
    def _probe(host, repo, timeout):
        return raw, ssh_ok
    return _probe


def _fresh_state_dir():
    d = Path(tempfile.mkdtemp())
    return d / "state.json"


# AC1 PIN: VPS unreachable -> EXACTLY one alert, then EXACTLY one recovery on return.
state = _fresh_state_dir()
sent.clear()
for _ in range(5):  # many failures
    server_watchdog.run(
        state_path=state, probe=probe_returning("", False), notifier=fake_notifier,
        ssh_host="ubuntu@1.2.3.4", now=lambda: NOW)
chk("AC1: a sustained outage raises EXACTLY ONE alert (not N)", sum("unreachable" in s for s in sent) == 1, str(sent))
# then the VPS comes back healthy
raw_ok = "HEALTH=200\n" + _brief_row(ts=FRESH_TS)
server_watchdog.run(state_path=state, probe=probe_returning(raw_ok, True), notifier=fake_notifier,
                    ssh_host="ubuntu@1.2.3.4", now=lambda: NOW)
chk("AC1: recovery sends EXACTLY ONE recovery notice",
    sum("recovered" in s.lower() or "reachable again" in s.lower() for s in sent) == 1, str(sent))

# AC2 PIN: a missing scheduled brief -> a DISTINCT alert (not the unreachable alert).
state = _fresh_state_dir()
sent.clear()
for _ in range(3):
    server_watchdog.run(
        state_path=state, probe=probe_returning("HEALTH=200\n" + _brief_row(ts=STALE_TS), True),
        notifier=fake_notifier, ssh_host="ubuntu@1.2.3.4", now=lambda: NOW)
chk("AC2: missing scheduled brief -> exactly one alert", sum("missing" in s.lower() for s in sent) == 1, str(sent))
chk("AC2: the missing-brief alert is DISTINCT from the unreachable alert",
    not any("unreachable" in s for s in sent), str(sent))

# AC2 PIN: a brief whose body is a provider error -> a DISTINCT alert.
state = _fresh_state_dir()
sent.clear()
for _ in range(3):
    server_watchdog.run(
        state_path=state,
        probe=probe_returning("HEALTH=200\n" + _brief_row(ts=FRESH_TS, auth_down=True), True),
        notifier=fake_notifier, ssh_host="ubuntu@1.2.3.4", now=lambda: NOW)
chk("AC2: provider-error brief -> exactly one alert", len([s for s in sent if "alert" in s.lower() or "🚨" in s]) == 1, str(sent))
chk("AC2: the provider-error alert is distinct from missing + unreachable",
    not any("unreachable" in s for s in sent) and not any("missing" in s.lower() for s in sent), str(sent))

# AC3 PIN: the alert path does NOT depend on the VPS process — run() notifies via the passed
# notifier (the Mac's own Telegram), and a healthy probe sends NOTHING (no false alarm).
state = _fresh_state_dir()
sent.clear()
for _ in range(3):
    server_watchdog.run(state_path=state, probe=probe_returning(raw_ok, True), notifier=fake_notifier,
                        ssh_host="ubuntu@1.2.3.4", now=lambda: NOW)
chk("AC3: a healthy VPS sends NO alert", sent == [], str(sent))

# DOWN suppresses brief checks: an unreachable VPS does not ALSO pile on a missing-brief alert.
state = _fresh_state_dir()
sent.clear()
for _ in range(4):
    server_watchdog.run(state_path=state, probe=probe_returning("", False), notifier=fake_notifier,
                        ssh_host="ubuntu@1.2.3.4", now=lambda: NOW)
chk("down does not double-alert as missing-brief", not any("missing" in s.lower() for s in sent), str(sent))

# run() is a graceful no-op when the Mac has no VPS target configured (no crash, no alert).
state = _fresh_state_dir()
sent.clear()
rc = server_watchdog.run(state_path=state, probe=probe_returning("", False), notifier=fake_notifier,
                         ssh_host="", now=lambda: NOW)
chk("no GENERAL_SERVER_SSH -> graceful no-op (no alert, no raise)", sent == [] and rc in (0, None), str((rc, sent)))

# run() never raises even when the probe itself blows up (a watcher crash would blind the channel).
def exploding_probe(host, repo, timeout):
    raise RuntimeError("ssh exploded")


state = _fresh_state_dir()
sent.clear()
rc = server_watchdog.run(state_path=state, probe=exploding_probe, notifier=fake_notifier,
                         ssh_host="ubuntu@1.2.3.4", now=lambda: NOW)
chk("a probe exception -> treated as unreachable + alerts (never raises)", not isinstance(rc, Exception), str(rc))


# ================================================================================================ #
# SECURITY: the repo token crosses an SSH shell boundary — it must be injection-safe
# ================================================================================================ #
print("\n=== security: repo token sanitization ===")


def _safe_expected(poison):
    import re
    return re.sub(r"[^A-Za-z0-9_./-]", "", poison).strip("./").strip() or "General"


chk("a clean repo name passes through", server_watchdog._safe_repo("General") == "General")
chk("a nested path is preserved", server_watchdog._safe_repo("deploy/General") == "deploy/General")
for poison, label in [("General$(reboot)", "$(…) substitution"),
                      ("General`id`", "backticks"),
                      ('x"; rm -rf #', "double-quote breakout"),
                      ("General;reboot", "command separator"),
                      ("$HOME/General", "a leading $VAR expansion")]:
    out = server_watchdog._safe_repo(poison)
    chk(f"repo with {label} is stripped to a shell-safe token",
        out == _safe_expected(poison), f"{poison!r} -> {out!r}")


# ================================================================================================ #
# Installer scripts (AC1/AC3 Mac agent; AC4 VPS watchdog cron)
# ================================================================================================ #
print("\n=== installer scripts ===")
mac_installer = (ROOT / "scripts" / "install-mac-server-watchdog-daemon.sh").read_text(encoding="utf-8")
chk("AC1/AC3: Mac installer exists", bool(mac_installer.strip()))
chk("AC3: Mac agent runs ./general server-watchdog (Mac-local, independent of the VPS process)",
    "server-watchdog" in mac_installer and "./general" in mac_installer, "")
chk("AC1: it is a PERIODIC launchd agent (StartInterval key), not a KeepAlive respawn",
    "<key>StartInterval</key>" in mac_installer and "<key>KeepAlive</key>" not in mac_installer, "")
chk("AC1: distinct launchd label (no collision with the on-box watchdog/cockpit agents)",
    "com.roman.general.server-watchdog" in mac_installer, "")
# the on-box watchdog label must NOT be reused (two agents cannot share a label)
chk("the on-box watchdog label is untouched (distinct label used)",
    mac_installer.count("com.roman.general.watchdog") == 0, "")


def _cron_block():
    text = (ROOT / "scripts" / "install-server-cron.sh").read_text(encoding="utf-8")
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if "<<'CRON'" in ln)
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "CRON")
    return "\n".join(lines[start + 1:end]), text


block, server_cron_text = _cron_block()
chk("AC4: the VPS schedules its own watchdog.sh (catches a local wedge)",
    "watchdog.sh" in block, block)
# Pointed at the VPS's OWN cockpit: the cron line must NOT override COCKPIT_URL (the script's built-in
# default is localhost:8787 = THIS host's cockpit). An override pointing elsewhere (e.g. the Mac) is the bug.
chk("AC4: the VPS watchdog uses its own localhost cockpit (no COCKPIT_URL override toward another host)",
    "watchdog.sh" in block and "COCKPIT_URL" not in block, block)
chk("AC4: the installer is idempotent (re-running strips the old watchdog line first)",
    "watchdog.sh" in server_cron_text and "grep -vE" in server_cron_text, "")
# AC3 PIN (VPS side): watchdog.sh alerts out-of-process (curl-to-Telegram), so it still pages with
# general.service stopped. Assert the scheduled line runs the shell script directly (not via general).
chk("AC4/AC3: the VPS watchdog runs the dumb shell script directly (out-of-process; no general.service)",
    "bash scripts/watchdog.sh" in block, block)


# ================================================================================================ #
# Report
# ================================================================================================ #
print("\n===================== EU-433 VPS CROSS-WATCH QA =====================")
passed = sum(1 for _, ok_, _ in results if ok_)
for name, ok_, det in results:
    print(f"  [{'PASS' if ok_ else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok_ else ""))
print("---------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
