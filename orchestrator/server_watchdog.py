"""orchestrator/server_watchdog.py — the Mac watches the VPS (EU-433).

The mirror of EU-428: there the always-on VPS watches the (sleeping) Mac; HERE the Mac watches the
VPS. The 2026-07-22 VPS responsibility audit found that *nothing* watches the VPS, and — uniquely
dangerous — the VPS is the elected Telegram sender (``decisions.should_poll_telegram``, EU-303), so a
wedged VPS **cannot report its own outage**: the only alert channel IS the VPS, so silence is
indistinguishable from health. It was already happening mildly: since 07-20 the daily brief arrived
carrying a raw 401 as its body (EU-430), and "a brief arrived" looked like success.

This module is the Mac-side cross-host watcher. It is deliberately split into a PURE decision core
(parse → classify → step) that is exhaustively unit-tested, and a thin ``run()`` that does the one
side-effectful thing (SSH the VPS, notify via Telegram). Invoked from a SEPARATE Mac launchd agent as
``./general server-watchdog`` (see ``scripts/install-mac-server-watchdog-daemon.sh``), so — like the
EU-403 on-box watchdog — it runs OUT OF the serve process and cannot share its failure modes, and it
curls Telegram from the MAC, so the alert path is independent of the VPS process entirely (AC3: a
wedged sender cannot suppress its own alarm).

WHAT IT CHECKS, each run (one SSH round-trip that runs a dumb remote probe):

  1. AC1 — LIVENESS. The SSH connection itself is the reachability probe; the remote then curls the
     VPS cockpit (``http://127.0.0.1:8787/api/health``) for a health code. SSH refused/timed out, OR
     a non-200 cockpit, is the ``down`` condition → ONE alert + ONE recovery (the watchdog.sh shape:
     out-of-process, persisted fail-count + alerted state, a periodic re-notify so a long outage is
     not a single 3am ping).
  2. AC2 — CONTENT. A liveness ping is not health: the VPS's newest ``daily_brief`` audit row is read
     over the same SSH bridge. A brief that never arrived within the window (``brief_missing``) OR
     whose body is a provider error (``brief_bad`` — the ``auth_down`` flag EU-430 records, plus the
     ``looks_like_provider_error`` marker as a belt-and-suspenders guard) raises a DISTINCT alert.
  3. While ``down`` the brief conditions are not observable, so they are SUPPRESSED (state frozen) —
     one alert for the real cause, never a pile-on of three.

Resilience to the Mac sleeping (AC1 caveat): a launchd ``StartInterval`` does not fire while the host
sleeps, but fires once on wake, so a VPS outage that began mid-sleep is caught on wake + after the
2-check threshold. Absence of a check is not evidence of health, so this watcher is PAIRED with the
VPS's OWN out-of-process watchdog (AC4, scheduled by ``install-server-cron.sh``), which runs 24/7 on
the always-on box and catches a local wedge the sleeping Mac cannot see.

Never raises, never exits non-zero (it is timer-driven; its job is to REPORT via Telegram, not to
signal the scheduler) — mirroring ``watchdog.sh`` / ``cron_guard``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# ── tunables (all overridable via env, mirroring watchdog.sh) ─────────────────
HEALTH_TIMEOUT_S = 8          # the REMOTE curl --max-time (bounds a hung cockpit)
SSH_CONNECT_TIMEOUT_S = 10    # ssh -o ConnectTimeout (bounds a dead-host hang)
SSH_TIMEOUT_S = 30            # overall subprocess timeout for the SSH round-trip
FAIL_THRESHOLD = 2            # consecutive failures before the FIRST alert
RE_ALERT_EVERY = 12           # re-notify every N further fails (~hourly at a 5-min cadence)
BRIEF_WINDOW_S = 30 * 3600    # a daily brief older than 30h is "missing" (1 daily cycle + grace)
STATE_FILE = "server_watchdog_state.json"

# The three independent conditions, in alert-priority order.
CONDITIONS = ("down", "brief_missing", "brief_bad")

# Shell/path-safe chars only for the remote repo token: it is interpolated into a double-quoted
# remote assignment over SSH, so any of $ ` " ! ; & | ( ) could inject. GENERAL_SERVER_REPO is
# operator-set (trusted), but never carry an env value verbatim across an SSH shell boundary.
_REPO_SAFE = re.compile(r"[^A-Za-z0-9_./-]")


def _safe_repo(repo: str | None) -> str:
    """Strip anything that could break out of the remote ``$HOME/<repo>/state/audit.jsonl`` path
    assignment; fall back to the canonical VPS checkout name ``General``. Defense-in-depth only."""
    cleaned = _REPO_SAFE.sub("", repo or "").strip("./").strip()
    return cleaned or "General"


@dataclass
class RemoteStatus:
    """The parsed result of one SSH probe of the VPS."""

    reachable: bool       # the SSH connection succeeded (the AC1 reachability signal)
    health_code: str      # "200" / "502" / "000" / "?" ("?" = SSH ok but no coherent HEALTH line)
    brief_row: str        # the newest daily_brief audit row (raw JSON), "" if none


@dataclass
class Conditions:
    """Which conditions are failing this run, plus the detail the alerts carry."""

    down: bool
    brief_missing: bool
    brief_bad: bool
    health_code: str = "?"
    brief_age_s: float | None = None
    brief_auth_down: bool = False
    brief_detail: str = ""


# --------------------------------------------------------------------------- #
# parse_remote — split the SSH probe's stdout into a structured status (PURE)
# --------------------------------------------------------------------------- #
def parse_remote(raw: str, ssh_ok: bool) -> RemoteStatus:
    """Parse the remote probe's combined stdout.

    The remote emits one ``HEALTH=<code>`` line (the cockpit curl) and, optionally, the newest
    ``daily_brief`` audit row as a single JSON line. Leading noise (sshd banners) is tolerated: the
    HEALTH line is located anywhere, and the brief row is the last line that looks like JSON. When
    SSH itself failed the host is not reachable and nothing else is knowable."""
    if not ssh_ok:
        return RemoteStatus(reachable=False, health_code="?", brief_row="")
    health = "?"
    for ln in (raw or "").splitlines():
        line = ln.strip()
        if line.startswith("HEALTH="):
            health = line[len("HEALTH="):].strip() or "?"
            break
    # the brief row is the last JSON-object line (audit rows start with '{'); not the HEALTH line
    brief = ""
    for ln in (raw or "").splitlines():
        line = ln.strip()
        if line.startswith("{") and line.endswith("}"):
            brief = line
    return RemoteStatus(reachable=True, health_code=health, brief_row=brief)


def _parse_ts(ts) -> float | None:
    """A daily_brief row's ``ts`` (the audit format ``%Y-%m-%dT%H:%M:%S%z``) as epoch seconds, or
    None when absent/unparseable. Bare (no offset) timestamps are read as UTC — mirrors
    ``sync._parse_iso_epoch`` without importing the git-clone-heavy sync module here."""
    if not ts:
        return None
    import datetime as _dt
    s = str(ts).strip().strip('"').strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = _dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.timestamp()
    return None


# --------------------------------------------------------------------------- #
# classify — which conditions are failing, given a status + clock (PURE)
# --------------------------------------------------------------------------- #
def classify(status: RemoteStatus, *, now: float, brief_window_s: float) -> Conditions:
    """Map one probe to the set of failing conditions.

    ``down`` suppresses the brief conditions: while the VPS is unreachable or its cockpit is not
    healthy, the brief row cannot be observed, so ``brief_missing``/``brief_bad`` are held False
    (and ``run`` freezes their state) — one alert for the real cause, never a three-way pile-on.

    ``brief_missing`` is true when no daily brief arrived within the window (no row, an unparseable
    ts, or a ts older than ``brief_window_s``) — never silent-assume fresh. ``brief_bad`` is true
    when the newest brief's body is a provider error: the ``auth_down`` flag EU-430 records on the
    row, OR a ``detail`` that ``looks_like_provider_error`` (a guard against a future regression
    that forgets to set the flag). A real briefing line never trips it."""
    down = (not status.reachable) or (status.health_code != "200")
    if down:
        return Conditions(down=True, brief_missing=False, brief_bad=False,
                          health_code=status.health_code)

    brief_age: float | None = None
    auth_down = False
    detail = ""
    present = bool(status.brief_row)
    if present:
        try:
            row = json.loads(status.brief_row)
        except ValueError:
            row = {}
        ts_epoch = _parse_ts(row.get("ts"))
        brief_age = (now - ts_epoch) if ts_epoch is not None else None
        auth_down = bool(row.get("auth_down"))
        detail = str(row.get("detail") or "")

    missing = (not present) or (brief_age is None) or (brief_age > brief_window_s)
    from . import auth_probe
    bad = present and (auth_down or auth_probe.looks_like_provider_error(detail))
    return Conditions(down=False, brief_missing=missing, brief_bad=bad,
                      health_code=status.health_code, brief_age_s=brief_age,
                      brief_auth_down=auth_down, brief_detail=detail)


# --------------------------------------------------------------------------- #
# step_condition — one alert per incident + one recovery on clear (PURE)
# --------------------------------------------------------------------------- #
def step_condition(entry: dict | None, *, failing: bool, threshold: int,
                   re_alert_every: int) -> tuple[bool, bool, dict]:
    """Advance one condition's persisted state by one observation.

    Returns ``(send_alert, send_recovery, new_entry)``. Mirrors the EU-403 ``watchdog.sh`` /
    ``cron_guard`` discipline: the FIRST alert fires after ``threshold`` consecutive failures; a
    periodic re-notify fires every ``re_alert_every`` further failures (so a sustained outage is not
    a single easily-missed ping); the first CLEAR after an alert sends exactly ONE recovery; a second
    clear is silent. ``entry=None`` starts a fresh condition. Never raises."""
    e = {"fail_count": 0, "alerted": False} if not entry else dict(entry)
    e.setdefault("fail_count", 0)
    e.setdefault("alerted", False)
    e["fail_count"] = int(e.get("fail_count") or 0)
    e["alerted"] = bool(e.get("alerted"))

    if not failing:
        recovery = e["alerted"]
        e["fail_count"] = 0
        e["alerted"] = False
        return (False, recovery, e)

    e["fail_count"] += 1
    alert = False
    if e["fail_count"] >= threshold and threshold > 0:
        if (not e["alerted"]) or (re_alert_every > 0 and e["fail_count"] % re_alert_every == 0):
            alert = True
            e["alerted"] = True
    return (alert, False, e)


# --------------------------------------------------------------------------- #
# persisted per-condition state (best-effort, never fatal — mirrors cron_guard)
# --------------------------------------------------------------------------- #
def _default_state_path() -> Path:
    return Path(__file__).resolve().parent.parent / "state" / STATE_FILE


def _load_state(path: str | Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(path: str | Path, state: dict) -> None:
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(p) + ".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass


def _fmt_age(s: float | None) -> str:
    if s is None:
        return "?"
    if s < 3600:
        return f"{int(s / 60)}m"
    if s < 86400:
        return f"{int(s / 3600)}h"
    return f"{int(s / 86400)}d"


# --------------------------------------------------------------------------- #
# alert / recovery messages — each condition a DISTINCT headline (the AC2 pin)
# --------------------------------------------------------------------------- #
def _alert_msg(name: str, c: Conditions, host: str) -> str:
    if name == "down":
        return (
            "🚨 ELITE UNIT — VPS unreachable / cockpit down\n\n"
            f"server:       {host}\n"
            f"health probe: {c.health_code}\n\n"
            "The Mac's cross-host watcher could not reach the VPS cockpit over SSH (or it answered "
            "non-200). The VPS is the elected Telegram sender, so it CANNOT report this itself — "
            "this alert came from the Mac. Check general.service on the VPS and the network path."
        )
    if name == "brief_missing":
        return (
            "🚨 ELITE UNIT — scheduled daily brief is missing\n\n"
            f"server:        {host}\n"
            f"cockpit:       {c.health_code}\n"
            f"last brief age: {_fmt_age(c.brief_age_s)}\n\n"
            "The VPS cockpit is up, but no daily brief has arrived within the window — the daily "
            "stand-up cron may not have fired, or the ceremony crashed before recording. 'A message "
            "arrived' is not sufficient; a missing scheduled brief is its own signal (EU-433 AC2)."
        )
    # brief_bad
    return (
        "🚨 ELITE UNIT — daily brief body is a provider error\n\n"
        f"server:   {host}\n"
        f"cockpit:  {c.health_code}\n"
        f"auth_down flag: {'set' if c.brief_auth_down else 'not set'}\n"
        f"detail:   {c.brief_detail[:200]}\n\n"
        "The newest daily brief recorded on the VPS is a provider/auth error string, not a brief "
        "(the EU-430 condition). The VPS is up and sending, so from the phone 'a brief arrived' "
        "looked healthy — this cross-host content check is the signal that distinguishes it."
    )


def _recovery_msg(name: str, c: Conditions, host: str) -> str:
    if name == "down":
        return (f"✅ ELITE UNIT — VPS reachable again\n\nserver: {host}\nhealth: {c.health_code}\n"
                "The Mac's cross-host watcher can reach the VPS cockpit again; it resumes silent monitoring.")
    if name == "brief_missing":
        return (f"✅ ELITE UNIT — daily brief is arriving again\n\nserver: {host}\n"
                f"last brief age: {_fmt_age(c.brief_age_s)}\nA fresh daily brief was recorded on the VPS.")
    return (f"✅ ELITE UNIT — daily brief body is healthy again\n\nserver: {host}\n"
            "The newest daily brief recorded on the VPS is real content again, not a provider error.")


# --------------------------------------------------------------------------- #
# _remote_probe — one SSH round-trip that runs the dumb remote probe (side-effect)
# --------------------------------------------------------------------------- #
def _remote_probe(host: str, repo: str, timeout: float) -> tuple[str, bool]:
    """SSH into the VPS and run the remote probe; return ``(stdout, ssh_ok)``.

    The remote script is fed over SSH stdin (``ssh … bash -s``) so it runs VERBATIM on the VPS — no
    nested shell-quoting. It curls the VPS's OWN cockpit (``127.0.0.1:8787`` — the cockpit is never
    exposed to the internet; VPS_DEPLOYMENT.md), then prints the newest ``daily_brief`` audit row.
    BatchMode + ConnectTimeout bound a dead-host hang (mirrors ``sync.pull_server_audit``). Any
    failure (host down, network split, ssh error) returns ``("", False)`` — classified as ``down``."""
    remote = (
        "set +e\n"
        "h=$(curl -s -m %d -o /dev/null -w '%%{http_code}' http://127.0.0.1:8787/api/health 2>/dev/null)\n"
        '[ -n "$h" ] || h=000\n'
        'echo "HEALTH=$h"\n'
        'f="$HOME/%s/state/audit.jsonl"\n'
        '[ -f "$f" ] && grep -E \'"event"[[:space:]]*:[[:space:]]*"daily_brief"\' "$f" | tail -n 1\n'
    ) % (HEALTH_TIMEOUT_S, repo)
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT_S}",
             host, "bash", "-s"],
            input=remote, capture_output=True, text=True, timeout=timeout,
        )
        return (proc.stdout or ""), (proc.returncode == 0)
    except Exception:  # noqa: BLE001 - a probe failure is the 'down' condition, never a crash
        return "", False


# --------------------------------------------------------------------------- #
# run — the entry point: probe → classify → step → notify (the only side-effectful path)
# --------------------------------------------------------------------------- #
def run(*, state_path: str | Path | None = None, probe=None, notifier=None,
        ssh_host: str | None = None, repo: str | None = None, now=None,
        brief_window_s: float | None = None, threshold: int = FAIL_THRESHOLD,
        re_alert_every: int = RE_ALERT_EVERY, ssh_timeout: float = SSH_TIMEOUT_S) -> int:
    """One watcher tick. Probes the VPS, classifies, advances per-condition state, and notifies.

    All collaborators are injectable so the decision path is unit-tested end-to-end with no network:
    ``probe`` (default ``_remote_probe``), ``notifier`` (default ``notify.send`` — the Mac's OWN
    Telegram, independent of the VPS process per AC3), ``ssh_host`` (default ``$GENERAL_SERVER_SSH``).
    A no-op (returns 0, no alert) when no VPS target is configured — the watcher only runs where it
    is meaningful (the Mac), not on the VPS itself or a dev box without VPS access. Never raises."""
    if notifier is None:
        from . import notify
        notifier = notify.send
    if now is None:
        now = time.time
    if state_path is None:
        state_path = _default_state_path()
    host = (ssh_host if ssh_host is not None else os.environ.get("GENERAL_SERVER_SSH", "")).strip()
    if not host:
        return 0  # not configured on this host — nothing to watch
    repo = _safe_repo(repo or os.environ.get("GENERAL_SERVER_REPO", "General"))
    window = brief_window_s if brief_window_s is not None else BRIEF_WINDOW_S
    if probe is None:
        probe = _remote_probe

    # 1. probe (a probe blow-up is the 'down' condition — never propagates)
    try:
        raw, ssh_ok = probe(host, repo, ssh_timeout)
    except Exception:  # noqa: BLE001
        raw, ssh_ok = "", False
    status = parse_remote(raw or "", ssh_ok)
    cond = classify(status, now=now(), brief_window_s=window)

    # 2. advance each condition's state and notify on the alert/recovery edges
    state = _load_state(state_path)
    failing = {"down": cond.down, "brief_missing": cond.brief_missing, "brief_bad": cond.brief_bad}
    for name in CONDITIONS:
        # while DOWN the brief conditions are unobservable: freeze their state, never pile on
        if cond.down and name != "down":
            continue
        alert, recovery, entry = step_condition(state.get(name), failing=failing[name],
                                                threshold=threshold, re_alert_every=re_alert_every)
        state[name] = entry
        try:
            if alert:
                notifier(_alert_msg(name, cond, host))
            elif recovery:
                notifier(_recovery_msg(name, cond, host))
        except Exception:  # noqa: BLE001 - notifying must never break the watcher
            pass
    _save_state(state_path, state)
    return 0
