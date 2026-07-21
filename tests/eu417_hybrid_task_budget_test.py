"""EU-417 — is_glm(cfg) is blind to hybrid-secondary/tag routing, so a hybrid-mode build pass
(main=opus, secondary=glm) still gets options.task_budget attached even though its EFFECTIVE
backend is GLM (via current_for_tag('builder')). z.ai won't honour the beta header the SDK sends
with task_budget, so a routed-GLM pass must keep today's turn-cap-only behaviour.

This harness pins the tag-aware gate (is_glm_for_tag) AND the builder budget-gate logic that
consumes it, across the four routing modes:
  (1) hybrid (main=opus, secondary=glm) -> GLM via current_for_tag('builder') -> budget OFF;
  (2) single/non-hybrid main=opus, no GLM cfg -> budget ON (no regression to EU-377 Anthropic);
  (3) fleet-default GLM (cfg.model_backend=glm, no hybrid) -> budget OFF (retained is_glm path);
  (4) reverse-hybrid (main=glm, secondary=opus) -> opus via current_for_tag('builder') -> budget ON.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, ".")

sdk = types.ModuleType("claude_agent_sdk")


class _Opts:
    def __init__(self, **k):
        self.__dict__.update(k)


sdk.ClaudeAgentOptions = _Opts
sdk.__getattr__ = lambda n: (lambda *a, **k: None)
sys.modules["claude_agent_sdk"] = sdk

from orchestrator import backends, builder  # noqa: E402
from orchestrator.config import Config  # noqa: E402

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


# The helper exists.
ok("backends exposes is_glm_for_tag", hasattr(backends, "is_glm_for_tag"))

# (1) hybrid mode: main=opus, secondary=glm -> the builder's EFFECTIVE backend is GLM.
tok_bk = backends.set_backend("opus")
tok_hy = backends.set_hybrid("glm")
try:
    ok("(1a) hybrid: current_for_tag('builder') normalizes to GLM",
       backends.normalize(backends.current_for_tag("builder")) == backends.GLM)
    cfg = Config(apps=[])  # model_backend defaults to opus (NATIVE)
    ok("(1b) hybrid: is_glm_for_tag('builder', cfg) is True (tag-resolved GLM wins)",
       backends.is_glm_for_tag("builder", cfg) is True,
       "main is opus + cfg.model_backend is opus, so only the tag-aware path can see the GLM secondary")
    # Replicate the EXACT builder.py budget gate and confirm task_budget is NOT attached.
    cfg.builder_task_budget = 70_000
    options = _Opts()
    _budget = builder.budget_for(cfg, "medium")
    if _budget:
        if not backends.is_glm_for_tag("builder", cfg):
            options.task_budget = {"total": _budget}
    ok("(1c) hybrid: a GLM-routed build pass gets NO task_budget",
       not hasattr(options, "task_budget"),
       f"got task_budget={getattr(options, 'task_budget', None)!r}")
finally:
    backends.reset_hybrid(tok_hy)
    backends.reset_backend(tok_bk)

# (2) single/non-hybrid: main=opus, no GLM anywhere -> budget ON (EU-377 Anthropic behaviour).
ok("(2a) non-hybrid: no hybrid secondary pinned", backends.hybrid_secondary() is None)
cfg2 = Config(apps=[])  # model_backend opus
ok("(2b) non-hybrid: is_glm_for_tag('builder', cfg) is False",
   backends.is_glm_for_tag("builder", cfg2) is False)
cfg2.builder_task_budget = 70_000
options2 = _Opts()
_budget2 = builder.budget_for(cfg2, "medium")
if _budget2:
    if not backends.is_glm_for_tag("builder", cfg2):
        options2.task_budget = {"total": _budget2}
ok("(2c) non-hybrid: an Anthropic build pass keeps task_budget attached",
   getattr(options2, "task_budget", None) == {"total": 70_000})

# (3) fleet-default GLM: cfg.model_backend=glm, no hybrid -> budget OFF (retained is_glm(cfg) path).
tok_bk3 = backends.set_backend("opus")
try:
    cfg3 = Config(apps=[])
    cfg3.model_backend = "glm"
    cfg3.builder_task_budget = 70_000
    ok("(3a) fleet-GLM: is_glm_for_tag('builder', cfg) is True via retained is_glm(cfg)",
       backends.is_glm_for_tag("builder", cfg3) is True)
    options3 = _Opts()
    _budget3 = builder.budget_for(cfg3, "medium")
    if _budget3:
        if not backends.is_glm_for_tag("builder", cfg3):
            options3.task_budget = {"total": _budget3}
    ok("(3b) fleet-GLM: a GLM-default build pass gets NO task_budget",
       not hasattr(options3, "task_budget"))
finally:
    backends.reset_backend(tok_bk3)

# (4) reverse hybrid: main=glm (fleet default / run-pinned), secondary=opus -> the builder's
# EFFECTIVE backend is opus (the non-GLM secondary), so is_glm_for_tag is False and task_budget
# MUST attach. This is the case the iteration-1 is_glm(cfg)-first rewrite got wrong: is_glm(cfg)
# saw the fleet-default glm and short-circuited True before the tag resolution could override it.
tok_bk4 = backends.set_backend("glm")
tok_hy4 = backends.set_hybrid("opus")
try:
    ok("(4a) reverse-hybrid: current_for_tag('builder') resolves to opus (NATIVE)",
       backends.normalize(backends.current_for_tag("builder")) == backends.NATIVE)
    cfg4 = Config(apps=[])
    cfg4.model_backend = "glm"  # fleet default / run-pinned main is GLM
    ok("(4b) reverse-hybrid: is_glm_for_tag('builder', cfg) is False (non-GLM secondary wins)",
       backends.is_glm_for_tag("builder", cfg4) is False,
       "main/cfg is glm but the builder routes to the opus secondary, so the per-tag result must be False")
    cfg4.builder_task_budget = 70_000
    options4 = _Opts()
    _budget4 = builder.budget_for(cfg4, "medium")
    if _budget4:
        if not backends.is_glm_for_tag("builder", cfg4):
            options4.task_budget = {"total": _budget4}
    ok("(4c) reverse-hybrid: an opus-routed build pass keeps task_budget attached",
       getattr(options4, "task_budget", None) == {"total": 70_000},
       f"got task_budget={getattr(options4, 'task_budget', None)!r}")
finally:
    backends.reset_hybrid(tok_hy4)
    backends.reset_backend(tok_bk4)

# (5) the builder source gates on the tag-aware helper, not the blind is_glm(cfg) literal.
src = Path("orchestrator/builder.py").read_text()
ok("(5) builder.py gates task_budget on is_glm_for_tag('builder', cfg)",
   "if not _backends.is_glm_for_tag(\"builder\", cfg):" in src
   or "if not _backends.is_glm_for_tag('builder', cfg):" in src)
ok("(5b) builder.py no longer uses the tag-blind is_glm(cfg) at the budget gate",
   "if not _backends.is_glm(cfg):" not in src)

print(f"\n{checks}/{checks} passed")
