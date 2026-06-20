"""Auto model optimizer QA: cheapest-that-fits, never above the configured ceiling, never below the
Sonnet floor for code, conserves when the daily budget is tight, and is a pure no-op when off."""
import sys, types, tempfile
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import models as M, usage
from orchestrator.config import Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

ns = types.SimpleNamespace
def ticket(**kw):
    base = dict(id="AUTO-1", key="AUTO-1", summary="", description="", acceptance_criteria=[], labels=[],
                issue_type="Task")
    base.update(kw)
    return ns(**base)

SMALL = ticket(summary="fix a typo in the footer", description="one-word copy fix",
               acceptance_criteria=["correct the spelling"])
BIG = ticket(summary="migrate auth schema and refactor RLS across services",
             description="big multi-service migration touching security and database " * 8,
             acceptance_criteria=[f"criterion {i}" for i in range(9)],
             labels=["migration"])

# --- tier helpers ---
chk("tier_of haiku/sonnet/opus", (M.tier_of(M.HAIKU), M.tier_of(M.SONNET), M.tier_of(M.OPUS)) == (0, 1, 2))
chk("tier_of unknown -> top (never silently downgrade an override)", M.tier_of("some-future-model") == 2)
chk("model_at clamps", M.model_at(-3) == M.HAIKU and M.model_at(9) == M.OPUS)

# --- optimize: ceiling / floor / size / budget ---
chk("never exceeds ceiling (sonnet ceiling, XL task stays sonnet)",
    M.optimize(M.SONNET, size="XL", budget_pct=0.0, floor_tier=1)[0] == M.SONNET)
chk("small task under opus ceiling -> sonnet", M.optimize(M.OPUS, size="XS", floor_tier=1)[0] == M.SONNET)
chk("big task under opus ceiling -> opus", M.optimize(M.OPUS, size="XL", floor_tier=1)[0] == M.OPUS)
chk("budget tight (>=80%) drops a tier", M.optimize(M.OPUS, size="XL", budget_pct=0.85, floor_tier=1)[0] == M.SONNET)
chk("budget over (>=100%) drops two — but floor holds at sonnet",
    M.optimize(M.OPUS, size="XL", budget_pct=1.2, floor_tier=1)[0] == M.SONNET)
chk("floor 0 lets a discussion drop to haiku when over budget",
    M.optimize(M.SONNET, size="XS", budget_pct=1.2, floor_tier=0)[0] == M.HAIKU)
chk("no size signal -> uses effort", M.optimize(M.OPUS, effort="high", floor_tier=1)[0] == M.OPUS
    and M.optimize(M.OPUS, effort="low", floor_tier=1)[0] == M.SONNET)

# --- for_builder: off = fixed; on = sized, floored at sonnet ---
off = Config(apps=[], audit_path="/tmp/x.jsonl", auto_model=False, builder_model=M.OPUS)
chk("auto OFF -> builder uses the exact configured model (no behaviour change)",
    M.for_builder(off, BIG, "high")[0] == M.OPUS and M.for_builder(off, SMALL, "low")[0] == M.OPUS)

on = Config(apps=[], audit_path="/tmp/x.jsonl", auto_model=True, builder_model=M.OPUS)
mb_small, why_small = M.for_builder(on, SMALL, "low")
mb_big, _ = M.for_builder(on, BIG, "high")
chk("auto ON, healthy budget -> small ticket STILL builds on Opus (Opus is best for code)",
    mb_small == M.OPUS, mb_small)
chk("auto ON, healthy budget -> big ticket builds on Opus", mb_big == M.OPUS, mb_big)
chk("builder floor is Sonnet, never Haiku for code", mb_small != M.HAIKU)
chk("reason names the model", "opus" in why_small.lower())

# conserve_only policy directly: Opus normally, Sonnet only under budget pressure
chk("conserve_only: Opus on a healthy budget",
    M.optimize(M.OPUS, budget_pct=0.1, floor_tier=1, conserve_only=True)[0] == M.OPUS)
chk("conserve_only: drops to Sonnet only when budget tight",
    M.optimize(M.OPUS, budget_pct=0.85, floor_tier=1, conserve_only=True)[0] == M.SONNET)

# --- for_reviewer: same policy — Opus for code unless budget tight ---
chk("auto OFF reviewer fixed", M.for_reviewer(off, "x" * 50000)[0] == M.OPUS)
chk("auto ON, healthy budget -> reviewer stays Opus (small diff)", M.for_reviewer(on, "tiny diff")[0] == M.OPUS)
chk("auto ON, healthy budget -> reviewer Opus (huge diff)", M.for_reviewer(on, "x" * 20000)[0] == M.OPUS)

# --- budget signal flows from the real ledger ---
tmp = Path(tempfile.mkdtemp()); led = tmp / "audit.jsonl"
usage.configure(str(led))
bcfg = Config(apps=[], audit_path=str(led), auto_model=True, builder_model=M.OPUS,
              daily_token_budget=1000, budget_alert_pct=0.8)
usage.record("claude-opus-4-8", 900, 0, 0.0, "builder")   # 900/1000 today -> 90% -> tight
mb_tight, why_tight = M.for_builder(bcfg, BIG, "high")
chk("a tight live budget downgrades even a big build to Sonnet", mb_tight == M.SONNET, why_tight)
usage._PATH = None  # reset

print("\n============== AUTO MODEL OPTIMIZER QA ==============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
