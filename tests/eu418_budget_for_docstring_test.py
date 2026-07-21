"""EU-418 — budget_for docstring accuracy (docs-only).

budget_for's docstring claimed 'registry-routed weak backends get the wider budget', but
`_backend_turn_scale` derives 'weak' from `backends.normalize(effective) != NATIVE`, and
`normalize` maps ANY value that isn't a glm/zai/z.ai alias — including a legitimate custom
registry backend id — to NATIVE. So a registry/unknown backend actually gets scale 1.0 (the
default budget), NOT the widened one. This test pins both the (already-correct) behaviour and
the corrected prose, so the inaccurate clause can't silently creep back.
"""
import sys
import types

sys.path.insert(0, ".")

sdk = types.ModuleType("claude_agent_sdk")


class _Opts:
    def __init__(self, **k):
        self.__dict__.update(k)


sdk.ClaudeAgentOptions = _Opts
sdk.__getattr__ = lambda n: (lambda *a, **k: None)
sys.modules["claude_agent_sdk"] = sdk

from orchestrator import builder, backends  # noqa: E402
from orchestrator.config import Config, AppConfig  # noqa: E402

checks = 0
failed = 0


def ok(name, cond, detail=""):
    global checks, failed
    checks += 1
    if not cond:
        failed += 1
        print(f"  ✗ {name}  {detail}")


app = AppConfig(name="automatixy", repo_path="/tmp/eu418-fake-repo", base_branch="DEV",
                protected_branch="MAIN", backlog_backend="none")
cfg = Config(apps=[app], audit_path="/tmp/eu418-fake-audit.jsonl", use_worktree=False)

# A plausible custom model-registry record id: NOT a glm/zai/z.ai alias, so normalize() -> NATIVE.
REGISTRY_ID = "registry-gpt-5-codex"

# Sanity: normalize really does map a registry id (and unknowns) to NATIVE, never GLM.
ok("normalize: registry id -> NATIVE", backends.normalize(REGISTRY_ID) == backends.NATIVE,
   backends.normalize(REGISTRY_ID))
ok("normalize: arbitrary unknown -> NATIVE", backends.normalize("something-else") == backends.NATIVE,
   backends.normalize("something-else"))

# ---- 1. A registry-routed builder pass gets scale 1.0 -> un-widened budget (criterion 1) ----
tok = backends.set_backend(REGISTRY_ID)
try:
    # The effective builder backend IS the registry id verbatim...
    ok("registry id: current_for_tag('builder') preserves the id",
       backends.current_for_tag("builder") == REGISTRY_ID,
       backends.current_for_tag("builder"))
    # ...but normalize() flattens it to NATIVE, so the scale is the default 1.0.
    ok("registry id: _backend_turn_scale == 1.0 (NOT _BACKEND_TURN_SCALE)",
       builder._backend_turn_scale(cfg) == 1.0, str(builder._backend_turn_scale(cfg)))
    ok("registry id: _backend_turn_scale != _BACKEND_TURN_SCALE",
       builder._backend_turn_scale(cfg) != builder._BACKEND_TURN_SCALE,
       str((builder._backend_turn_scale(cfg), builder._BACKEND_TURN_SCALE)))
    # Therefore budget_for is the base * effort-scale, NOT base * effort-scale * _BACKEND_TURN_SCALE.
    widened = int(cfg.builder_task_budget * builder._TURN_SCALE.get("high", 1.0)
                  * builder._BACKEND_TURN_SCALE)
    ok("registry id: budget_for high is the UN-widened value", builder.budget_for(cfg, "high") != widened,
       str((builder.budget_for(cfg, "high"), widened)))
finally:
    backends.reset_backend(tok)

# Contrast: a GLM-aliased pass DOES get the wider budget (the only path normalize() lifts to GLM).
glm_tok = backends.set_backend("glm")
try:
    ok("glm: _backend_turn_scale == _BACKEND_TURN_SCALE (widened)",
       builder._backend_turn_scale(cfg) == builder._BACKEND_TURN_SCALE,
       str(builder._backend_turn_scale(cfg)))
finally:
    backends.reset_backend(glm_tok)

# ---- 2. budget_for's docstring no longer makes the inaccurate claim (criterion 2) ----
doc = builder.budget_for.__doc__ or ""
ok("docstring: drops 'registry-routed weak backends get the wider budget'",
   "registry-routed weak backends get the wider budget" not in doc, doc)
ok("docstring: still references the backend factor (_BACKEND_TURN_SCALE)",
   ("_BACKEND_TURN_SCALE" in doc or "backend factor" in doc.lower()), doc)

print(f"{checks - failed}/{checks} passed")
if failed:
    print("RESULT: FAIL")
    sys.exit(1)
print("RESULT: PASS")
