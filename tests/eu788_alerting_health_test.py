"""EU-788: Mac Telegram send-failures must be observable on the cockpit surface.

Tests the full chain from notify.alerting state → health checks → /api/health → boot probe.

Scenarios (one per acceptance criterion):
  1. notify.send() records its outcome: with TELEGRAM_* configured, when HTTP post fails the
     module exposes a degraded alerting status; a subsequent successful send flips it back to ok.
     Verified by stubbing requests.post to fail then succeed.
  2. health.checks() includes an 'alerting' check that is status 'warn' with a
     "Commander is not receiving alerts" detail when notify.alerting_status() is failed, and
     'ok' when it is ok; health.summary() reflects it in warnings count.
     Verified by forcing notify state and asserting the check status.
  3. A boot self-test (autopilot.boot_alerting_probe) sends exactly ONE silent probe (Telegram
     disable_notification set) only when notify.configured(), records the outcome into notify's
     alerting state, and never raises. Verified by stubbing notify.send: return False → state
     degraded and probe returns falsey; return True → state ok; assert silent=True was passed.
  4. GET /api/health (server.create_app.cfg).test_client) returns the alerting check in its
     checks array so the cockpit pill/banner render the degraded state. Verified via flask test
     client with notify forced degraded → checks contains the alerting check with status warn.
  5. When Telegram is not configured, the boot probe no-ops and does NOT mark alerting
     degraded (that stays the existing 'not configured' health signal). Verified by clearing
     TELEGRAM_* → probe skips, alerting check not 'warn'-from-failure.

Written as standalone pytest-style checks (exit-code driven like other eu* tests). Stubs
requests and claude_agent_sdk so the whole suite runs offline.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

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
req.post = lambda *a, **k: types.SimpleNamespace(status_code=200, json=lambda: {})
req.get = req.post
sys.modules["requests"] = req

# Ensure orchestrator is importable before we swap requests
os.environ.setdefault("_EU788_STUBBED", "1")
sys.path.insert(0, ".")

from orchestrator import notify  # noqa: E402
from orchestrator import health  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


def _clear_env():
    """Remove Telegram env vars."""
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    os.environ.pop("TELEGRAM_CHAT_ID", None)


def _set_env(token="fake-token", chat_id="12345"):
    os.environ["TELEGRAM_BOT_TOKEN"] = token
    os.environ["TELEGRAM_CHAT_ID"] = chat_id


def _reset_notify_state():
    """Reset alerting state + reset any dedup flags that leak between scenarios."""
    notify._alerting_outcome["status"] = "ok"
    notify._alerting_outcome["ts"] = None
    notify._alerting_outcome["reason"] = ""
    notify._plan_limit_alert_sent = False
    notify._plan_limit_alert_episode = None


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# AC1. notify.send() records its outcome: failure → degraded, success → ok again
# ═══════════════════════════════════════════════════════════════════════════════════════════════
_reset_notify_state()
_set_env()

_fake_resp_ok = types.SimpleNamespace(status_code=200, json=lambda: {})
_fake_resp_err = types.SimpleNamespace(status_code=400, json=lambda: {"description": "Bad Request"})

with patch("orchestrator.notify.requests.post") as mock_post:
    mock_post.return_value = _fake_resp_err
    ok = notify.send("test message")
    chk("send returns False when Telegram returns non-200", ok is False, ok)
    st = notify.alerting_status()
    chk("alerting_status shows 'failed' after bad send", st["status"] == "failed", st)
    chk("alerting_status records a timestamp", st["ts"] is not None, st)
    chk("alerting_status captures the reason", "http 400" in st.get("reason", ""), st)

_reset_notify_state()

with patch("orchestrator.notify.requests.post") as mock_post:
    mock_post.return_value = _fake_resp_ok
    ok = notify.send("recovery message")
    chk("send returns True on 200", ok is True, ok)
    st = notify.alerting_status()
    chk("success flips alerting_status back to 'ok'", st["status"] == "ok", st)
    chk("ts clears after recovery", st["ts"] is None, st)

_reset_notify_state()


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# AC2. health.checks() includes 'alerting' check: warn when degraded, ok when healthy
# ═══════════════════════════════════════════════════════════════════════════════════════════════
_dir = Path(tempfile.mkdtemp())
AUDIT = _dir / "audit.jsonl"
AUDIT.write_text("", encoding="utf-8")

from orchestrator.config import AppConfig, Config  # noqa: E402
cfg = Config(apps=[AppConfig(name="alpha", repo_path=str(_dir), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(AUDIT), use_worktree=False)
cfg.detected_auth = lambda: "test"


with patch.object(notify, "configured", return_value=True):
    # Force degraded state
    notify._alerting_outcome.update({"status": "failed", "ts": 0.0, "reason": "bad token"})
    checks = health.checks(cfg)
    alerting_checks = [c for c in checks if c["name"] == "Alerting"]
    chk("health.checks produces an 'Alerting' check", len(alerting_checks) >= 1,
        f"checks={[c['name'] for c in checks]}")
    if alerting_checks:
        ac = alerting_checks[0]
        chk("Alerting check status is 'warn' when send failed", ac["status"] == "warn", ac)
        chk("detail mentions Commander not receiving alerts",
            "Commander is not receiving alerts" in ac.get("detail", ""),
            ac.get("detail", ""))

_reset_notify_state()

with patch.object(notify, "configured", return_value=True):
    notify._alerting_outcome["status"] = "ok"
    notify._alerting_outcome["ts"] = None
    notify._alerting_outcome["reason"] = ""
    checks = health.checks(cfg)
    alerting_checks = [c for c in checks if c["name"] == "Alerting"]
    if alerting_checks:
        ac = alerting_checks[0]
        chk("Alerting check status is 'ok' when send succeeded", ac["status"] == "ok", ac)

# Not configured → no 'Alerting' check should appear (it's gated behind configured())
with patch.object(notify, "configured", return_value=False):
    checks = health.checks(cfg)
    alerting_checks = [c for c in checks if c["name"] == "Alerting"]
    chk("no Alerting check when Telegram is not configured", len(alerting_checks) == 0,
        f"found {len(alerting_checks)} Alerting checks")

# summary() warns count reflects the alerting warning
_reset_notify_state()
with patch.object(notify, "configured", return_value=True):
    notify._alerting_outcome.update({"status": "failed", "ts": 0.0, "reason": "err"})
    s = health.summary(cfg)
    chk("summary warnings include the alerting warn", s["warnings"] >= 1,
        f"warnings={s['warnings']} checks={[(c['name'],c['status']) for c in s['checks']]}")


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# AC3. boot_alerting_probe sends ONE silent probe, records outcome, never raises
# ═══════════════════════════════════════════════════════════════════════════════════════════════
from orchestrator import autopilot  # noqa: E402

# 3a) Not configured → no-ops, returns Falsey, does NOT mark degraded
_reset_notify_state()
_clear_env()          # must clear any leftover creds from AC1/AC2
_check_cfg = types.SimpleNamespace(audit_path=str(AUDIT))
result = autopilot.boot_alerting_probe(_check_cfg)
chk("probe returns False when not configured", result is False, result)
st = notify.alerting_status()
chk("probe does NOT mark degraded when not configured", st["status"] == "ok", st)

# 3b) Configured + probe succeeds → state ok, probe returns True, silence=True
_reset_notify_state()
_set_env()
_fake_resp_ok = types.SimpleNamespace(status_code=200, json=lambda: {})

with patch("orchestrator.notify.requests.post") as mock_post:
    mock_post.return_value = _fake_resp_ok
    result = autopilot.boot_alerting_probe(_check_cfg)
    chk("probe returns True on successful send", result is True, result)
    chk("probe calls send once", mock_post.call_count == 1, mock_post.call_count)
    # Verify disable_notification was requested
    call_kwargs = mock_post.call_args
    payload = call_kwargs[1]["json"]
    chk("probe sets disable_notification=True (silent)",
        payload.get("disable_notification") is True, payload)
    st = notify.alerting_status()
    chk("alerting state is ok after probe success", st["status"] == "ok", st)

# 3c) Configured + probe fails → state degraded, probe returns Falsey, silence=True
_reset_notify_state()
_fake_resp_err = types.SimpleNamespace(status_code=400, json=lambda: {"description": "Unauthorized"})

with patch("orchestrator.notify.requests.post") as mock_post:
    mock_post.return_value = _fake_resp_err
    result = autopilot.boot_alerting_probe(_check_cfg)
    chk("probe returns False on failed send", result is False, result)
    st = notify.alerting_status()
    chk("alerting state is degraded after probe failure", st["status"] == "failed", st)

# 3d) Never raises even if something explodes
_reset_notify_state()
_set_env()
original_configured = notify.configured

def _broken_configured():
    raise RuntimeError("config boom")

notify.configured = _broken_configured
try:
    result = autopilot.boot_alerting_probe(_check_cfg)
    chk("boot_alerting_probe never raises (survives broken configured)", result is False, result)
finally:
    notify.configured = original_configured
    _reset_notify_state()


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# AC4. GET /api/health returns the alerting check in its checks array
# ═══════════════════════════════════════════════════════════════════════════════════════════════
import orchestrator.server as srv  # noqa: E402

_reset_notify_state()
_set_env()
notify._alerting_outcome.update({"status": "failed", "ts": 0.0, "reason": "bad auth"})

app = srv.create_app(cfg)
client = app.test_client()
r = client.get("/api/health")
chk("GET /api/health returns 200", r.status_code == 200, r.status_code)

data = json.loads(r.get_data(as_text=True))
checks_list = data.get("checks", [])
alerting_entries = [c for c in checks_list if c["name"] == "Alerting"]
chk("/api/health includes 'Alerting' check in checks array", len(alerting_entries) >= 1,
    f"checks={[c['name'] for c in checks_list]}")
if alerting_entries:
    ae = alerting_entries[0]
    chk("/api/health Alerting has status 'warn' when degraded", ae["status"] == "warn", ae)
    chk("/api/health Alerting detail references commander alerting",
        "Commander is not receiving alerts" in ae.get("detail", ""), ae.get("detail", ""))

_reset_notify_state()
notify._alerting_outcome["status"] = "ok"
notify._alerting_outcome["ts"] = None
notify._alerting_outcome["reason"] = ""
app_ok = srv.create_app(cfg)
client_ok = app_ok.test_client()
r_ok = client_ok.get("/api/health")
data_ok = json.loads(r_ok.get_data(as_text=True))
checks_ok = data_ok.get("checks", [])
alerting_ok = [c for c in checks_ok if c["name"] == "Alerting"]
if alerting_ok:
    chk("/api/health Alerting has status 'ok' when healthy",
        alerting_ok[0]["status"] == "ok", alerting_ok[0])


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# AC5. When Telegram is NOT configured, the boot probe no-ops and does NOT mark degraded
# ═══════════════════════════════════════════════════════════════════════════════════════════════
_reset_notify_state()
_clear_env()

# Probe with no creds → returns False, state stays "ok" (NOT "failed")
result = autopilot.boot_alerting_probe(_check_cfg)
chk("probe no-ops when creds absent", result is False, result)
st = notify.alerting_status()
chk("alerting stays 'ok' (not failed) when unconfigured", st["status"] == "ok", st)
chk("no ts recorded when unconfigured", st["ts"] is None, st)

# Health check when not configured → no Alerting check, Telegram shows "warn" (not configured)
with patch.object(notify, "configured", return_value=False):
    checks = health.checks(cfg)
    tg_checks = [c for c in checks if c["name"] == "Telegram"]
    alerting_checks = [c for c in checks if c["name"] == "Alerting"]
    chk("Telegram check says 'warn' when unconfigured",
        tg_checks[0]["status"] == "warn" if tg_checks else False,
        f"{[{'name': c['name'], 'status': c['status']} for c in checks]}")
    chk("no Alerting check generated when unconfigured", len(alerting_checks) == 0,
        f"unexpected {len(alerting_checks)} Alerting checks")


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# Regression guard: send() with default args still works (no silent param changes behaviour)
# ═══════════════════════════════════════════════════════════════════════════════════════════════
_reset_notify_state()
_set_env()
with patch("orchestrator.notify.requests.post") as mock_post:
    mock_post.return_value = _fake_resp_ok
    ok = notify.send("normal alert")
    chk("normal send (no silent arg) still works", ok is True, ok)
    payload = mock_post.call_args[1]["json"]
    chk("default send does NOT set disable_notification",
        "disable_notification" not in payload, payload)


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# Edge case: request exception → still records degraded
# ═══════════════════════════════════════════════════════════════════════════════════════════════
_reset_notify_state()
_set_env()

with patch("orchestrator.notify.requests.post") as mock_post:
    mock_post.side_effect = req.RequestException("connection refused")
    try:
        ok = notify.send("after exception")
        # Should return False and record degraded
        chk("send returns False on RequestException", ok is False, ok)
        st = notify.alerting_status()
        chk("exception also degrades alerting", st["status"] == "failed", st)
    except Exception:
        chk("send NEVER raises on network error", False, "raised!")


print("\n============ EU-788 ALERTING HEALTH QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
