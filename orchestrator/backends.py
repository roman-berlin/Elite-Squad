"""EU-189: model-backend selection — Opus (Claude) vs GLM (Z.ai), chosen from the cockpit.

The operator picks a backend per run; every officer in that run executes against it. Because GLM
is reached through Z.ai's **Anthropic-compatible** endpoint, both backends speak the same protocol
— this is plumbing, not request translation.

How it works
------------
A run pins ONE backend via a run-scoped ``contextvars.ContextVar`` set at the top of ``loop.run``
from ``cfg.model_backend``. The single SDK seam (``agent._run_agent_unrouted``) calls
``apply(options)`` just before ``query()``:

* **Opus (native)** — a pure no-op. ``options.env`` is left empty, so the SDK subprocess inherits
  the parent environment (the Max subscription via ``claude login`` / ``CLAUDE_CODE_OAUTH_TOKEN``)
  exactly as today.
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
* **No shared mutable state.** The contextvar is per-run; ``apply`` builds a fresh dict every call.
"""
from __future__ import annotations

import contextvars
import os

# Canonical backend ids.
NATIVE = "opus"   # Anthropic / Claude — the default; env untouched (Max subscription inherited).
GLM = "glm"       # Z.ai GLM via the Anthropic-compatible endpoint.

# Run-scoped selection. Default NATIVE so any SDK call outside a run (meetings, ad-hoc) stays Opus.
_BACKEND: contextvars.ContextVar[str] = contextvars.ContextVar("model_backend", default=NATIVE)

# GLM defaults — overridable via env. The token has NO default: it must be provided or GLM is off.
_GLM_BASE_URL_DEFAULT = "https://api.z.ai/api/anthropic"
_GLM_MODEL_DEFAULT = "glm-4.6"
_GLM_TIMEOUT_DEFAULT = "3000000"


def normalize(value: str | None) -> str:
    """Map a raw selection (cockpit form value / config) to a canonical backend id.

    ``glm`` / ``zai`` / ``z.ai`` → :data:`GLM`; everything else (incl. ``opus`` / ``native`` /
    ``anthropic`` / ``claude`` / ``''`` / typos) → :data:`NATIVE`. Unknown degrades SAFELY to
    NATIVE — never silently to GLM.
    """
    v = (value or "").strip().lower()
    if v in ("glm", "zai", "z.ai", "z-ai"):
        return GLM
    return NATIVE


def set_backend(value: str | None):
    """Pin the backend for the current run context. Returns a token for :func:`reset_backend`."""
    return _BACKEND.set(normalize(value))


def reset_backend(token) -> None:
    """Restore the backend contextvar. Best-effort — a token from another context is ignored."""
    try:
        _BACKEND.reset(token)
    except (ValueError, LookupError):  # pragma: no cover - token from a different context
        pass


def current() -> str:
    """The backend pinned for the current run (:data:`NATIVE` outside any run)."""
    return _BACKEND.get()


def _glm_token() -> str:
    return (os.environ.get("GLM_AUTH_TOKEN") or "").strip()


def available(backend: str) -> bool:
    """True if ``backend`` can actually run. NATIVE is always available; GLM needs its token."""
    if normalize(backend) == GLM:
        return bool(_glm_token())
    return True


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
        "API_TIMEOUT_MS": (os.environ.get("GLM_API_TIMEOUT_MS") or _GLM_TIMEOUT_DEFAULT).strip(),
        # Blank the native-subscription creds for THIS call so they never reach z.ai.
        "ANTHROPIC_API_KEY": "",
        "CLAUDE_CODE_OAUTH_TOKEN": "",
    }


def apply(options, backend: str | None = None) -> str:
    """Apply the selected backend to ONE ``ClaudeAgentOptions`` right before the SDK call.

    Returns the EFFECTIVE backend id actually applied (:data:`NATIVE` or :data:`GLM`) so the caller
    can label the ledger/audit. For NATIVE this is a pure no-op (env untouched, model unchanged).

    Fail-closed: GLM with no ``GLM_AUTH_TOKEN`` falls back to NATIVE with a warning rather than
    pointing the subprocess at z.ai with an empty bearer.
    """
    chosen = normalize(backend) if backend is not None else current()
    if chosen != GLM:
        return NATIVE
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


def is_glm(cfg=None) -> bool:
    """True if the active backend is GLM.

    Checks the explicit ``cfg.model_backend`` first (robust for ``run_agent_with_fallback``, which
    receives ``cfg``), then the run-scoped contextvar. Either saying GLM is enough.
    """
    if cfg is not None and normalize(getattr(cfg, "model_backend", NATIVE)) == GLM:
        return True
    return current() == GLM
