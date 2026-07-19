"""EU-223: per-app model-backend override — run EU on Opus and AUTO on GLM AT THE SAME TIME.

The EU-190 sticky backend was GLOBAL (one model_backend.json for every run), so the EU-103
parallel per-app drains could not each pick their own model. This extends the store to an
OPTIONAL per-app override — ``{"backend": "opus", "apps": {"automatixy": "glm"}}`` — while keeping
full back-compat with the flat legacy shape ``{"backend": "glm"}``.

Fail-first checks for each testable acceptance criterion:
  1. active(cfg, app_name=...) — app-override beats global; no override -> global; no store at
     all -> config.yaml default.
  2. A flat legacy pref file keeps working (global-only behaviour unchanged).
  3. set_active(..., app_name=...) touches ONLY that app's entry; clearing (inherit) removes just
     that key.
  4. server._resolve_run_backend(rcfg) resolves the run's own single app (rcfg.apps[0].name) so
     two concurrent single-app rcfgs built from the SAME per-app store get different providers.
  5. cockpit_views.backend_control(cfg, app_name) renders a per-project selector defaulting to
     'inherit global', posting the app name to /api/model.
"""
import sys
import types
import tempfile
import json
import os
from pathlib import Path

# Stub the Agent SDK before any orchestrator import (house convention).
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        s.__dict__.update(k)

    def __call__(s, *a, **k):
        return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import backend_pref, backends           # noqa: E402
from orchestrator.config import Config, AppConfig          # noqa: E402

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


def _cfg(tmp: Path, app_name: str, mb: str = "opus") -> Config:
    return Config(apps=[AppConfig(name=app_name, repo_path=str(tmp), base_branch="DEV",
                                   protected_branch="MAIN", backlog_backend="none")],
                  audit_path=str(tmp / "state" / "audit.jsonl"), use_worktree=False,
                  model_backend=mb)


# ============ 1) app-override beats global; no override -> global; no store -> config default ==
d1 = Path(tempfile.mkdtemp())
cfg_auto = _cfg(d1, "automatixy", mb="opus")
cfg_eu = _cfg(d1, "Elite-Unit", mb="opus")   # same state dir -> same anchored store

chk("no store at all -> config default (opus)", backend_pref.active(cfg_auto, app_name="automatixy") == "opus")

_store = d1 / "state" / "model_backend.json"
_store.parent.mkdir(parents=True, exist_ok=True)
_store.write_text(json.dumps({"backend": "opus", "apps": {"automatixy": "glm"}}), encoding="utf-8")

chk("active(cfg, app_name='automatixy') -> glm (app-override beats global)",
    backend_pref.active(cfg_auto, app_name="automatixy") == "glm")
chk("active(cfg, app_name='Elite-Unit') -> opus (no override -> global)",
    backend_pref.active(cfg_eu, app_name="Elite-Unit") == "opus")
chk("active(cfg) with no app_name -> global (unchanged)", backend_pref.active(cfg_auto) == "opus")


# ============ 2) flat legacy shape keeps working (global-only) ==================================
d2 = Path(tempfile.mkdtemp())
cfg2 = _cfg(d2, "automatixy", mb="opus")
(d2 / "state").mkdir(parents=True, exist_ok=True)
(d2 / "state" / "model_backend.json").write_text(json.dumps({"backend": "glm"}), encoding="utf-8")

chk("flat legacy file: active(cfg) -> glm", backend_pref.active(cfg2) == "glm")
chk("flat legacy file: active(cfg, app_name=<any>) -> glm too (global-only, unaffected)",
    backend_pref.active(cfg2, app_name="whatever-app") == "glm")

backend_pref.set_active("opus", cfg2)   # no app_name -> global path, still round-trips
chk("set_active('opus', cfg) with no app_name round-trips through the cfg-anchored store",
    backend_pref.get(cfg2) == "opus")


# ============ 3) set_active(..., app_name=...) touches ONLY that app's entry ====================
d3 = Path(tempfile.mkdtemp())
cfg3 = _cfg(d3, "automatixy", mb="opus")
backend_pref.set_active("opus", cfg3)                              # global = opus
backend_pref.set_active("glm", cfg3, app_name="widgetco")           # a sibling app's override, pre-existing
backend_pref.set_active("glm", cfg3, app_name="automatixy")         # the entry under test

_raw = json.loads((d3 / "state" / "model_backend.json").read_text())
chk("set_active(app_name=) leaves the global backend untouched", _raw.get("backend") == "opus")
chk("set_active(app_name=) leaves a DIFFERENT app's override untouched", _raw.get("apps", {}).get("widgetco") == "glm")
chk("set_active(app_name=) writes the target app's override", _raw.get("apps", {}).get("automatixy") == "glm")
chk("get_apps(cfg) reflects both app overrides", backend_pref.get_apps(cfg3) == {"widgetco": "glm", "automatixy": "glm"})

backend_pref.set_active("inherit", cfg3, app_name="automatixy")     # clear -> falls back to global
_raw2 = json.loads((d3 / "state" / "model_backend.json").read_text())
chk("clearing an app's override (inherit) removes just that key", "automatixy" not in _raw2.get("apps", {}))
chk("clearing one app's override leaves the sibling app's override intact", _raw2.get("apps", {}).get("widgetco") == "glm")
chk("after clearing, the app resolves to the global backend", backend_pref.active(cfg3, app_name="automatixy") == "opus")


# ============ 4) server._resolve_run_backend resolves the run's own single app =================
import orchestrator.server as srv   # noqa: E402

_had_token = os.environ.get("GLM_AUTH_TOKEN")
os.environ["GLM_AUTH_TOKEN"] = "test-zai-key-eu223"

d4 = Path(tempfile.mkdtemp())
rcfg_auto = _cfg(d4, "automatixy", mb="opus")
rcfg_eu = _cfg(d4, "Elite-Unit", mb="opus")     # same state dir -> same anchored store
(d4 / "state").mkdir(parents=True, exist_ok=True)
(d4 / "state" / "model_backend.json").write_text(
    json.dumps({"backend": "opus", "apps": {"automatixy": "glm"}}), encoding="utf-8")

err_auto = srv._resolve_run_backend(rcfg_auto)
err_eu = srv._resolve_run_backend(rcfg_eu)
chk("resolve_run_backend(automatixy rcfg) -> glm (its own single app, rcfg.apps[0].name)",
    rcfg_auto.model_backend == "glm" and err_auto is None)
chk("resolve_run_backend(Elite-Unit rcfg) -> opus (falls back to global from the SAME store)",
    rcfg_eu.model_backend == "opus" and err_eu is None)
chk("two concurrent single-app rcfgs from the SAME store resolved to DIFFERENT providers",
    rcfg_auto.model_backend != rcfg_eu.model_backend)

if _had_token is None:
    os.environ.pop("GLM_AUTH_TOKEN", None)
else:
    os.environ["GLM_AUTH_TOKEN"] = _had_token


# ============ 5) backend_control renders a per-project selector, inherit-by-default ==============
from orchestrator import cockpit_views   # noqa: E402

d5 = Path(tempfile.mkdtemp())
cfg5 = _cfg(d5, "automatixy", mb="opus")
backend_pref.set_active("opus", cfg5)

# 2026-07-19 (Commander order): the toolbar now speaks MAIN + SECONDARY — the per-project
# selector no longer renders there (the EU-223 override MECHANICS above stay fully pinned:
# backend_pref per-app storage, active() precedence, /api/model app= route all green).
html_no_override = cockpit_views.backend_control(cfg5, "automatixy")
chk("toolbar no longer renders the per-project selector (moved out 2026-07-19)",
    "This project" not in html_no_override and "Inherit global" not in html_no_override)
chk("Main model selector present",
    "Main model" in html_no_override and "action=/api/model" in html_no_override
    and "Opus (Claude)" in html_no_override)
chk("Secondary selector present (defaults to None)",
    "Secondary" in html_no_override and "name=secondary" in html_no_override
    and "value='none' selected" in html_no_override)

# the per-app override still takes effect for RUNS even though the toolbar hides it
backend_pref.set_active("glm", cfg5, app_name="automatixy")
chk("per-app override mechanics unchanged — active() still honours it",
    backend_pref.active(cfg5, "automatixy") == "glm")


# ============ tally ==============================================================================
passed = sum(1 for _, ok, _ in results if ok)
print("\n========== EU-223 PER-APP MODEL BACKEND QA ==========")
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
