"""EU-189: model-backend selection — Opus (Claude) vs GLM (Z.ai), chosen from the cockpit.

The operator picks a backend per run; every officer in that run executes against it. Because GLM
is reached through Z.ai's **Anthropic-compatible** endpoint, both backends speak the same protocol
— this is plumbing, not request translation.

How it works
------------
A run pins ONE backend via a run-scoped ``contextvars.ContextVar`` set at the top of ``loop.run``
from ``cfg.model_backend``. The single SDK seam (``agent._run_agent_unrouted``) calls
``apply(options)`` just before ``query()``:

* **Opus (native)** — a pure no-op. ``options.env`` is left empty by ``apply``, so the SDK
  subprocess inherits the parent environment (the Max subscription via ``claude login`` /
  ``CLAUDE_CODE_OAUTH_TOKEN``) exactly as today. (The EU-255 Jira/Telegram credential scrub is a
  separate step applied at the SDK seam AFTER ``apply`` — see ``secret_strip_overrides`` below.)
* **GLM** — writes the z.ai endpoint + bearer into ``options.env`` for THAT ONE subprocess. The SDK
  transport builds ``process_env = {**os.environ, ..., **options.env, ...}`` (verified against
  claude_agent_sdk 0.2.101), so ``options.env`` overrides the inherited env for the child process
  only — **zero** ``os.environ`` mutation, so Opus and GLM coexist and concurrent runs never bleed.

Credentials (env only — never YAML, never logged, never rendered)
-----------------------------------------------------------------
GLM authenticates with a **bearer token** on ``ANTHROPIC_AUTH_TOKEN`` (NOT ``ANTHROPIC_API_KEY``):

  ``GLM_BASE_URL``        default ``https://api.z.ai/api/anthropic``
  ``GLM_AUTH_TOKEN``      the z.ai key — sent as ``ANTHROPIC_AUTH_TOKEN``
  ``GLM_MODEL``           default ``glm-4.6``
  ``GLM_API_TIMEOUT_MS``  default ``3000000``

The cockpit shows key **presence** (``available('glm')``), never the value.

Safety
------
* **Fail-closed.** GLM selected but ``GLM_AUTH_TOKEN`` absent → ``apply`` falls back to NATIVE and
  warns. It NEVER points a subprocess at z.ai with an empty bearer — which would let the CLI fall
  through to the stored Anthropic OAuth and leak the Max token to a third party.
* **Credential isolation.** A GLM call blanks ``ANTHROPIC_API_KEY`` and ``CLAUDE_CODE_OAUTH_TOKEN``
  in ``options.env`` so Anthropic subscription creds are never forwarded to z.ai.
* **Untrusted-execution minimization (EU-255).** The SDK seam (``agent._run_agent_unrouted``)
  merges :func:`secret_strip_overrides` into ``options.env`` right AFTER ``apply`` — for BOTH
  backends — blanking ``JIRA_EMAIL``/``JIRA_API_TOKEN``/``TELEGRAM_BOT_TOKEN``/``TELEGRAM_CHAT_ID``/
  ``GENERAL_COCKPIT_PROMOTE`` (and any other ``JIRA_``/``TELEGRAM_``-prefixed var), since the
  officer subprocess builds/tests untrusted product code and Jira/Telegram calls only ever happen
  in the orchestrator's own process. Kept out of ``apply`` so ``apply`` stays a pure model/backend
  transform; ``gate.py`` omits the same keys via :func:`is_sensitive_key` at its own subprocess seam.
* **No shared mutable state.** The contextvar is per-run; ``apply`` builds a fresh dict every call.
"""
from __future__ import annotations

import contextvars
import os
import re

# Canonical backend ids.
NATIVE = "opus"   # Anthropic / Claude — the default; env untouched (Max subscription inherited).
GLM = "glm"       # Z.ai GLM via the Anthropic-compatible endpoint.

# Run-scoped selection. Default NATIVE so any SDK call outside a run (meetings, ad-hoc) stays Opus.
_BACKEND: contextvars.ContextVar[str] = contextvars.ContextVar("model_backend", default=NATIVE)

# EU-236: the run-scoped ModelRegistry that apply() resolves a registry-id pin against — set by
# loop.run alongside the backend id. Keeping it run-scoped (cfg-anchored) is what lets apply() stay
# hermetic when it is called at the SDK seam with NO explicit registry (agent._run_agent_unrouted):
# it resolves against THIS run's store, not the live state/ store. Default None -> apply() falls
# back to a live-anchored ModelRegistry().
_REGISTRY: contextvars.ContextVar = contextvars.ContextVar("model_registry", default=None)

# GLM defaults — overridable via env. The token has NO default: it must be provided or GLM is off.
_GLM_BASE_URL_DEFAULT = "https://api.z.ai/api/anthropic"
_GLM_MODEL_DEFAULT = "glm-4.6"
_GLM_SMALL_FAST_DEFAULT = "glm-4.5-air"   # EU-190: the SDK's background/small-fast model under GLM
_GLM_TIMEOUT_DEFAULT = "3000000"

# The literal aliases normalize() recognizes for each canonical id. Extracted as constants (rather
# than inlined in normalize()) so EU-236's apply() can tell "an explicit opus/glm alias" apart from
# "an unrecognized id that might be a registry backend" without re-deriving normalize()'s rules.
_GLM_ALIASES = ("glm", "zai", "z.ai", "z-ai")
_NATIVE_ALIASES = ("", "opus", "native", "anthropic", "claude")


def normalize(value: str | None) -> str:
    """Map a raw selection (cockpit form value / config) to a canonical backend id.

    ``glm`` / ``zai`` / ``z.ai`` → :data:`GLM`; everything else (incl. ``opus`` / ``native`` /
    ``anthropic`` / ``claude`` / ``''`` / typos) → :data:`NATIVE`. Unknown degrades SAFELY to
    NATIVE — never silently to GLM.
    """
    v = (value or "").strip().lower()
    if v in _GLM_ALIASES:
        return GLM
    return NATIVE


def resolve_selection(value: str | None, registry=None) -> str:
    """EU-236: resolve a raw backend selection (a cockpit form value or a persisted preference) to a
    persist/run-ready id — WITHOUT flattening a registry id.

    An ``opus``/``glm`` alias maps to its canonical id; a value that is a KNOWN registry record id
    (per ``registry``) is returned VERBATIM; anything else degrades SAFELY to :data:`NATIVE`. Unlike
    :func:`normalize` (which collapses every non-alias to NATIVE), this preserves custom-backend ids
    so they can thread through ``backend_pref`` / the run pin / :func:`apply`; unlike a blind
    pass-through, an unknown/deleted id still falls back to NATIVE — never silently to GLM.
    """
    v = (value or "").strip()
    if not v:
        return NATIVE
    vl = v.lower()
    if vl in _GLM_ALIASES:
        return GLM
    if vl in _NATIVE_ALIASES:
        return NATIVE
    if registry is not None:
        try:
            if registry.get(v) is not None:
                return v
        except Exception:  # noqa: BLE001 - a corrupt/unreadable registry must never break selection
            pass
    return NATIVE


def _pin_value(value: str | None) -> str:
    """Canonicalize a RUN PIN for the contextvar: an ``opus``/``glm`` alias -> its canonical id; any
    other non-empty value (an EU-236 registry-id candidate) is preserved VERBATIM so :func:`current`
    (and thus :func:`apply`) can resolve it — ``apply`` fails closed if the id resolves to no
    backend. Empty/None -> NATIVE. NEVER maps an unknown value to GLM."""
    v = (value or "").strip()
    if not v:
        return NATIVE
    vl = v.lower()
    if vl in _GLM_ALIASES:
        return GLM
    if vl in _NATIVE_ALIASES:
        return NATIVE
    return v


def set_backend(value: str | None, registry=None):
    """Pin the backend for the current run context. Returns a token for :func:`reset_backend`.

    ``value`` may be an ``opus``/``glm`` alias OR (EU-236) a model-registry record id — the id is
    preserved verbatim (see :func:`_pin_value`) so :func:`current`/:func:`apply` can resolve it.

    ``registry`` (EU-236, optional): the cfg-anchored :class:`ModelRegistry` this run resolves its
    registry ids against; stored run-scoped so :func:`apply`, called at the SDK seam with no explicit
    registry, stays hermetic instead of reaching the live ``state/`` store.
    """
    reg_token = _REGISTRY.set(registry)
    bk_token = _BACKEND.set(_pin_value(value))
    return (bk_token, reg_token)


def reset_backend(token) -> None:
    """Restore the backend + registry contextvars from the ``(backend, registry)`` token pair
    returned by :func:`set_backend`. Best-effort — a token from another context (or the wrong shape)
    is ignored."""
    try:
        bk_token, reg_token = token
    except (TypeError, ValueError):  # pragma: no cover - a non-pair token (bad caller)
        return
    for var, tok in ((_BACKEND, bk_token), (_REGISTRY, reg_token)):
        try:
            var.reset(tok)
        except (ValueError, LookupError):  # pragma: no cover - token from a different context
            pass


def current() -> str:
    """The backend pinned for the current run (:data:`NATIVE` outside any run)."""
    return _BACKEND.get()


# EU-255: keys/prefixes that must NEVER reach an officer subprocess building untrusted product
# code. Jira/Telegram calls happen ONLY in the orchestrator's own (parent) process — an officer
# subprocess never legitimately needs them, yet the SDK builds `{**os.environ, **options.env}`
# (subprocess_cli.py), so the full parent env — including these — is inherited unless blanked here.
SENSITIVE_KEYS: frozenset[str] = frozenset({
    "JIRA_EMAIL",
    "JIRA_API_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "GENERAL_COCKPIT_PROMOTE",
    # EU-371(3), 2026-07-16 total audit: the paid z.ai bearer was inherited by every officer/gate
    # subprocess building untrusted product code (EU-255 stripped Jira/Telegram only). Safe to
    # strip for GLM runs too — :func:`apply` reads the token in the PARENT (os.environ, untouched)
    # and hands it to the child as ANTHROPIC_AUTH_TOKEN, a different key (see ``_glm_env``).
    "GLM_AUTH_TOKEN",
})
SENSITIVE_PREFIXES: tuple[str, ...] = ("JIRA_", "TELEGRAM_")


def is_sensitive_key(key: str) -> bool:
    """True if ``key`` is one of the credentials an untrusted-code subprocess must never see."""
    return key in SENSITIVE_KEYS or any(key.startswith(p) for p in SENSITIVE_PREFIXES)


def secret_strip_overrides() -> dict[str, str]:
    """Blank overrides for every sensitive key PRESENT in the parent env, to merge into an officer
    subprocess's ``options.env`` right before spawn (done at the single SDK seam
    ``agent._run_agent_unrouted``, NOT here in :func:`apply`, which stays a pure model/backend
    transform). Blanking (not omitting) is required because the SDK merges ``options.env`` OVER the
    inherited ``os.environ`` — a key simply absent from ``options.env`` still comes through from the
    parent. A fresh dict every call; os.environ itself is never touched, so orchestrator-side
    Jira/Telegram calls in THIS process are unaffected."""
    return {k: "" for k in os.environ if is_sensitive_key(k)}


def _glm_token() -> str:
    return (os.environ.get("GLM_AUTH_TOKEN") or "").strip()


def available(backend: str) -> bool:
    """True if ``backend`` can actually run. NATIVE is always available; GLM needs its token."""
    if normalize(backend) == GLM:
        return bool(_glm_token())
    return True


def resolve_for_run(cfg=None, app_name: str | None = None) -> tuple[str, str | None]:
    """2026-07-19 (Commander order): the MAIN/SECONDARY pick for a run.

    Returns ``(backend, fallback_reason)``: the app's main model (backend_pref.active) when it is
    usable, else the configured SECONDARY when that is usable — with a human reason string so the
    caller can audit/notify — else the main unchanged (the existing hard blocks then fire exactly
    as before, so with NO secondary configured behaviour is byte-identical to the old world).

    "Usable" is deliberately cheap and static-plus-cached: GLM needs its token/config
    (glm_config_issues); the native Claude backend needs the plan limit NOT hit
    (usage.plan_limit_hit — itself cached); a custom registry backend counts usable (no probe).
    Never raises."""
    try:
        from . import backend_pref
        primary = backend_pref.active(cfg, app_name)
    except Exception:  # noqa: BLE001
        return NATIVE, None
    try:
        sec = backend_pref.get_secondary(cfg)
    except Exception:  # noqa: BLE001
        sec = None
    if not sec or sec == primary:
        # No stand-in configured → the old world, at the old cost: no usability probe at all
        # (plan_limit_hit shells out on a cold cache — too heavy for every launch).
        return primary, None

    def _usable(bk: str) -> tuple[bool, str]:
        try:
            if normalize(bk) == GLM:
                iss = glm_config_issues()
                return (not iss, "; ".join(iss) if iss else "")
            if normalize(bk) == NATIVE:
                from . import usage
                hit = usage.plan_limit_hit(cfg) if cfg is not None else {}
                return (not hit.get("hit"), "plan limit hit" if hit.get("hit") else "")
            return True, ""
        except Exception:  # noqa: BLE001
            return True, ""   # an unknowable state must never block dispatch here

    ok, why = _usable(primary)
    if ok:
        return primary, None
    sec_ok, _ = _usable(sec)
    if sec_ok:
        return sec, f"main '{primary}' unavailable ({why}) — using secondary '{sec}'"
    return primary, None


def alternates(current: str) -> list[str]:
    """Runnable backend ids OTHER than ``current`` — the options to offer when the active backend is
    blocked (e.g. an Opus/Claude plan-limit). Used by the cockpit's limit prompt (EU-191). NATIVE is
    always runnable; GLM only when its token is configured. Order: GLM first (the usual alternate)."""
    cur = normalize(current)
    return [bk for bk in (GLM, NATIVE) if bk != cur and available(bk)]


def glm_model() -> str:
    """The GLM model id to send (env-overridable, defaults to ``glm-4.6``)."""
    return (os.environ.get("GLM_MODEL") or _GLM_MODEL_DEFAULT).strip()


def _glm_env() -> dict[str, str]:
    """The ``options.env`` overrides that aim ONE subprocess at z.ai. A fresh dict every call.

    Blanks the Anthropic subscription creds so they are never forwarded to the z.ai endpoint.
    """
    return {
        "ANTHROPIC_BASE_URL": (os.environ.get("GLM_BASE_URL") or _GLM_BASE_URL_DEFAULT).strip(),
        "ANTHROPIC_AUTH_TOKEN": _glm_token(),
        # EU-190: pin the SDK's small/fast (background) model too, else it defaults to a Claude
        # Haiku id and the SDK's background calls would hit z.ai with an id it doesn't serve.
        "ANTHROPIC_SMALL_FAST_MODEL": (os.environ.get("GLM_SMALL_FAST_MODEL")
                                       or _GLM_SMALL_FAST_DEFAULT).strip(),
        "API_TIMEOUT_MS": (os.environ.get("GLM_API_TIMEOUT_MS") or _GLM_TIMEOUT_DEFAULT).strip(),
        # Blank the native-subscription creds for THIS call so they never reach z.ai.
        "ANTHROPIC_API_KEY": "",
        "CLAUDE_CODE_OAUTH_TOKEN": "",
    }


def _registry_backend_env(cfg: dict) -> dict[str, str]:
    """The ``options.env`` overrides for ONE registry-backed subprocess — built exactly like
    :func:`_glm_env` (same keys, same native-credential blanking), just sourced from a
    :meth:`ModelRegistry.get_backend_config` dict instead of the hardcoded GLM env vars."""
    return {
        "ANTHROPIC_BASE_URL": cfg["base_url"],
        "ANTHROPIC_AUTH_TOKEN": cfg["auth_token"],
        "ANTHROPIC_SMALL_FAST_MODEL": cfg.get("small_fast_model_id") or cfg["model_id"],
        # Blank the native-subscription creds for THIS call so they never reach the custom endpoint.
        "ANTHROPIC_API_KEY": "",
        "CLAUDE_CODE_OAUTH_TOKEN": "",
    }


def _apply_registry(options, model_id: str, registry=None) -> str:
    """EU-236: apply a registry-backed id (neither NATIVE nor GLM) to ``options``.

    ``registry`` lets callers/tests inject a :class:`~orchestrator.model_registry.ModelRegistry`
    (or a cfg-anchored/tmp-store one) so this stays hermetic; the default constructs a plain
    ``ModelRegistry()`` anchored to the live ``state/model_registry.json``.

    Fail-closed exactly like the GLM branch: an unknown id, or a known id with no resolvable
    credential, falls back to NATIVE with a warning — never a subprocess aimed at a custom endpoint
    with an empty bearer.
    """
    from .model_registry import ModelRegistry  # deferred: avoid a hard import cycle at module load
    reg = registry if registry is not None else ModelRegistry()
    cfg = reg.get_backend_config(model_id)
    if not cfg or not cfg.get("base_url") or not cfg.get("auth_token"):
        print(f"  ⚠ model-backend: {model_id!r} is not a known/configured registry backend — "
              "falling back to Opus (Claude).", flush=True)
        return NATIVE
    merged = dict(getattr(options, "env", None) or {})
    merged.update(_registry_backend_env(cfg))
    options.env = merged
    options.model = cfg["model_id"]
    return model_id


def apply(options, backend: str | None = None, registry=None) -> str:
    """Apply the selected backend to ONE ``ClaudeAgentOptions`` right before the SDK call.

    Returns the EFFECTIVE backend id actually applied — :data:`NATIVE`, :data:`GLM`, or (EU-236) a
    registry record id — so the caller can label the ledger/audit. For NATIVE this is a pure no-op
    (env untouched, model unchanged).

    Credential minimization (EU-255) is a SEPARATE concern and is applied AT THE SEAM
    (``agent._run_agent_unrouted``, via :func:`secret_strip_overrides`) right after this call — so
    ``apply`` remains a pure model/backend transform and both backends get the same env scrub.

    Fail-closed: GLM with no ``GLM_AUTH_TOKEN``, or a registry id with no resolvable credential,
    falls back to NATIVE with a warning rather than pointing the subprocess at a third-party
    endpoint with an empty bearer.

    ``registry`` (EU-236, optional): a :class:`~orchestrator.model_registry.ModelRegistry` to
    consult for a ``backend`` that is neither a NATIVE nor a GLM alias. When omitted, the run-scoped
    registry pinned by :func:`set_backend` is used (so the seam call ``apply(options)`` resolves
    against THIS run's cfg-anchored store and stays hermetic); with none pinned, a live-anchored
    ``ModelRegistry()`` is the last resort. Ignored for NATIVE/GLM.
    """
    raw = backend if backend is not None else current()
    v = (raw or "").strip().lower()
    if v in _GLM_ALIASES:
        if not _glm_token():
            print("  ⚠ model-backend: GLM selected but GLM_AUTH_TOKEN is not set — falling back to "
                  "Opus (Claude). Set GLM_AUTH_TOKEN in .env to enable GLM.", flush=True)
            return NATIVE
        # Merge the GLM overrides over any existing options.env — a brand-new dict, never shared.
        merged = dict(getattr(options, "env", None) or {})
        merged.update(_glm_env())
        options.env = merged
        options.model = glm_model()
        return GLM
    if v in _NATIVE_ALIASES:
        return NATIVE
    # Not a recognized opus/glm alias — EU-236: try it as a registry backend id. When the caller
    # gave no explicit registry (the seam call apply(options)), resolve against the run-scoped,
    # cfg-anchored registry pinned by set_backend — so runs and tests stay hermetic.
    if registry is None:
        registry = _REGISTRY.get()
    return _apply_registry(options, raw.strip(), registry=registry)


def list_backends(registry=None) -> list[dict]:
    """EU-236: every backend a run could pick — the hardcoded defaults (:data:`NATIVE`,
    :data:`GLM`) PLUS every record in the model registry, as ``{"id", "label"}`` dicts (in that
    order; registry records oldest-first, same order as :meth:`ModelRegistry.list`).

    ``registry`` lets callers inject a hermetic tmp-store registry for tests; the default is a
    plain ``ModelRegistry()`` anchored to the live store. A missing/corrupt
    ``state/model_registry.json`` degrades gracefully (``ModelRegistry._read`` never raises), so
    this always returns at least the two hardcoded defaults.
    """
    out = [
        {"id": NATIVE, "label": "Opus (Claude)"},
        {"id": GLM, "label": "GLM (Z.ai)"},
    ]
    from .model_registry import ModelRegistry  # deferred: avoid a hard import cycle at module load
    reg = registry if registry is not None else ModelRegistry()
    for record in reg.list():
        rid = record.get("id")
        if not rid:
            continue
        out.append({"id": rid, "label": record.get("display_name") or rid})
    return out


def is_glm(cfg=None) -> bool:
    """True if the active backend is GLM.

    Checks the explicit ``cfg.model_backend`` first (robust for ``run_agent_with_fallback``, which
    receives ``cfg``), then the run-scoped contextvar. Either saying GLM is enough.
    """
    if cfg is not None and normalize(getattr(cfg, "model_backend", NATIVE)) == GLM:
        return True
    return current() == GLM


# ── EU-190: GLM configuration validation + a live connection test ────────────────────────────────
# So a bad GLM setup (missing/incorrect token, wrong URL) surfaces a clear, actionable message in
# the cockpit ("what to fix, or re-onboard") instead of failing cryptically mid-run.

def glm_config_issues() -> list[str]:
    """STATIC validation of the GLM env config (no network). Empty list = looks OK to attempt.

    Catches the problems knowable without a call: no token, or a missing/malformed base URL. It is
    NOT a guarantee the backend works — an *incorrect* token only shows up via glm_test_connection.
    """
    issues: list[str] = []
    if not _glm_token():
        issues.append("GLM_AUTH_TOKEN is not set")
    url = (os.environ.get("GLM_BASE_URL") or _GLM_BASE_URL_DEFAULT).strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        issues.append(f"GLM_BASE_URL is missing or not a valid URL ({url or 'empty'!r})")
    return issues


def glm_test_connection(timeout: float = 8.0) -> tuple[bool, str]:
    """LIVE probe of the GLM endpoint with the configured token. Returns ``(ok, human detail)``.

    Maps common failures to actionable messages (bad token, wrong URL, unreachable). Sends a
    1-token ``ping`` so cost is negligible. Never logs or returns the token itself.
    """
    issues = glm_config_issues()
    if issues:
        return False, "; ".join(issues)
    base = (os.environ.get("GLM_BASE_URL") or _GLM_BASE_URL_DEFAULT).strip().rstrip("/")
    model = glm_model()
    try:
        import requests
        resp = requests.post(
            f"{base}/v1/messages",
            headers={
                "authorization": f"Bearer {_glm_token()}",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={"model": model, "max_tokens": 1,
                  "messages": [{"role": "user", "content": "ping"}]},
            timeout=timeout,
        )
    except requests.exceptions.ConnectionError:
        return False, f"cannot reach {base} — check GLM_BASE_URL and your network"
    except requests.exceptions.Timeout:
        return False, f"{base} timed out after {timeout:.0f}s — check GLM_BASE_URL"
    except Exception as exc:  # noqa: BLE001 — a test must never raise into the cockpit
        return False, f"connection test failed ({type(exc).__name__})"
    code = resp.status_code
    if 200 <= code < 300:
        return True, "OK"
    if code in (401, 403):
        return False, "token rejected (HTTP 401/403) — check GLM_AUTH_TOKEN (incorrect or expired)"
    if code == 404:
        return False, f"endpoint not found (HTTP 404) — check GLM_BASE_URL ({base})"
    if code == 400:
        return False, f"request rejected (HTTP 400) — the endpoint is reachable; check GLM_MODEL ({model})"
    return False, f"GLM endpoint returned HTTP {code}"


# ── EU-237: form-supplied backend connection test (the /models "Test connection" button) ─────────

def test_backend_connection(provider: str, base_url: str, model_id: str, api_key: str,
                            timeout: float = 5.0) -> dict:
    """LIVE probe of a FORM-SUPPLIED backend config — the POST /models/test handler behind the
    /models add/edit form's Test-connection button (EU-237). Returns ``{"success", "message"}``
    (a dict, not :func:`glm_test_connection`'s tuple: the route hands it straight back as the
    JSON body).

    Unlike :func:`glm_test_connection` (env-configured GLM), every input arrives from the form:
    nothing is read from env or the registry, nothing is persisted, and the ``api_key`` is used
    solely inside the one probe request — it NEVER appears in the returned message (the EU-234
    boundary: messages carry the base URL / model id / HTTP status, never the credential).

    ``provider`` decides the request shape (the registry's PROVIDERS enum is the source of truth):

    * ``anthropic`` — POST ``<base>/v1/messages`` (the Anthropic messages shape).
    * ``openai``    — POST ``<base>/v1/chat/completions`` (the OpenAI chat shape).

    Both send a 1-max-token ``ping`` so a successful test costs ~nothing, with a short 5s default
    timeout so the cockpit button answers fast. Never raises — any failure maps to an actionable
    ``message`` like the GLM prober's.
    """
    from .model_registry import PROVIDERS  # deferred: avoid a hard import cycle at module load

    def _fail(message: str) -> dict:
        return {"success": False, "message": message}

    prov = (provider or "").strip().lower()
    base = (base_url or "").strip().rstrip("/")
    model = (model_id or "").strip()
    key = (api_key or "").strip()
    # Static validation first (no network) — same spirit as glm_config_issues: catch what is
    # knowable without a call, so a half-filled form gets an instant, targeted answer.
    if prov not in PROVIDERS:
        return _fail(f"unknown provider {prov!r} — expected one of {', '.join(PROVIDERS)}")
    if not re.match(r"^https?://", base, re.IGNORECASE):
        return _fail(f"base URL is missing or not a valid URL ({base or 'empty'!r})")
    if not model:
        return _fail("model ID is empty")
    if not key:
        return _fail("no API key to test — paste one into the form (or save first, then retest)")
    if base.lower().endswith("/v1"):
        # Tolerate a base pasted WITH the /v1 suffix (common for OpenAI-compatible hosts): the
        # paths below carry their own /v1, and <host>/v1/v1/… would 404 a perfectly good config.
        base = base[:-3].rstrip("/")
    payload = {"model": model, "max_tokens": 1,
               "messages": [{"role": "user", "content": "ping"}]}
    if prov == "anthropic":
        url = f"{base}/v1/messages"
        headers = {
            # A real run forwards the key as a bearer (ANTHROPIC_AUTH_TOKEN — see
            # _registry_backend_env), while api.anthropic.com proper authenticates via x-api-key.
            # Send BOTH so the probe matches whichever shape the endpoint expects — same key
            # either way, and neither header ever leaves this request.
            "authorization": f"Bearer {key}",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    else:
        url = f"{base}/v1/chat/completions"
        headers = {"authorization": f"Bearer {key}", "content-type": "application/json"}
    try:
        import requests
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.exceptions.ConnectionError:
        return _fail(f"cannot reach {base} — check the base URL and your network")
    except requests.exceptions.Timeout:
        return _fail(f"{base} timed out after {timeout:.0f}s — check the base URL")
    except Exception as exc:  # noqa: BLE001 — a cockpit probe must never raise into the route
        return _fail(f"connection test failed ({type(exc).__name__})")
    code = resp.status_code
    if 200 <= code < 300:
        return {"success": True, "message": f"OK — {model} answered at {base}"}
    if code in (401, 403):
        return _fail(f"API key rejected (HTTP {code}) — check the key (incorrect or expired)")
    if code == 404:
        return _fail(f"endpoint not found (HTTP 404) — check the base URL ({base})")
    if code == 400:
        return _fail(f"request rejected (HTTP 400) — the endpoint is reachable; "
                     f"check the model ID ({model})")
    return _fail(f"endpoint returned HTTP {code}")
