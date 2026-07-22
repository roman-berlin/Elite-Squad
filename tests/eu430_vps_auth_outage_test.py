"""EU-430 — VPS model auth is dead and the raw 401 is broadcast as the daily brief.

2026-07-22 VPS responsibility audit found: ~/.claude/.credentials.json expired (expiresAt
2026-07-19, refreshToken EMPTY), no fallback, so every model call since 07-20 08:30 UTC returned
401. council.py accepted the SDK result as-is and broadcast it to the Commander's phone verbatim
("Failed to authenticate. API Error: 401 Invalid authentication credentials"); nothing classified
it (is_login_failure never saw these strings); ROSTER.md + state/last-standup.md were poisoned with
the 401 string.

Pins (one test per acceptance criterion):
  AC2/AC3 — a ceremony whose model call fails auth does NOT broadcast the provider's raw error as
            the brief; it sends a short operator-shaped alert and skips the poisoned artefacts.
            is_login_failure is widened to the two observed strings and wired into the path so an
            auth failure degrades health (probe cache invalidated) instead of rendering as content.
  AC4    — a credential-expiry pre-flight warns when expiresAt is within 48h OR refreshToken empty.
  AC5    — a provider-error body is never persisted as an artefact (ROSTER.md status line, the
            stand-up file, the daily transcript).

No network, no real models — stub the Agent SDK + requests, monkeypatch run_agent / the creds seam.
"""
import asyncio, os, sys, tempfile, types
from pathlib import Path

# Stub the Agent SDK + requests so importing the orchestrator never reaches the network.
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
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import auth_probe, council, roster
from orchestrator.config import AppConfig, Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))
    print(("PASS: " if c else "FAIL: ") + n + (f" - {d}" if d and not c else ""))


# The two error strings the dead VPS credential actually surfaced (the ticket's evidence).
AUTH_401 = "Failed to authenticate. API Error: 401 Invalid authentication credentials"
OAUTH_EXPIRED = "Error: OAuth access token has expired"
REAL_BRIEF = "**Today:** land the EU-430 auth-outage guard. **FOR YOU** — None."
DNS_MSG = ("HTTPSConnectionPool(host='api.anthropic.net', port=443): Max retries exceeded "
           "(Caused by NameResolutionError)")


# ================================================================================================ #
# AC3: the markers now cover BOTH observed 401 strings (they did not before — the blind spot)
# ================================================================================================ #
print("\n=== AC3: is_login_failure covers the two observed 401 strings ===")
chk("is_login_failure matches the 401 'Invalid authentication credentials' string",
    auth_probe.is_login_failure(AUTH_401), AUTH_401)
chk("is_login_failure matches 'OAuth access token has expired'",
    auth_probe.is_login_failure(OAUTH_EXPIRED), OAUTH_EXPIRED)
# the original markers still hold (no regression in the widened set)
chk("is_login_failure still matches the 2026-07-15 'Not logged in' signature",
    auth_probe.is_login_failure("Not logged in · Please run /login"))
# and a non-auth failure must NEVER read as a dead credential
for msg, label in [(DNS_MSG, "a DNS blip (infra, not auth)"),
                   ("max_turns_reached maxTurns:96 turnCount:97", "a turn-limit"),
                   ("AssertionError: expected 3 got 4", "a plain ticket failure"),
                   (REAL_BRIEF, "a real briefing"), ("", "empty"), (None, "None")]:
    chk(f"is_login_failure refuses {label}", not auth_probe.is_login_failure(msg), repr(msg))


# ================================================================================================ #
# AC2/AC5: looks_like_provider_error — the artefact/broadcast guard (broader than the login markers)
# ================================================================================================ #
print("\n=== AC2/AC5: looks_like_provider_error (the never-persist-as-content guard) ===")
for msg, label in [(AUTH_401, "the 401 auth string"),
                   (OAUTH_EXPIRED, "OAuth expired"),
                   ("API Error: 500 internal server error", "a generic API error"),
                   ("Failed to authenticate.", "a bare auth failure")]:
    chk(f"looks_like_provider_error flags {label}", auth_probe.looks_like_provider_error(msg), msg)
for msg, label in [(REAL_BRIEF, "a real briefing"),
                   ("Yesterday: shipped EU-429. Today: review queue.", "a stand-up line"),
                   ("", "empty"), (None, "None")]:
    chk(f"looks_like_provider_error refuses {label}",
        not auth_probe.looks_like_provider_error(msg), repr(msg))


# ================================================================================================ #
# AC4: credential_status — the expiry pre-flight (warn at 48h OR empty refresh; never a false red)
# ================================================================================================ #
print("\n=== AC4: credential_status — expiry pre-flight ===")
NOW = 1_700_000_000.0  # fixed "now" so the threshold math is deterministic

def _block(expires_at, refresh="r"):
    # _read_oauth_block's contract is to return the UNWRAPPED inner OAuth block (it strips the
    # "claudeAiOauth" envelope), so the stub returns the bare fields a real read would yield.
    return {"expiresAt": expires_at, "refreshToken": refresh, "subscriptionType": "max"}

# healthy: expires 10 days out, refresh present -> ok
_orig_read_block = auth_probe._read_oauth_block
auth_probe._read_oauth_block = lambda: _block(NOW + 10 * 86400)
st = auth_probe.credential_status(now_epoch=NOW)
chk("valid credential (10 days out) -> ok", st["level"] == "ok", str(st))

# the ticket's exact condition: EMPTY refresh token -> warn (cannot self-heal)
auth_probe._read_oauth_block = lambda: _block(NOW + 10 * 86400, refresh="")
st = auth_probe.credential_status(now_epoch=NOW)
chk("empty refreshToken -> warn (the VPS's dead condition)", st["level"] == "warn", str(st))
chk("warn reason names the empty refresh token", "refresh" in st["reason"].lower(), st["reason"])

# within the 48h threshold -> warn (caught BEFORE the first 401)
auth_probe._read_oauth_block = lambda: _block(NOW + 12 * 3600)  # 12h left
st = auth_probe.credential_status(now_epoch=NOW)
chk("expires within 48h -> warn", st["level"] == "warn", str(st))
chk("within-threshold warn carries hours_left", st.get("expires_in_h") is not None, str(st))

# already past -> warn
auth_probe._read_oauth_block = lambda: _block(NOW - 3600)
st = auth_probe.credential_status(now_epoch=NOW)
chk("already expired -> warn", st["level"] == "warn", str(st))

# ISO-8601 expiresAt (the ticket's observed shape) parses the same as an epoch
import datetime as _dt
iso = _dt.datetime.fromtimestamp(NOW + 12 * 3600, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
auth_probe._read_oauth_block = lambda: {"expiresAt": iso, "refreshToken": "r"}
st = auth_probe.credential_status(now_epoch=NOW)
chk("ISO-8601 expiresAt is parsed (within 48h -> warn)", st["level"] == "warn", str(st))

# just over the threshold -> ok (the threshold is a real boundary, not always-warn)
auth_probe._read_oauth_block = lambda: _block(NOW + 72 * 3600)  # 3 days out
st = auth_probe.credential_status(now_epoch=NOW)
chk("72h out (past the 48h threshold) -> ok", st["level"] == "ok", str(st))

# no credentials file -> unknown (presence-only; must NOT false-red a box with no claude login)
auth_probe._read_oauth_block = lambda: None
st = auth_probe.credential_status(now_epoch=NOW)
chk("no credentials file -> unknown (presence-only, not a false red)", st["level"] == "unknown", str(st))

# exercise the REAL _read_oauth_block: it must UNWRAP the "claudeAiOauth" envelope a real
# `claude /login` writes, and parse the ISO expiresAt inside it (no stubbed seam here).
auth_probe._read_oauth_block = _orig_read_block
import json as _json
_home = Path(tempfile.mkdtemp())
(_home / ".claude").mkdir(parents=True)
_real_iso = _dt.datetime.fromtimestamp(NOW + 12 * 3600, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
(_home / ".claude" / ".credentials.json").write_text(
    _json.dumps({"claudeAiOauth": {"expiresAt": _real_iso, "refreshToken": "r",
                                   "subscriptionType": "max"}}), encoding="utf-8")
_orig_home = auth_probe.Path.home
auth_probe.Path.home = lambda: _home
try:
    st = auth_probe.credential_status(now_epoch=NOW)
finally:
    auth_probe.Path.home = _orig_home
chk("real read unwraps claudeAiOauth + parses ISO (within 48h -> warn)",
    st["level"] == "warn" and st.get("expires_in_h") is not None, str(st))

# env-based auth short-circuits to ok (the OAuth file isn't the source on an API-key box)
for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
    os.environ[k] = "x"; auth_probe._read_oauth_block = lambda: _block(NOW - 3600)
    chk(f"{k} set -> ok (env auth is not file-expiry-bound)",
        auth_probe.credential_status(now_epoch=NOW)["level"] == "ok", k)
    os.environ.pop(k, None)


# ================================================================================================ #
# AC2/AC3: ceremonies — a 401 result alerts, never broadcasts the raw error, never writes artefacts
# ================================================================================================ #
print("\n=== AC2/AC3: ceremonies alert-not-broadcast + skip poisoned artefacts ===")
tmp = Path(tempfile.mkdtemp())
ROOT = tempfile.mkdtemp(); (Path(ROOT) / ".git").mkdir()
app = AppConfig(name="Elite-Unit", repo_path=ROOT, base_branch="dev", workdir=ROOT,
                gate_commands=[f"{sys.executable} tests/run_all.py"], backlog_backend="none")
cfg = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
ns = types.SimpleNamespace
sent: list[str] = []
council.notify.send = lambda text, chat_id=None: (sent.append(text), True)[1]
standup_file = council._standup_file(cfg)
roster_doc = roster.doc_path(cfg)


def _agent_returning(text):
    """A run_agent stub whose every call returns `text` (the model output / error string)."""
    async def _fake(prompt, options, tag=None):
        return ns(final=text, text=text, is_error=bool(auth_probe.looks_like_provider_error(text)),
                  cost_usd=0.0, num_turns=1, tools=[], provider="Anthropic", model_version="m",
                  is_plan_limit=False, plan_limit_kind="", is_turn_limit=False, duration_s=0.1,
                  input_tokens=0, output_tokens=0)
    return _fake


# --- daily_brief: a 401 result becomes the operator alert, NOT a broadcast brief ---------------
from orchestrator.audit import AuditLog as _AuditLog
_down_audit = _AuditLog(str(tmp / "down-audit.jsonl"))
council.run_agent = _agent_returning(AUTH_401)
auth_probe._cache["result"] = {"state": "valid", "detail": "stale", "checked_at": 0.0}
auth_probe._cache["at"] = NOW
sent.clear()
out = asyncio.run(council.daily_brief(cfg, audit=_down_audit, broadcast=True))
chk("daily_brief returns the operator alert on a 401 (not the raw error)",
    "authenticate" in out.lower() and "401" not in out, out)
chk("daily_brief broadcast the ALERT, not the provider error",
    any("authenticate" in s.lower() for s in sent) and not any("401" in s for s in sent), str(sent))
chk("daily_brief never sent the raw provider error to Telegram",
    not any(AUTH_401 in s for s in sent), str(sent))
chk("the auth-outage invalidated the stale 'valid' probe cache (health degrades on next check)",
    auth_probe._cache["result"] is None, str(auth_probe._cache))
import json as _json
_down_rows = [_json.loads(ln) for ln in (tmp / "down-audit.jsonl").read_text().splitlines() if ln.strip()]
_down_row = _down_rows[-1] if _down_rows else {}
chk("the outage is recorded in audit.jsonl with auth_down=True (not broadcast as a brief)",
    _down_row.get("event") == "daily_brief" and _down_row.get("auth_down") is True,
    str(_down_row))

# control: a HEALTHY result is still broadcast as the brief (no false suppression)
council.run_agent = _agent_returning(REAL_BRIEF)
sent.clear()
out = asyncio.run(council.daily_brief(cfg, audit=None, broadcast=True))
chk("control: a healthy daily_brief still broadcasts the real brief",
    any("EU-430" in s for s in sent), str(sent))


# --- hold_council (daily muster): 401 officer reports never reach last-standup.md / ROSTER.md ---
council.run_agent = _agent_returning(AUTH_401)
auth_probe.invalidate()
sent.clear()
try:
    standup_file.unlink(missing_ok=True)
except OSError:
    pass
try:
    roster_doc.unlink(missing_ok=True)
except OSError:
    pass
# pre-seed a poisoned stand-up file so we can assert it is NOT overwritten with more 401 text
standup_file.write_text("PRE-EXISTING CLEAN STATE\n", encoding="utf-8")
out = asyncio.run(council.hold_council(cfg, audit=None, broadcast=True))
chk("hold_council returns the operator alert on auth failure",
    "authenticate" in out.lower() and "401" not in out, out)
chk("hold_council broadcast the alert, not the provider error",
    any("authenticate" in s.lower() for s in sent) and not any("401" in s for s in sent), str(sent))
chk("hold_council did NOT overwrite last-standup.md with poisoned 401 text",
    "401" not in standup_file.read_text(encoding="utf-8"), standup_file.read_text(encoding="utf-8")[:120])
chk("ROSTER.md was NOT regenerated with a provider-error status line",
    not roster_doc.exists() or "> " + AUTH_401[:20] not in roster_doc.read_text(encoding="utf-8"),
    roster_doc.read_text(encoding="utf-8")[:120] if roster_doc.exists() else "(absent)")

# control: a healthy muster DOES write the stand-up file (no false suppression of the artefact)
council.run_agent = _agent_returning("Yesterday: quiet. Today: stand-up. Blockers: none.")
sent.clear()
asyncio.run(council.hold_council(cfg, audit=None, broadcast=True))
chk("control: a healthy muster writes last-standup.md",
    standup_file.exists() and "401" not in standup_file.read_text(encoding="utf-8"),
    standup_file.read_text(encoding="utf-8")[:120] if standup_file.exists() else "(absent)")


# ================================================================================================ #
# AC5: roster — a provider-error body is never persisted as the ROSTER status line
# ================================================================================================ #
print("\n=== AC5: roster guard — provider error never becomes the status line ===")
_bd = roster.build_doc(cfg, AUTH_401)
# a provider-error status is dropped: no status blockquote line, and the error text never appears
# (the mermaid ' --> ' arrows legitimately contain '> ' as a substring, so check the blockquote LINE)
chk("build_doc drops a provider-error status (never embedded as a blockquote or body)",
    not any(ln.startswith("> ") for ln in _bd.splitlines())
    and "401" not in _bd and "Failed to authenticate" not in _bd, _bd[:200])
# write a poisoned file and confirm latest_status does not surface the error as the status
roster_doc.parent.mkdir(parents=True, exist_ok=True)
roster_doc.write_text(f"# Roster\n\n> {AUTH_401}\n\n## Officers\n", encoding="utf-8")
chk("latest_status does not return a provider-error line as the unit's status",
    roster.latest_status(cfg) == "", repr(roster.latest_status(cfg)))
roster_doc.unlink(missing_ok=True)


# ================================================================================================ #
# AC4 wiring: the ceremony pre-flight sends a Telegram warning at the threshold
# ================================================================================================ #
print("\n=== AC4 wiring: ceremony pre-flight warns to Telegram at the threshold ===")
auth_probe._read_oauth_block = lambda: _block(NOW + 12 * 3600, refresh="r")  # within 48h
sent.clear()
# drive the pre-flight with a fixed clock by monkeypatching credential_status's now
_orig_credential_status = auth_probe.credential_status
auth_probe.credential_status = lambda **kw: _orig_credential_status(now_epoch=NOW, **kw)
try:
    council._auth_preflight(cfg)
finally:
    auth_probe.credential_status = _orig_credential_status
chk("pre-flight warns to Telegram when the credential is within the threshold",
    any("auth pre-flight" in s.lower() for s in sent), str(sent))

# a healthy credential fires NO pre-flight warning (no spam on a good box)
auth_probe._read_oauth_block = lambda: _block(NOW + 10 * 86400, refresh="r")
sent.clear()
auth_probe.credential_status = lambda **kw: _orig_credential_status(now_epoch=NOW, **kw)
try:
    council._auth_preflight(cfg)
finally:
    auth_probe.credential_status = _orig_credential_status
chk("pre-flight is silent on a healthy credential (no false alarm)",
    sent == [], str(sent))


print("\n================ EU-430 VPS AUTH OUTAGE QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
