"""Auth liveness — an EXPIRED Claude login must be detected, hold the drain, and charge no strikes.

2026-07-15 ~22:05 incident: the drain burned error strikes on 5+ tickets because every builder call
failed with "Not logged in · Please run /login" — the Claude Code OAuth token had expired — while
health.summary() reported healthy:True. config.detected_auth()/_claude_login_present() are
presence-only (a credential SOURCE exists), never validity. The fix adds a liveness dimension
(orchestrator/auth_probe.py) surfaced in health/doctor, and routes expired-login builder failures
to an EU-228-style no-strike hold in autopilot (mirroring 05a7e98's base-gate hold pattern).

Pins (each is a testable acceptance criterion from the ticket):
  1. auth_probe.is_login_failure() matches the incident's markers and nothing else (a DNS blip,
     a turn-limit, a plain ticket failure must never read as "re-login needed").
  2. auth_probe.probe() classifies one CLI round-trip into valid / expired / unreachable /
     unknown — distinguishing "invalid credential" from "network down" (infra_classify-style
     markers) — caches the verdict (~15 min) so health stays fast, degrades to presence-only when
     the probe can't run, and honours the GENERAL_AUTH_PROBE=0 kill-switch (the suite's contract).
  3. health.checks() surfaces "Claude auth" as its own check: EXPIRED -> bad, unreachable -> warn
     (never bad), valid -> ok, probe-unavailable -> ok/presence-only with the "Claude login"
     presence check unchanged; no credential at all -> no liveness row (presence is already bad).
  4. autopilot: an expired-login ERRORED report charges NO strike (_tally_errored), arms the
     dedicated auth-hold with exactly ONE "re-login needed" alert (_enter_auth_hold), and the hold
     clears with one paired resume alert only on a VERIFIED valid probe (_auth_hold_recheck) —
     unreachable/unknown keep holding. Loop wiring keeps auth ids OUT of the EU-228 offline-hold
     (whose connectivity probe would announce a misleading "restored").

No network, no real models — stub the Agent SDK + requests, monkeypatch the probe/notify seams.
"""
import os, sys, types, tempfile
from pathlib import Path

# run_all.py exports GENERAL_AUTH_PROBE=0 to every harness (the no-network contract); THIS harness
# tests the probe itself through the stubbed _run_probe seam, so re-enable it in-process.
os.environ.pop("GENERAL_AUTH_PROBE", None)

# Stub the Agent SDK + requests so importing the orchestrator never reaches the network.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda *a, **k: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import auth_probe, autopilot, health
from orchestrator import config as config_mod
from orchestrator.audit import AuditLog
from orchestrator.config import AppConfig, Config
from orchestrator.contracts import Outcome, TicketReport

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))
    print(("PASS: " if c else "FAIL: ") + n + (f" - {d}" if d and not c else ""))


tmp = Path(tempfile.mkdtemp())
audit = AuditLog(str(tmp / "audit.jsonl"))

# captured Telegram sends — monkeypatch the notify seam autopilot calls
sent: list[str] = []
autopilot.notify.send = lambda text, chat_id=None: (sent.append(text), True)[1]

def report(tid, outcome, notes=""):
    return TicketReport(ticket_id=tid, outcome=outcome, iterations=1, cost_usd=0.0, notes=notes)

NOT_LOGGED_IN = "Not logged in · Please run /login"
DNS_MSG = ("HTTPSConnectionPool(host='toibis.atlassian.net', port=443): Max retries exceeded "
           "(Caused by NameResolutionError)")


# ================================================================================================ #
# AC1: is_login_failure — the incident's markers, and nothing else
# ================================================================================================ #
print("\n=== AC1: is_login_failure() markers ===")
for msg, label in [
    (NOT_LOGGED_IN, "the 2026-07-15 signature"),
    ("Invalid API key · Please run /login", "invalid API key"),
    ("API Error: 401 {\"type\":\"error\",\"error\":{\"type\":\"authentication_error\"}}",
     "authentication_error API shape"),
]:
    chk(f"is_login_failure matches {label}", auth_probe.is_login_failure(msg), msg)
for msg, label in [
    (DNS_MSG, "a DNS blip (infra, not auth)"),
    ("max_turns_reached maxTurns:96 turnCount:97", "a turn-limit"),
    ("AssertionError: expected 3 got 4", "a plain ticket failure"),
    ("JIRA_API_TOKEN rejected: 401 from atlassian", "a Jira-credential failure (wrong system)"),
    ("", "empty notes"),
    (None, "None notes"),
]:
    chk(f"is_login_failure refuses {label}", not auth_probe.is_login_failure(msg), repr(msg))


# ================================================================================================ #
# AC2: probe() classification, cache, presence-only degrade, kill-switch
# ================================================================================================ #
print("\n=== AC2: probe() — classify, cache, degrade, kill-switch ===")
_orig_run_probe = auth_probe._run_probe
_orig_claude_bin = auth_probe._claude_bin

def _set_probe(rc, out):
    auth_probe._run_probe = lambda timeout=0: (rc, out)
    auth_probe.invalidate()

_set_probe(0, "ok")
chk("rc=0 -> valid", auth_probe.probe()["state"] == "valid", str(auth_probe.probe()))

_set_probe(1, NOT_LOGGED_IN)
pr = auth_probe.probe()
chk("login-failure output -> expired (invalid credential)", pr["state"] == "expired", str(pr))
chk("expired detail carries the CLI evidence", "not logged in" in pr["detail"].lower(), pr["detail"])

_set_probe(None, auth_probe._TIMEOUT_SENTINEL)
chk("probe timeout -> unreachable (network, NOT a dead credential)",
    auth_probe.probe()["state"] == "unreachable", str(auth_probe.probe()))

_set_probe(1, DNS_MSG)
chk("infra_classify-style network output -> unreachable",
    auth_probe.probe()["state"] == "unreachable", str(auth_probe.probe()))

_set_probe(1, "some totally unrecognizable failure")
chk("unrecognizable failure -> unknown (presence-only)",
    auth_probe.probe()["state"] == "unknown", str(auth_probe.probe()))

# no `claude` binary at all -> unknown, through the REAL _run_probe
auth_probe._run_probe = _orig_run_probe
auth_probe._claude_bin = lambda: None
auth_probe.invalidate()
pr = auth_probe.probe()
chk("no `claude` binary -> unknown (presence-only detection still works)",
    pr["state"] == "unknown" and "presence-only" in pr["detail"], str(pr))
auth_probe._claude_bin = _orig_claude_bin

# cache: a second unforced probe() inside the TTL must NOT re-run the subprocess seam
calls = {"n": 0}
def _counting(timeout=0):
    calls["n"] += 1
    return (0, "ok")
auth_probe._run_probe = _counting
auth_probe.invalidate()
auth_probe.probe(); auth_probe.probe(); auth_probe.probe()
chk("verdict is cached — 3 probe() calls inside the TTL run the subprocess once",
    calls["n"] == 1, str(calls))
auth_probe.probe(force=True)
chk("force=True re-probes through the cache", calls["n"] == 2, str(calls))
auth_probe.probe(max_age_s=0.0)
chk("max_age_s=0 re-probes (the auth-hold recheck cadence knob)", calls["n"] == 3, str(calls))

# invalidate() drops the cached verdict — the seam _enter_auth_hold relies on
auth_probe.invalidate()
chk("invalidate() drops the cached verdict", auth_probe._cache["result"] is None)

# kill-switch: GENERAL_AUTH_PROBE=0 (what run_all exports) never touches the subprocess seam
calls["n"] = 0
os.environ["GENERAL_AUTH_PROBE"] = "0"
pr = auth_probe.probe(force=True)
chk("GENERAL_AUTH_PROBE=0 -> unknown without running the probe",
    pr["state"] == "unknown" and calls["n"] == 0, f"{pr} calls={calls}")
os.environ.pop("GENERAL_AUTH_PROBE", None)
auth_probe._run_probe = _orig_run_probe
auth_probe.invalidate()


# ================================================================================================ #
# AC3: health.checks() — "Claude auth" is its own check with distinct states
# ================================================================================================ #
print("\n=== AC3: health.checks() surfaces the liveness state ===")
ROOT = tempfile.mkdtemp()
(Path(ROOT) / ".git").mkdir()
app = AppConfig(name="Elite-Unit", repo_path=ROOT, base_branch="dev", workdir=ROOT,
                gate_commands=[f"{sys.executable} tests/run_all.py"], backlog_backend="none")
cfg = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)

def _row(out, name):
    return next((r for r in out if r["name"] == name), None)

# a credential must LOOK present so the liveness check runs (presence path stays env-driven)
os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"

_set_probe(1, NOT_LOGGED_IN)
out = health.checks(cfg)
row = _row(out, "Claude auth")
chk("expired -> 'Claude auth' check exists and is BAD", row is not None and row["status"] == "bad",
    str(row))
chk("expired detail says EXPIRED + how to fix (/login)",
    row and "EXPIRED" in row["detail"] and "/login" in row["detail"], row and row["detail"])
login_row = _row(out, "Claude login")
chk("the presence check ('Claude login') is unchanged and still ok",
    login_row is not None and login_row["status"] == "ok", str(login_row))
_summary = health.summary(cfg)
chk("summary() carries the bad 'Claude auth' row and counts it as a problem "
    "(the incident's healthy:True lie is now impossible)",
    any(c["name"] == "Claude auth" and c["status"] == "bad" for c in _summary["checks"])
    and _summary["healthy"] is False and _summary["problems"] >= 1,
    str([c for c in _summary["checks"] if c["status"] == "bad"]))

_set_probe(None, auth_probe._TIMEOUT_SENTINEL)
row = _row(health.checks(cfg), "Claude auth")
chk("unreachable -> warn, never bad (an outage must not read as a dead credential)",
    row is not None and row["status"] == "warn", str(row))

_set_probe(0, "ok")
row = _row(health.checks(cfg), "Claude auth")
chk("valid -> ok", row is not None and row["status"] == "ok", str(row))

# probe can't run -> presence-only, not a new failure state
auth_probe._run_probe = _orig_run_probe
auth_probe._claude_bin = lambda: None
auth_probe.invalidate()
row = _row(health.checks(cfg), "Claude auth")
chk("probe unavailable -> ok + presence-only (degrades to the old behaviour, no false red)",
    row is not None and row["status"] == "ok" and "presence-only" in row["detail"], str(row))
auth_probe._claude_bin = _orig_claude_bin

# no credential at all -> no liveness row (presence check is already the bad row)
os.environ.pop("ANTHROPIC_API_KEY", None)
_saved_oauth = os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
_orig_present = config_mod._claude_login_present
config_mod._claude_login_present = lambda: False
try:
    out = health.checks(cfg)
finally:
    config_mod._claude_login_present = _orig_present
    if _saved_oauth is not None:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = _saved_oauth
chk("no credential -> no 'Claude auth' row; 'Claude login' presence row is the bad one",
    _row(out, "Claude auth") is None and _row(out, "Claude login")["status"] == "bad",
    str([r for r in out if r["name"].startswith("Claude")]))

# the doctor must force a FRESH probe (a stale cached verdict right after /login would mislead)
import inspect
from orchestrator import main as main_mod
_doctor_src = inspect.getsource(main_mod._doctor)
chk("doctor forces a fresh probe (auth_probe.probe(force=True) in main._doctor)",
    "auth_probe.probe(force=True)" in _doctor_src)


# ================================================================================================ #
# AC4: autopilot — no strikes, ONE alert, hold clears only on a VERIFIED valid probe
# ================================================================================================ #
print("\n=== AC4: autopilot — expired login charges no strike, holds with one alert ===")
counts = {}
reports = [
    report("EU-401", Outcome.ERRORED, NOT_LOGGED_IN),
    report("EU-402", Outcome.ERRORED, "API Error: authentication_error — please run /login"),
    report("AUTO-9", Outcome.ERRORED, "TypeError: cannot read property 'x' of undefined"),
]
park_now, errored, retrying, infra_ids, changed = autopilot._tally_errored(reports, counts)
chk("expired-login tickets charged NO strike (error_counts untouched)",
    "EU-401" not in counts and "EU-402" not in counts, str(counts))
chk("expired-login tickets ride the no-strike (infra) set", {"EU-401", "EU-402"} <= infra_ids,
    str(infra_ids))
chk("expired-login tickets never parked or retried",
    not ({"EU-401", "EU-402"} & set(park_now)) and not ({"EU-401", "EU-402"} & set(retrying)),
    f"park={park_now} retrying={retrying}")
chk("a genuinely-broken ticket in the same cycle still strikes (no blanket amnesty)",
    counts.get("AUTO-9") == 1, str(counts))

# fail-first control: with the marker classifier stubbed out, the SAME report WOULD strike —
# proves _tally_errored actually delegates to auth_probe.is_login_failure.
counts2 = {}
_orig_ilf = auth_probe.is_login_failure
auth_probe.is_login_failure = lambda text: False
try:
    autopilot._tally_errored([report("EU-403", Outcome.ERRORED, NOT_LOGGED_IN)], counts2)
finally:
    auth_probe.is_login_failure = _orig_ilf
chk("control: without is_login_failure, the same 'Not logged in' report WOULD strike",
    counts2.get("EU-403") == 1, str(counts2))

# enter: exactly ONE alert for a multi-ticket wave, cache invalidated so the recheck re-verifies
autopilot._last_auth_alert = 0.0
autopilot._auth_resume_alert_due = False
auth_probe._run_probe = lambda timeout=0: (0, "ok")
auth_probe._cache["at"] = __import__("time").time()
auth_probe._cache["result"] = {"state": "valid", "detail": "stale", "checked_at": 0.0}
sent.clear()
active = autopilot._enter_auth_hold(None, audit, {"EU-401", "EU-402"}, False)
chk("_enter_auth_hold activates the hold", active is True)
chk("exactly ONE 're-login needed' alert for 2 simultaneously-failed tickets",
    len(sent) == 1, str(sent))
chk("the alert says how to fix it (/login) and that no strikes were charged",
    sent and "/login" in sent[0] and "No error strikes" in sent[0], sent[0] if sent else "")
chk("entering the hold invalidates the (stale-valid) probe cache",
    auth_probe._cache["result"] is None, str(auth_probe._cache))

# a SECOND cycle's auth failures while already active must NOT re-alert
sent.clear()
chk("already-active hold sends NO extra alert",
    autopilot._enter_auth_hold(None, audit, {"EU-404"}, True) is True and sent == [], str(sent))

# churn damper: hold cleared then immediately re-entered inside the cooldown -> no repeat ping
sent.clear()
chk("re-entering within the alert cooldown sends NO repeat ping (churn damper)",
    autopilot._enter_auth_hold(None, audit, {"EU-401"}, False) is True and sent == [], str(sent))

# recheck: still expired -> stays held, no alert
_orig_probe_fn = auth_probe.probe
auth_probe.probe = lambda **kw: {"state": "expired", "detail": "x", "checked_at": 0.0}
sent.clear()
chk("recheck stays held while the probe still says expired",
    autopilot._auth_hold_recheck(None, audit, True) is True and sent == [], str(sent))

# recheck: unreachable / unknown -> keep holding (no evidence the login is back)
auth_probe.probe = lambda **kw: {"state": "unreachable", "detail": "x", "checked_at": 0.0}
chk("recheck keeps holding on unreachable (network down is not a verified re-login)",
    autopilot._auth_hold_recheck(None, audit, True) is True)
auth_probe.probe = lambda **kw: {"state": "unknown", "detail": "x", "checked_at": 0.0}
chk("recheck keeps holding on unknown (probe can't run -> builders can't either)",
    autopilot._auth_hold_recheck(None, audit, True) is True)

# recheck: VERIFIED valid -> clears with exactly one paired resume alert
auth_probe.probe = lambda **kw: {"state": "valid", "detail": "ok", "checked_at": 0.0}
sent.clear()
chk("recheck clears the hold on a VERIFIED valid probe",
    autopilot._auth_hold_recheck(None, audit, True) is False)
chk("clearing sends exactly one resume alert (paired to the enter alert)",
    len(sent) == 1 and "resuming" in sent[0], str(sent))
sent.clear()
chk("a churned (unalerted) hold clears with NO orphan resume ping",
    autopilot._auth_hold_recheck(None, audit, True) is False and sent == [], str(sent))
chk("recheck(active=False) is a pure no-op",
    autopilot._auth_hold_recheck(None, audit, False) is False and sent == [])
auth_probe.probe = _orig_probe_fn
auth_probe._run_probe = _orig_run_probe
auth_probe.invalidate()

# loop wiring drift guards: auth ids are split out of the offline-hold (whose probe would
# announce a misleading "connectivity restored"), the auth-hold is armed from the same cycle,
# and the top-of-loop recheck actually gates picking. Mirrors base_gate_infra_test's guards.
_src = inspect.getsource(autopilot)
chk("wiring: offline-hold excludes auth ids (infra_errored - _base_ids - _auth_ids)",
    "infra_errored - _base_ids - _auth_ids" in _src)
chk("wiring: the cycle arms the auth-hold (_enter_auth_hold(cfg, audit, _auth_ids, auth_hold))",
    "_enter_auth_hold(cfg, audit, _auth_ids, auth_hold)" in _src)
chk("wiring: the loop rechecks the auth-hold before picking (_auth_hold_recheck(cfg, audit, auth_hold))",
    "_auth_hold_recheck(cfg, audit, auth_hold)" in _src)


print("\n================ AUTH LIVENESS QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
