"""Claude-credential LIVENESS probe — is the detected login actually VALID right now?

2026-07-15 ~22:05 incident: the drain burned error strikes on 5+ tickets because every builder
call failed with "Not logged in · Please run /login" — the Claude Code OAuth token had EXPIRED —
while ``health.summary()`` reported ``healthy: True``. Root cause: ``config.detected_auth()`` /
``config._claude_login_present()`` only check that a credential SOURCE exists (env var set,
credentials file non-empty, keychain entry present); neither ever checks the credential is VALID.

This module is the missing liveness dimension. Two entry points:

  * ``probe()``            — run one trivial ``claude -p`` round-trip (short timeout) and classify
                             the outcome into ``valid | expired | unreachable | unknown``. Cached
                             in-process for ``_CACHE_TTL_S`` (~15 min) so ``health.checks()`` /
                             the cockpit ``/api/health`` stay fast. ``GENERAL_AUTH_PROBE=0``
                             disables the subprocess entirely (state ``unknown`` → presence-only);
                             ``tests/run_all.py`` sets that for every harness — the suite's
                             contract is no network, no real models.
  * ``is_login_failure()`` — marker test for a builder-failure notes string ("Not logged in",
                             "Please run /login", …) so ``autopilot`` can route an expired-login
                             builder failure to the EU-228 no-strike hold path instead of charging
                             error strikes toward parking (see ``autopilot._enter_auth_hold``).

State semantics (mirrors ``infra_classify``'s invalid-vs-offline distinction — an outage must
never read as a bad credential, and vice versa):

  ``valid``        — the probe completed a round-trip; the credential works right now.
  ``expired``      — the CLI answered with a login-failure marker: credential present but REJECTED.
  ``unreachable``  — network/API down (infra_classify-style markers, or the probe timed out):
                     could not verify; NOT evidence the credential is bad.
  ``unknown``      — the probe couldn't run at all (no ``claude`` binary, probe disabled, or an
                     unrecognizable failure): presence-only detection (``config.detected_auth``)
                     is the best information available — behave exactly as before this module.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

_CACHE_TTL_S = 900.0      # ~15 min — health/summary callers ride this cache
_PROBE_TIMEOUT_S = 30.0   # one trivial round-trip; a hang must never stall health for long

# GUI-launched apps inherit a minimal PATH, so `which` alone can miss the claude binary
# (mirrors health._EXTRA_BINS; ~/.local/bin added — the native installer's default).
_EXTRA_BINS = ("/opt/homebrew/bin", "/usr/local/bin",
               os.path.expanduser("~/.bun/bin"), os.path.expanduser("~/.local/bin"))

# Login-failure markers, matched case-insensitively against CLI output / builder-failure notes.
# Kept TIGHT on purpose: a broad marker ("token expired") would also match a Jira-credential
# failure in a builder's notes and hold the whole drain on the wrong signal. These are the exact
# shapes the Claude Code CLI / Anthropic API emit for a dead credential.
_LOGIN_FAILURE_MARKERS = (
    "not logged in",             # "Not logged in · Please run /login" (the 2026-07-15 signature)
    "please run /login",
    "invalid api key",           # "Invalid API key · Please run /login"
    "authentication_error",      # the API error type the SDK/CLI surface on a rejected credential
    "oauth token has expired",
    "oauth token revoked",
    # EU-430 (2026-07-22 VPS outage): the dead Max-plan OAuth credential surfaced as a 401 the
    # existing markers did NOT match — "Invalid authentication credentials" (the API 401 body) and
    # "OAuth access token has expired" (the refresh path). Without these the council broadcast the
    # raw 401 to the Commander's phone as the daily brief for 3 ceremonies. Note "oauth token has
    # expired" above does NOT substring-match "oauth ACCESS token has expired" (the extra word), so
    # the full observed shape is listed explicitly.
    "invalid authentication credentials",
    "oauth access token has expired",
)

# EU-430: provider-error shapes broader than the login markers — a model result that is actually an
# error string, not content. Used by the council/roster ARTEFACT guard so a failing/dead credential
# is never broadcast as the brief or persisted into ROSTER.md / last-standup.md. These prefixes
# never appear in a legitimate stand-up line or briefing, so the guard can't censor real content.
_PROVIDER_ERROR_MARKERS = (
    "api error:",                # "Failed to authenticate. API Error: 401 …"
    "failed to authenticate",
)

# Internal sentinels _run_probe returns in place of CLI output when the probe can't produce one,
# so ALL classification lives in _classify (and tests can drive every path through one seam).
_NO_BINARY_SENTINEL = "__no_claude_binary__"
_TIMEOUT_SENTINEL = "__probe_timeout__"

_cache: dict = {"at": 0.0, "result": None}
_cache_lock = threading.Lock()
# EU-366: single-flight the expensive `claude -p` probe. `_cache_lock` only guards the tiny
# read/write of the cache dict — it is deliberately released around the up-to-30s subprocess, so a
# burst of health callers at TTL expiry each used to launch their OWN probe (a stampede). This lock
# serializes the actual probe so exactly one runs; the others wait and read its fresh result.
_probe_lock = threading.Lock()


def is_login_failure(text: str | None) -> bool:
    """True when ``text`` (CLI output or a builder report's notes) carries a login-failure marker
    — credential present but REJECTED. Never matches network/timeout/turn-limit text."""
    t = (text or "").lower()
    return bool(t) and any(m in t for m in _LOGIN_FAILURE_MARKERS)


def looks_like_provider_error(text: str | None) -> bool:
    """True when ``text`` is a provider/auth error string rather than real model output (EU-430).

    Broader than :func:`is_login_failure`: a model call that fails auth returns the provider's raw
    error as its ``result`` (e.g. ``"Failed to authenticate. API Error: 401 …"``). The council and
    roster artefact guards use this so such a string is NEVER broadcast as the daily brief or
    persisted into ROSTER.md / last-standup.md / the transcript. Never matches ordinary briefing or
    stand-up text — these shapes are never legitimate content."""
    if not text:
        return False
    low = text.strip().lower()
    if is_login_failure(low):
        return True
    return any(m in low for m in _PROVIDER_ERROR_MARKERS)


def _disabled() -> bool:
    return os.environ.get("GENERAL_AUTH_PROBE", "").strip().lower() in ("0", "false", "off", "no")


def _claude_bin() -> str | None:
    p = shutil.which("claude")
    if p:
        return p
    for d in _EXTRA_BINS:
        c = os.path.join(d, "claude")
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _run_probe(timeout: float = _PROBE_TIMEOUT_S) -> tuple[int | None, str]:
    """One trivial ``claude -p ok`` call → ``(returncode, combined stdout+stderr)``.

    ``(None, <sentinel>)`` when the probe couldn't run (no binary / timed out / spawn error) —
    classification of every shape happens in ``_classify``, never here. Inherits this process's
    environment on purpose: the probe must validate the SAME credential the builders will use
    (ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN / the claude login session)."""
    bin_ = _claude_bin()
    if bin_ is None:
        return (None, _NO_BINARY_SENTINEL)
    try:
        r = subprocess.run([bin_, "-p", "ok"], capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
        return (r.returncode, (r.stdout or "") + "\n" + (r.stderr or ""))
    except subprocess.TimeoutExpired:
        return (None, _TIMEOUT_SENTINEL)
    except Exception as exc:  # noqa: BLE001 - a probe failure must never take health down with it
        return (None, f"__probe_error__ {exc}")


def _classify(rc: int | None, out: str) -> tuple[str, str]:
    """Map one probe run to ``(state, detail)`` — see the module docstring for state semantics."""
    if rc == 0:
        return ("valid", "verified live — `claude -p` round-trip OK")
    low = (out or "").lower()
    if _NO_BINARY_SENTINEL in low:
        return ("unknown", "`claude` CLI not found — presence-only detection")
    if is_login_failure(low):
        first = next((ln.strip() for ln in (out or "").splitlines() if ln.strip()), "")
        return ("expired", f"credential REJECTED: {first[:120]}")
    if _TIMEOUT_SENTINEL in low:
        return ("unreachable", f"probe timed out after {int(_PROBE_TIMEOUT_S)}s — API slow or network down")
    # infra_classify-style markers distinguish "network down" from "credential bad" (EU-228).
    # Lazy import: infra_classify pulls in the SDK-heavy loop module, and health/doctor must keep
    # working (reporting "Agent SDK: bad") on a box where the SDK isn't installed.
    try:
        from . import infra_classify
        tag = infra_classify.classify(low)
    except Exception:  # noqa: BLE001 - no classifier available → can't tell, fall through
        tag = ""
    if tag:
        return ("unreachable", f"network/API unreachable ({tag}) — could not verify the credential")
    return ("unknown", "probe failed without a recognizable auth/network marker — presence-only")


def probe(*, force: bool = False, max_age_s: float | None = None,
          timeout: float = _PROBE_TIMEOUT_S) -> dict:
    """Cached liveness verdict: ``{"state", "detail", "checked_at"}``.

    A cached result younger than ``max_age_s`` (default ``_CACHE_TTL_S``) is returned as-is so
    the frequent callers (health.checks, the cockpit) stay fast; ``force=True`` always re-probes
    (the doctor uses this — a live diagnostic must not show a stale verdict right after a /login
    fix). ``GENERAL_AUTH_PROBE=0`` disables the subprocess entirely and reports ``unknown``."""
    if _disabled():
        return {"state": "unknown",
                "detail": "probe disabled (GENERAL_AUTH_PROBE=0) — presence-only", "checked_at": 0.0}
    now = time.time()
    ttl = _CACHE_TTL_S if max_age_s is None else max_age_s
    with _cache_lock:
        cached = _cache["result"]
        if not force and cached is not None and (now - _cache["at"]) < ttl:
            return dict(cached)
    # EU-366 single-flight: serialize the subprocess so a burst of callers runs ONE probe. After
    # acquiring, re-check the cache — a probe that completed while we waited means we return its
    # fresh result instead of launching a redundant one (skipped when force=True: force demands a
    # genuinely fresh probe).
    with _probe_lock:
        with _cache_lock:
            cached = _cache["result"]
            if not force and cached is not None and (time.time() - _cache["at"]) < ttl:
                return dict(cached)
        rc, out = _run_probe(timeout=timeout)
        state, detail = _classify(rc, out)
        checked_at = time.time()
        result = {"state": state, "detail": detail, "checked_at": checked_at}
        with _cache_lock:
            _cache["at"] = checked_at
            _cache["result"] = dict(result)
        return result


def invalidate() -> None:
    """Drop the cached verdict so the next ``probe()`` re-runs for real. The autopilot calls this
    when a builder failure carries a login-failure marker: hard evidence the cached "valid" (up to
    15 min old) is stale — the auth-hold recheck must re-verify, not read that stale cache and
    instantly (wrongly) clear the hold."""
    with _cache_lock:
        _cache["at"] = 0.0
        _cache["result"] = None


# --------------------------------------------------------------------------- #
# EU-430 (2026-07-22): credential-EXPIRY pre-flight — catch a soon-to-lapse or
# unrefreshable login BEFORE the first 401, not after the fourth failed brief.
# --------------------------------------------------------------------------- #
def _read_oauth_block() -> dict | None:
    """Best-effort read of the Claude OAuth credentials block (the file ``claude /login`` writes on
    a headless/VPS box). Returns the dict carrying ``expiresAt`` / ``refreshToken``, or None when
    absent/unreadable. Never raises; never returns a secret beyond the structural fields the expiry
    pre-flight must inspect. (The macOS Keychain store has no expiry field to read, so a Darwin box
    without the file correctly falls through to ``None`` → ``unknown``.)"""
    home = Path.home()
    for p in (home / ".claude" / ".credentials.json",
              home / ".config" / "claude" / ".credentials.json"):
        try:
            if not p.is_file():
                continue
            data = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, ValueError):
            continue
        block = data.get("claudeAiOauth") if isinstance(data, dict) else None
        if not isinstance(block, dict):
            block = data if isinstance(data, dict) else None
        if isinstance(block, dict) and any(k in block for k in ("expiresAt", "refreshToken", "accessToken")):
            return block
    return None


def _parse_expires_at(val) -> float | None:
    """epoch-seconds from an ``expiresAt`` value — tolerates epoch-seconds, epoch-millis, ISO-8601
    (e.g. ``"2026-07-19T16:30:06Z"`` — the shape the 2026-07-22 audit observed on the VPS), or
    None/unparseable → None."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        v = float(val)
        return v / 1000.0 if v > 1e12 else v          # ms (anything past ~2001 in ms) → seconds
    s = str(val).strip()
    if not s:
        return None
    try:                                              # epoch-as-string
        v = float(s)
        return v / 1000.0 if v > 1e12 else v
    except ValueError:
        pass
    try:                                              # ISO-8601
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def credential_status(*, threshold_h: float = 48.0, now_epoch: float | None = None) -> dict:
    """PRE-FLIGHT (EU-430 AC4): is the Claude credential about to lapse, or already unrefreshable?

    Returns ``{"level", "reason", "expires_in_h", "path"}`` where ``level`` is:

      ``"warn"``    — the credentials FILE is the auth source AND (``refreshToken`` is empty OR
                      ``expiresAt`` is within ``threshold_h`` hours of now / already past). This is
                      the VPS's exact dead condition (empty refresh token → cannot self-heal).
      ``"ok"``      — a healthy file (non-empty refresh + a comfortably-future expiry), OR env-based
                      auth (``ANTHROPIC_API_KEY`` / ``CLAUDE_CODE_OAUTH_TOKEN``) which is not
                      file-expiry-bound.
      ``"unknown"`` — no credentials file and no env auth: presence-only (the liveness probe handles
                      validity); must NOT false-red a box that simply has no claude login.

    Never raises. Pure (no side effects) — the ceremony wires a Telegram warning off a ``"warn"``
    verdict (``council._auth_preflight``)."""
    path = str(Path.home() / ".claude" / ".credentials.json")
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return {"level": "ok",
                "reason": "env-based auth (API key / OAuth token) — not file-expiry-bound",
                "expires_in_h": None, "path": path}
    block = _read_oauth_block()
    if block is None:
        return {"level": "unknown",
                "reason": "no credentials file — presence-only (the liveness probe handles validity)",
                "expires_in_h": None, "path": path}
    refresh = str(block.get("refreshToken") or "").strip()
    if not refresh:
        return {"level": "warn",
                "reason": "refreshToken is EMPTY — the login cannot self-heal; re-auth on the box",
                "expires_in_h": None, "path": path}
    exp = _parse_expires_at(block.get("expiresAt"))
    if exp is None:
        return {"level": "unknown",
                "reason": "credentials present but expiresAt unreadable — can't pre-flight expiry",
                "expires_in_h": None, "path": path}
    now = now_epoch if now_epoch is not None else time.time()
    hours_left = (exp - now) / 3600.0
    if hours_left <= threshold_h:
        reason = (f"credential EXPIRED {-hours_left:.1f}h ago — re-auth now" if hours_left <= 0
                  else f"credential expires in {hours_left:.1f}h "
                       f"(within {threshold_h:.0f}h threshold) — re-auth before it lapses")
        return {"level": "warn", "reason": reason, "expires_in_h": round(hours_left, 2), "path": path}
    return {"level": "ok", "reason": f"credential valid for {hours_left:.1f}h more",
            "expires_in_h": round(hours_left, 2), "path": path}
