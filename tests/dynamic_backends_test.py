"""EU-236: integrate the model registry into the backend-LOADING path — a custom backend added via
the cockpit becomes RUNNABLE and SELECTABLE, not just a stored record.

Fail-first checks for each testable acceptance criterion:
  1. ModelRegistry.get_backend_config(id) returns {base_url, model_id, auth_token (resolved via
     get_credential_for)} for a stored record; None for an unknown id.
  2. backends.apply(options, backend=<registry_id>) for a record with a resolvable credential sets
     options.model, options.env['ANTHROPIC_BASE_URL']/['ANTHROPIC_AUTH_TOKEN'], blanks the native
     creds, and returns the registry id.
  3. Fail-closed: a registry id with a missing/unresolvable credential leaves options.env untouched
     and returns NATIVE — mirroring the GLM branch (never an empty bearer).
  4. backends.list_backends() returns the hardcoded defaults (opus, glm) PLUS every registry
     record; a backend added to the registry appears; a missing/corrupt store still returns just
     opus+glm, no raise.
  5. Existing hardcoded ids still work with no registry entry: apply(options, 'opus') is a no-op;
     apply(options, 'glm') produces the z.ai env exactly as before.
  6. cockpit_views.backend_control(cfg) renders a selectable <option> for each registry backend.
"""
import sys
import types
import tempfile
import os
from pathlib import Path

# ── Stub the Agent SDK before any orchestrator import (house convention) ──
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        s.__dict__.update(k)

    def __call__(s, *a, **k):
        return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")
# Hermetic GLM env (2026-07-21): the serve-spawned base gate inherits .env, where
# GLM_MODEL=glm-5.2 — the literal glm-4.6 pins below pin the DEFAULT, so clear the
# overrides or this harness passes in a bare shell and fails in production.
import os as _os
_os.environ.pop("GLM_MODEL", None)
_os.environ.pop("GLM_MODEL_MID", None)

from orchestrator import backends                       # noqa: E402
from orchestrator import cockpit_views                  # noqa: E402
from orchestrator.config import AppConfig, Config       # noqa: E402
from orchestrator.model_registry import ModelRegistry   # noqa: E402

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


class _Options:
    """Minimal ``ClaudeAgentOptions`` stand-in — only ``model``/``env`` matter to ``apply()``."""

    def __init__(self, model=None, env=None):
        self.model = model
        self.env = dict(env or {})


def _tmp_registry() -> tuple[ModelRegistry, Path]:
    d = Path(tempfile.mkdtemp())
    store = d / "state" / "model_registry.json"
    return ModelRegistry(path=store), store


def _tmp_cfg(d: Path) -> Config:
    return Config(apps=[AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV",
                                  protected_branch="MAIN", backlog_backend="none")],
                  audit_path=str(d / "state" / "audit.jsonl"), use_worktree=False)


RECORD = {
    "display_name": "My Custom Claude",
    "provider": "anthropic",
    "base_url": "https://api.custom-anthropic.example/v1",
    "model_id": "custom-opus-1",
    "small_fast_model_id": "custom-haiku-1",
    "credential_ref": "vault://custom/my-claude",   # dropped by ModelRegistry's whitelist; harmless
}

# ============ 1) ModelRegistry.get_backend_config() ============ #
reg, _ = _tmp_registry()
rec = reg.add(RECORD)
reg.set_credential(rec["id"], "sk-custom-live-token")

cfg1 = reg.get_backend_config(rec["id"])
chk("get_backend_config: returns a dict for a known id", isinstance(cfg1, dict), cfg1)
chk("get_backend_config: base_url matches the record", cfg1["base_url"] == RECORD["base_url"])
chk("get_backend_config: model_id matches the record", cfg1["model_id"] == RECORD["model_id"])
chk("get_backend_config: auth_token resolved via get_credential_for()",
    cfg1["auth_token"] == "sk-custom-live-token")
chk("get_backend_config: unknown id -> None", reg.get_backend_config("does-not-exist") is None)

# A record with NO credential stored at all -> auth_token is None (not a raised error).
rec_nocred = reg.add({**RECORD, "display_name": "No Cred", "credential_ref": "vault://none/x"})
cfg_nocred = reg.get_backend_config(rec_nocred["id"])
chk("get_backend_config: auth_token is None when nothing is stored under credential_ref",
    cfg_nocred["auth_token"] is None, cfg_nocred)

# ============ 2) backends.apply(): registry id, resolvable credential ============ #
opts = _Options(model="claude-opus-4-8", env={})
eff = backends.apply(opts, backend=rec["id"], registry=reg)
chk("apply(registry id): returns the registry id", eff == rec["id"], eff)
chk("apply(registry id): options.model = record's model_id", opts.model == RECORD["model_id"])
chk("apply(registry id): ANTHROPIC_BASE_URL set from the registry config",
    opts.env.get("ANTHROPIC_BASE_URL") == RECORD["base_url"])
chk("apply(registry id): ANTHROPIC_AUTH_TOKEN set from the resolved credential",
    opts.env.get("ANTHROPIC_AUTH_TOKEN") == "sk-custom-live-token")
chk("apply(registry id): ANTHROPIC_SMALL_FAST_MODEL set from the registry config",
    opts.env.get("ANTHROPIC_SMALL_FAST_MODEL") == RECORD["small_fast_model_id"])
chk("apply(registry id): native ANTHROPIC_API_KEY blanked", opts.env.get("ANTHROPIC_API_KEY") == "")
chk("apply(registry id): native CLAUDE_CODE_OAUTH_TOKEN blanked",
    opts.env.get("CLAUDE_CODE_OAUTH_TOKEN") == "")

# ============ 3) Fail-closed: missing/unresolvable credential -> NATIVE, env untouched ========= #
opts2 = _Options(model="claude-opus-4-8", env={})
eff2 = backends.apply(opts2, backend=rec_nocred["id"], registry=reg)
chk("apply(registry id, no credential): FAILS CLOSED to NATIVE", eff2 == backends.NATIVE, eff2)
chk("apply(registry id, no credential): env left untouched (no partial/empty bearer)",
    opts2.env == {}, opts2.env)
chk("apply(registry id, no credential): model unchanged", opts2.model == "claude-opus-4-8")

# An id that isn't in the registry at all -> same fail-closed path.
opts3 = _Options(model="claude-opus-4-8", env={})
eff3 = backends.apply(opts3, backend="totally-unknown-id", registry=reg)
chk("apply(unknown id): FAILS CLOSED to NATIVE", eff3 == backends.NATIVE, eff3)
chk("apply(unknown id): env left untouched", opts3.env == {})

# ============ 4) list_backends(): hardcoded defaults + every registry record ============ #
ids = [b["id"] for b in backends.list_backends(registry=reg)]
chk("list_backends: includes 'opus'", backends.NATIVE in ids, ids)
chk("list_backends: includes 'glm'", backends.GLM in ids, ids)
chk("list_backends: includes the newly added registry backend", rec["id"] in ids, ids)
chk("list_backends: includes the second registry backend too", rec_nocred["id"] in ids, ids)
chk("list_backends: hardcoded defaults come first, in order",
    ids[0] == backends.NATIVE and ids[1] == backends.GLM, ids[:2])

# Missing store -> still opus+glm, no raise.
reg_missing, store_missing = _tmp_registry()
chk("registry store does not exist yet", not store_missing.exists())
ids_missing = [b["id"] for b in backends.list_backends(registry=reg_missing)]
chk("list_backends: missing store -> just opus+glm", ids_missing == [backends.NATIVE, backends.GLM],
    ids_missing)

# Corrupt store -> same graceful degradation.
reg_corrupt, store_corrupt = _tmp_registry()
store_corrupt.parent.mkdir(parents=True, exist_ok=True)
store_corrupt.write_text("{not valid json", encoding="utf-8")
ids_corrupt = [b["id"] for b in backends.list_backends(registry=reg_corrupt)]
chk("list_backends: corrupt store -> just opus+glm, no raise",
    ids_corrupt == [backends.NATIVE, backends.GLM], ids_corrupt)

# ============ 5) existing hardcoded ids still work (migration path — no registry entry) ======= #
_had_token = os.environ.get("GLM_AUTH_TOKEN")
os.environ.pop("GLM_AUTH_TOKEN", None)
try:
    opts_opus = _Options(model="claude-opus-4-8", env={})
    eff_opus = backends.apply(opts_opus, backend="opus", registry=reg)
    chk("apply('opus'): still a pure no-op", eff_opus == backends.NATIVE and opts_opus.env == {}
        and opts_opus.model == "claude-opus-4-8", (eff_opus, opts_opus.env, opts_opus.model))

    os.environ["GLM_AUTH_TOKEN"] = "zai-test-token-eu236"
    opts_glm = _Options(model="claude-opus-4-8", env={})
    eff_glm = backends.apply(opts_glm, backend="glm", registry=reg)
    chk("apply('glm'): still returns GLM", eff_glm == backends.GLM)
    chk("apply('glm'): still aims at z.ai (unchanged by the registry branch)",
        "z.ai" in opts_glm.env.get("ANTHROPIC_BASE_URL", "").lower())
    chk("apply('glm'): still overrides model to glm-4.6", opts_glm.model == "glm-4.6")
finally:
    if _had_token is not None:
        os.environ["GLM_AUTH_TOKEN"] = _had_token
    else:
        os.environ.pop("GLM_AUTH_TOKEN", None)

# ============ 6) cockpit_views.backend_control(): renders an <option> per registry backend ===== #
_wd = Path(tempfile.mkdtemp())
cfg = _tmp_cfg(_wd)
# Anchor the SAME registry data used above into cfg's state dir so backend_control (which builds
# its own cfg-anchored ModelRegistry) sees it.
cfg_store = Path(cfg.audit_path).resolve().parent / "model_registry.json"
cfg_reg = ModelRegistry(path=cfg_store)
cfg_rec = cfg_reg.add(RECORD)

html_out = cockpit_views.backend_control(cfg)
chk("backend_control: still renders the Opus option", "Opus (Claude)" in html_out)
chk("backend_control: renders an <option> for the registry backend's id",
    f"value='{cfg_rec['id']}'" in html_out, html_out)
chk("backend_control: renders the registry backend's display_name as its label",
    RECORD["display_name"] in html_out, html_out)

# A cfg with an empty/absent registry store must not break rendering (graceful degradation).
_wd2 = Path(tempfile.mkdtemp())
cfg_empty = _tmp_cfg(_wd2)
html_empty = cockpit_views.backend_control(cfg_empty)
chk("backend_control: renders fine with no registry store at all", "Opus (Claude)" in html_empty)

# ============ 7) FULL PRODUCTION PATH: cockpit pref -> resolve -> run pin -> apply(options) ===== #
# The regression the iteration-1 review caught: every seam (backend_pref, _resolve_run_backend,
# loop's set_backend contextvar) flattened a registry id back to 'opus' via backends.normalize(),
# so a custom backend was stored/selectable but NEVER actually reached apply() at the SDK seam. This
# drives the id through the REAL production chain and asserts apply(options) — the exact call
# agent._run_agent_unrouted makes with no explicit backend/registry — applies the registry env+model.
from orchestrator import server as _server            # noqa: E402
from orchestrator import backend_pref as _bp           # noqa: E402

_wd3 = Path(tempfile.mkdtemp())
cfg_full = _tmp_cfg(_wd3)
full_store = Path(cfg_full.audit_path).resolve().parent / "model_registry.json"
full_reg = ModelRegistry(path=full_store)
full_rec = full_reg.add(RECORD)
full_reg.set_credential(full_rec["id"], "sk-full-path-token")

# (a) Persist the registry backend via the cockpit backend_pref API (what POST /api/model does).
_bp.set_active(full_rec["id"], cfg_full)
chk("full-path: backend_pref.get preserves the registry id (not flattened to opus)",
    _bp.get(cfg_full) == full_rec["id"], _bp.get(cfg_full))
chk("full-path: backend_pref.active preserves the registry id",
    _bp.active(cfg_full) == full_rec["id"], _bp.active(cfg_full))

# (b) Resolve the run backend onto cfg (what server does before launching a run).
_server._resolve_run_backend(cfg_full)
chk("full-path: _resolve_run_backend sets cfg.model_backend to the registry id",
    cfg_full.model_backend == full_rec["id"], cfg_full.model_backend)

# (c) Pin it for the run exactly as loop.run does — with the cfg-anchored registry so apply() at the
#     seam stays hermetic — then call apply(options) with NO explicit backend/registry arg.
_tok = backends.set_backend(cfg_full.model_backend, registry=ModelRegistry(cfg_full))
try:
    chk("full-path: current() returns the registry id (contextvar preserves it)",
        backends.current() == full_rec["id"], backends.current())
    opts_full = _Options(model="claude-opus-4-8", env={})
    eff_full = backends.apply(opts_full)   # the exact seam call — no backend, no registry
    chk("full-path: apply(options) applies the registry backend end-to-end", eff_full == full_rec["id"],
        eff_full)
    chk("full-path: apply(options) sets model from the registry record",
        opts_full.model == RECORD["model_id"], opts_full.model)
    chk("full-path: apply(options) sets ANTHROPIC_BASE_URL from the registry record",
        opts_full.env.get("ANTHROPIC_BASE_URL") == RECORD["base_url"], opts_full.env)
    chk("full-path: apply(options) sets ANTHROPIC_AUTH_TOKEN from the resolved credential",
        opts_full.env.get("ANTHROPIC_AUTH_TOKEN") == "sk-full-path-token")
    chk("full-path: apply(options) blanks the native creds", opts_full.env.get("ANTHROPIC_API_KEY") == ""
        and opts_full.env.get("CLAUDE_CODE_OAUTH_TOKEN") == "")
finally:
    backends.reset_backend(_tok)

print("\n========== DYNAMIC BACKENDS QA (EU-236) ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
