"""EU-52: non-Builder officers must route through the economical model ladder (models.for_officer)
instead of hardcoding cfg.reviewer_model / cfg.builder_model.

Covers both wiring shapes:
  • recon-caller officers (scout/provost/quartermaster/pm/scrum) that pass `model=` into
    recon.run_officer, and
  • ClaudeAgentOptions officers (provost.gate, adjutant, drillmaster, squad, test_engineer).

Asserts the contract, not a specific model id:
  - auto_model OFF -> the configured ceiling, unchanged (pins/off-switch honored).
  - auto_model ON  -> goes through the ladder, so a tight budget downgrades below the ceiling.
"""
import asyncio, sys, tempfile, types
from pathlib import Path

# Stub the SDK so ClaudeAgentOptions just stores kwargs (we read .model back).
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import models, scout, provost, test_engineer, recon
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))

class RR:
    def __init__(s, t): s.final, s.text, s.is_error, s.cost_usd, s.num_turns, s.tools, s.provider, s.model_version = t, t, False, 0.0, 1, [], "Anthropic", "claude-sonnet-4-6"

d = Path(tempfile.mkdtemp())
(d / "audit.jsonl").write_text("")
def make_cfg(auto):
    return Config(apps=[AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV",
                                  protected_branch="MAIN", backlog_backend="none")],
                  audit_path=str(d / "audit.jsonl"), use_worktree=False, auto_model=auto)

CEILING = make_cfg(True).reviewer_model     # configured Opus ceiling

# Force a tight day so the ladder visibly steps *below* the ceiling when it's engaged.
models._budget_pct = lambda cfg: 1.0

# --- scout.recon: a recon-caller that passes model= into recon.run_officer --------------------- #
captured = {}
async def fake_run_officer(**kw):
    captured["model"] = kw["model"]; return "ok"
recon.run_officer = fake_run_officer

captured.clear(); asyncio.run(scout.recon(make_cfg(False), "automatixy"))
check("scout: auto OFF -> configured ceiling unchanged", captured["model"] == CEILING, captured.get("model"))

captured.clear(); asyncio.run(scout.recon(make_cfg(True), "automatixy"))
check("scout: auto ON -> routed through ladder (tight budget downgrades below ceiling)",
      captured["model"] != CEILING and captured["model"] in models.LADDER, captured.get("model"))

# --- provost.gate: a ClaudeAgentOptions officer (security gate, reviewer ceiling) -------------- #
gate_model = {}
async def fake_agent_provost(prompt, options, tag=""):
    gate_model["m"] = getattr(options, "model", None); return RR("SECURITY GATE: PASS")
provost.run_agent = fake_agent_provost
app = make_cfg(True).app("automatixy")
app.workdir = str(d)

gate_model.clear(); asyncio.run(provost.gate(make_cfg(False), app, "diff"))
check("provost.gate: auto OFF -> configured ceiling unchanged", gate_model["m"] == CEILING, gate_model.get("m"))

gate_model.clear(); asyncio.run(provost.gate(make_cfg(True), app, "diff"))
check("provost.gate: auto ON -> routed through ladder",
      gate_model["m"] != CEILING and gate_model["m"] in models.LADDER, gate_model.get("m"))

# --- test_engineer: a code-writing officer under the *builder* ceiling -------------------------- #
te_model = {}
async def fake_agent_te(prompt, options, tag=""):
    te_model["m"] = getattr(options, "model", None); return RR("COVERAGE: ok")
test_engineer.run_agent = fake_agent_te
tk = Ticket(id="AUTO-1", key="AUTO-1", summary="x", description="y", acceptance_criteria=["a"])

te_model.clear(); asyncio.run(test_engineer.ensure_coverage(tk, app, make_cfg(False)))
check("test_engineer: auto OFF -> builder ceiling unchanged",
      te_model["m"] == make_cfg(False).builder_model, te_model.get("m"))

te_model.clear(); asyncio.run(test_engineer.ensure_coverage(tk, app, make_cfg(True)))
check("test_engineer: auto ON -> routed through ladder (floor stays Sonnet for code)",
      te_model["m"] in models.LADDER and te_model["m"] != models.HAIKU, te_model.get("m"))

print("\n================ OFFICER-LADDER QA (EU-52) ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
