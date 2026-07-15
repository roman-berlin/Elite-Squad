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

import os
import shutil
import subprocess
import threading
import time

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
)

# Internal sentinels _run_probe returns in place of CLI output when the probe can't produce one,
# so ALL classification lives in _classify (and tests can drive every path through one seam).
_NO_BINARY_SENTINEL = "__no_claude_binary__"
_TIMEOUT_SENTINEL = "__probe_timeout__"

_cache: dict = {"at": 0.0, "result": None}
_cache_lock = threading.Lock()


def is_login_failure(text: str | None) -> bool:
    """True when ``text`` (CLI output or a builder report's notes) carries a login-failure marker
    — credential present but REJECTED. Never matches network/timeout/turn-limit text."""
    t = (text or "").lower()
    return bool(t) and any(m in t for m in _LOGIN_FAILURE_MARKERS)


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
    rc, out = _run_probe(timeout=timeout)
    state, detail = _classify(rc, out)
    result = {"state": state, "detail": detail, "checked_at": now}
    with _cache_lock:
        _cache["at"] = now
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
