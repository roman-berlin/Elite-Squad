"""Per-task submodel + effort inside every backend (2026-07-19, Commander order).

What already existed (pinned here as the baseline): models.py's auto ladder — Haiku→Sonnet→Opus
by ticket size/effort/budget, never above the configured ceiling, never below Sonnet for code —
on by default (auto_model). What this adds:

  (1) the DEEP tier: models.for_planner picks cfg.deep_model (default claude-fable-5) at MAX
      effort for deep-architecture tasks — L/XL size, effort-max/ultracode override, or an
      architecture/epic label — and the normal Opus-ceiling pick + high effort for a routine PRD;
  (2) is_deep_task classification (labels / issue_type / effort override / size);
  (3) cfg.deep_model = "" disables the deep tier (routine pick even for deep tasks);
  (4) the Planner AND the Architect route through for_planner (source pins);
  (5) GLM tier parallelism: backends.glm_model_for maps a Sonnet/Haiku-class request to
      GLM_MODEL_MID (the "parallel to Sonnet" model) and Opus/deep-class to the top GLM_MODEL;
      with GLM_MODEL_MID unset every tier keeps the top model — the old behaviour, byte-for-byte;
  (6) apply() feeds the officer's REQUESTED model class into that pick (source pin)."""
import os
import sys
import tempfile
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import backends, models
from orchestrator.config import Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


def tk(summary="Fix the button", desc="small", labels=None, itype="Task", ac=None):
    return types.SimpleNamespace(id="T-1", key="T-1", summary=summary, description=desc,
                                 labels=labels or [], issue_type=itype,
                                 acceptance_criteria=ac or [], url="")


tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[], audit_path=str(tmp / "audit.jsonl"))

# ── (2) deep classification ──
chk("(2a) an architecture label is deep", models.is_deep_task(tk(labels=["architecture"])))
chk("(2b) an Epic is deep", models.is_deep_task(tk(itype="Epic")))
chk("(2c) an effort-max override is deep", models.is_deep_task(tk(), effort="max"))
chk("(2d) 'ultracode' counts as deep effort", models.is_deep_task(tk(), effort="ultracode"))
chk("(2e) a small routine ticket is NOT deep", not models.is_deep_task(tk()))

# ── (1) the planner pick ──
m, e, r = models.for_planner(cfg, tk(labels=["architecture"]))
chk("(1a) deep architecture → the deep model at max effort",
    m == "claude-fable-5" and e == "max", f"{m} {e}")
m, e, r = models.for_planner(cfg, tk())
chk("(1b) a routine PRD stays on the Opus-ceiling pick at high effort",
    "fable" not in m and e == "high", f"{m} {e}")

# ── (3) disabling the deep tier ──
cfg.deep_model = ""
m, e, r = models.for_planner(cfg, tk(labels=["architecture"]))
chk("(3) deep_model='' disables the deep tier", "fable" not in m, m)
cfg.deep_model = "claude-fable-5"

# ── (4) the officers route through for_planner ──
psrc = Path("orchestrator/planner.py").read_text(encoding="utf-8")
asrc = Path("orchestrator/architect.py").read_text(encoding="utf-8")
chk("(4a) the Planner uses the task-aware pick", "models.for_planner(cfg, ticket" in psrc)
chk("(4b) the Architect uses the task-aware pick", "models.for_planner(cfg, ticket" in asrc)

# ── (5) GLM tier parallelism ──
_env_saved = {k: os.environ.get(k) for k in ("GLM_MODEL", "GLM_MODEL_MID")}
try:
    os.environ.pop("GLM_MODEL", None)
    os.environ.pop("GLM_MODEL_MID", None)
    chk("(5a) MID unset → every class keeps the top GLM (old behaviour)",
        backends.glm_model_for("claude-sonnet-5") == backends.glm_model()
        and backends.glm_model_for("claude-opus-4-8") == backends.glm_model())
    os.environ["GLM_MODEL_MID"] = "glm-4.5-air"
    chk("(5b) a Sonnet-class request maps to the mid GLM",
        backends.glm_model_for("claude-sonnet-5") == "glm-4.5-air")
    chk("(5c) a Haiku-class request maps to the mid GLM too",
        backends.glm_model_for(models.HAIKU) == "glm-4.5-air")
    chk("(5d) an Opus-class request keeps the top GLM",
        backends.glm_model_for("claude-opus-4-8") == backends.glm_model())
    chk("(5e) the deep model keeps the top GLM (unknown → top, never downgraded)",
        backends.glm_model_for("claude-fable-5") == backends.glm_model())
finally:
    for k, v in _env_saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

# ── (6) apply() feeds the requested class in ──
bsrc = Path("orchestrator/backends.py").read_text(encoding="utf-8")
chk("(6) apply() picks the GLM variant from the requested model's class",
    'glm_model_for(getattr(options, "model", None))' in bsrc)

# ── baseline sanity: the existing single-model auto ladder is intact ──
chk("(7) the auto ladder is untouched (Haiku→Sonnet→Opus)",
    models.LADDER == [models.HAIKU, models.SONNET, models.OPUS])
chk("(7b) auto_model stays ON by default", Config(apps=[], audit_path=str(tmp / "a.jsonl")).auto_model)

print("\n========== PER-TASK MODEL TIERING QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
