"""EU-377 — the Builder pass carries an API-side task_budget so it paces instead of grinding.

Measured 2026-07-17 (state/usage_ledger.jsonl, 319 builder passes with turn data): builder input
cost is QUADRATIC in turns — in_tok ≈ 728·N² + 26,491·N (R²=0.704), a 36x replay multiple; 70% of
the 1,455-tok/turn context growth is TOOL RESULTS (suite stdout read in, then re-sent every
remaining turn). Ceiling runs (≥90 turns) are 10.4% of passes, 27.7% of builder spend ($598), and
24.4% of them fail outright — grind to max_turns, die, retry. A turn cap cannot see intake; the
SDK's task_budget counts exactly it, and the server shows the model a countdown so it wraps up
gracefully. The installed SDK (0.2.101) exposes TaskBudget {total:int} and sends the beta header
itself; it was entirely unused before this.

Pins:
  (1) budget_for scales with effort like turns_for and honours the 20K SDK floor;
  (2) 0 disables (no budget attribute set on the options);
  (3) the build path sets options.task_budget = {"total": budget_for(...)} on Anthropic;
  (4) a GLM-routed pass does NOT get a budget (the backend won't honour the beta);
  (5) the config default arms it at 70K (the lever ships ON — half the measured ~140K
      ceiling-pass intake).
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

from orchestrator import builder  # noqa: E402
from orchestrator.config import Config  # noqa: E402

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


ok("builder exposes budget_for", hasattr(builder, "budget_for"))

cfg = Config(apps=[])
cfg.builder_task_budget = 70_000

# (1) effort scaling mirrors turns_for's _TURN_SCALE
ok("(1) medium effort = base", builder.budget_for(cfg, "medium") == 70_000)
ok("(1b) high effort scales 1.6x", builder.budget_for(cfg, "high") == 112_000,
   str(builder.budget_for(cfg, "high")))
ok("(1c) max effort scales 2.4x", builder.budget_for(cfg, "max") == 168_000)

cfg.builder_task_budget = 5_000
ok("(1d) the 20K SDK floor is honoured", builder.budget_for(cfg, "low") == 20_000)

# (2) 0 disables
cfg.builder_task_budget = 0
ok("(2) 0 disables the budget", builder.budget_for(cfg, "high") == 0)

# (5) the shipped default arms it
ok("(5) config default is 70K (the lever ships ON)",
   Config(apps=[]).builder_task_budget == 70_000)

# (3)+(4) the wiring in build(): source pins (driving a full build needs the SDK loop)
src = Path("orchestrator/builder.py").read_text()
ok("(3) the build path sets options.task_budget from budget_for",
   'options.task_budget = {"total": _budget}' in src and "_budget = budget_for(cfg, eff)" in src)
ok("(4) GLM-routed passes are excluded (won't honour the beta header)",
   "if not _backends.is_glm(cfg):" in src.split("_budget = budget_for", 1)[1][:600],
   "a GLM pass with a budget the backend ignores would silently change nothing — gate it")

print(f"\n{checks}/{checks} passed")
