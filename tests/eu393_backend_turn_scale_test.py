"""EU-393 — backend-aware turn/task budgets.

GLM burns roughly one tool call per turn, so a turn/task-budget ceiling sized for Opus starves a
GLM-routed builder before it clears exploration. `_backend_turn_scale` resolves the EFFECTIVE
per-run builder backend (hybrid-aware, cfg-aware — mirrors `backends.is_glm`) and multiplies
`_BACKEND_TURN_SCALE` (2.0) into both `turns_for` and `budget_for` for a weak (non-NATIVE) backend,
while a NATIVE-resolved run stays byte-identical to today's values.
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


app = AppConfig(name="automatixy", repo_path="/tmp/eu393-fake-repo", base_branch="DEV",
                 protected_branch="MAIN", backlog_backend="none")
cfg = Config(apps=[app], audit_path="/tmp/eu393-fake-audit.jsonl", use_worktree=False)

# ---- 1. NATIVE default: today's values, byte-identical ----
ok("native: turns low/medium = 60", builder.turns_for(cfg, "low") == 60 and builder.turns_for(cfg, "medium") == 60,
   str((builder.turns_for(cfg, "low"), builder.turns_for(cfg, "medium"))))
ok("native: turns high = 96", builder.turns_for(cfg, "high") == 96, str(builder.turns_for(cfg, "high")))
ok("native: turns xhigh/max = 144",
   builder.turns_for(cfg, "xhigh") == 144 and builder.turns_for(cfg, "max") == 144,
   str((builder.turns_for(cfg, "xhigh"), builder.turns_for(cfg, "max"))))
ok("native: budget_for high = 112000", builder.budget_for(cfg, "high") == 112_000, str(builder.budget_for(cfg, "high")))

# ---- 2. GLM run-pinned via backends.set_backend: scales by 2.0 ----
tok = backends.set_backend("glm")
try:
    ok("glm-pinned: turns medium = 120 (60*2.0)", builder.turns_for(cfg, "medium") == 120,
       str(builder.turns_for(cfg, "medium")))
    ok("glm-pinned: turns high = 192 (int(60*1.6*2.0))", builder.turns_for(cfg, "high") == 192,
       str(builder.turns_for(cfg, "high")))
    ok("glm-pinned: strictly more turns than NATIVE for same effort",
       builder.turns_for(cfg, "medium") > 60)
finally:
    backends.reset_backend(tok)

ok("post-reset: back to NATIVE (turns medium = 60)", builder.turns_for(cfg, "medium") == 60,
   str(builder.turns_for(cfg, "medium")))

# ---- 3. Hybrid routing: the resolved per-run builder tag wins, not the fleet default ----
main_tok = backends.set_backend("opus")
hybrid_tok = backends.set_hybrid("glm")
try:
    ok("hybrid: main NATIVE + glm secondary -> builder scales up",
       builder.turns_for(cfg, "medium") == 120, str(builder.turns_for(cfg, "medium")))
finally:
    backends.reset_hybrid(hybrid_tok)
    backends.reset_backend(main_tok)

# cfg says glm, but the pinned hybrid secondary is NATIVE -> the resolved per-run backend (NATIVE)
# wins over the fleet default (cfg.model_backend).
cfg_glm = Config(apps=[app], audit_path="/tmp/eu393-fake-audit.jsonl", use_worktree=False)
cfg_glm.model_backend = "glm"
main_tok2 = backends.set_backend("opus")
hybrid_tok2 = backends.set_hybrid("opus")
try:
    ok("hybrid: NATIVE secondary pinned -> does NOT scale despite cfg.model_backend='glm'",
       builder.turns_for(cfg_glm, "medium") == 60, str(builder.turns_for(cfg_glm, "medium")))
finally:
    backends.reset_hybrid(hybrid_tok2)
    backends.reset_backend(main_tok2)

# ---- 4. cfg-only path (mirrors is_glm): no contextvar pinned, cfg.model_backend='glm' ----
ok("cfg-only: cfg.model_backend='glm' with no pin scales turns",
   builder.turns_for(cfg_glm, "medium") == 120, str(builder.turns_for(cfg_glm, "medium")))

# ---- 5. budget_for scales by the same backend factor; floor + disable behaviour preserved ----
tok2 = backends.set_backend("glm")
try:
    ok("glm-pinned: budget_for medium = 140000 (70000*2.0)", builder.budget_for(cfg, "medium") == 140_000,
       str(builder.budget_for(cfg, "medium")))
    cfg_small = Config(apps=[app], audit_path="/tmp/eu393-fake-audit.jsonl", use_worktree=False)
    cfg_small.builder_task_budget = 5_000
    ok("glm-pinned: 20K SDK floor preserved even scaled", builder.budget_for(cfg_small, "low") == 20_000,
       str(builder.budget_for(cfg_small, "low")))
    cfg_off = Config(apps=[app], audit_path="/tmp/eu393-fake-audit.jsonl", use_worktree=False)
    cfg_off.builder_task_budget = 0
    ok("glm-pinned: 0 still disables (no budget)", builder.budget_for(cfg_off, "high") == 0)
finally:
    backends.reset_backend(tok2)

# ---- 6. Effort-independence of the backend scale (flat multiplier, not per-effort drift) ----
ratio_low = builder.turns_for(cfg, "low")
tok3 = backends.set_backend("glm")
try:
    ratio_low_glm = builder.turns_for(cfg, "low")
finally:
    backends.reset_backend(tok3)
ok("backend scale is a flat multiplier (low: 60 -> 120, exactly x2.0)",
   ratio_low_glm == ratio_low * 2.0, str((ratio_low, ratio_low_glm)))

print(f"{checks - failed}/{checks} passed")
if failed:
    print("RESULT: FAIL")
    sys.exit(1)
print("RESULT: PASS")
