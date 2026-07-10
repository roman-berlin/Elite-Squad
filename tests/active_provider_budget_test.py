"""Budget gates must judge the ACTIVE provider, not whichever provider is emptiest.

Live incident (2026-07-10 18:05): minutes after the sticky backend flipped to Opus, fresh drains
were graceful-stopped by "GLM quota at 97.8% used" — an INACTIVE provider. Two defects:
dual_provider_budget_status hard-coded active_provider="claude" (never consulting the EU-190
sticky pref), and graceful_stop_check/pre_flight_check stopped/held on a bad provider regardless
of whether it was active. An exhausted inactive provider is a can't-switch-there fact, never a
reason to stop the drain that runs on the healthy one.

Pins:
  1. active_provider follows backend_pref (opus default → claude; glm pref → glm).
  2. GLM exhausted + active=claude → NO graceful stop, NO pre-flight hold.
  3. GLM exhausted + active=glm → graceful stop fires (the check still works for the active one).
  4. Claude bad + active=claude → still stops (active-provider stops preserved).
"""
import sys, types, tempfile
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import usage, backend_pref
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

def _cfg(d: Path) -> Config:
    return Config(apps=[AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV",
                                  protected_branch="MAIN", backlog_backend="none")],
                  audit_path=str(d / "state" / "audit.jsonl"), use_worktree=False)

HEALTHY_CLAUDE = {"available": True, "low": False, "over": False, "remaining": 50_000_000,
                  "used": 100_000, "cap": 100_000_000,
                  "limits": [{"key": "weekly", "label": "weekly", "utilization": 0.10}]}
BAD_CLAUDE = {"available": True, "low": True, "over": False, "remaining": 1_000,
              "used": 9_600_000, "cap": 10_000_000,
              "limits": [{"key": "weekly", "label": "weekly", "utilization": 0.96}]}
EXHAUSTED_GLM = {"on": True, "bad": True, "over": False, "pct": 0.978, "remaining": 5_000}
HEALTHY_GLM = {"on": True, "bad": False, "over": False, "pct": 0.05, "remaining": 5_000_000}

_orig_claude, _orig_glm = usage.claude_budget_status_detailed, usage.glm_budget_status
def _stub(claude, glm):
    usage.claude_budget_status_detailed = lambda cfg=None: dict(claude)
    usage.glm_budget_status = lambda cfg=None: dict(glm)

try:
    # ---- 1+2) active=claude (opus default), GLM exhausted → nothing stops ---- #
    d = Path(tempfile.mkdtemp()); cfg = _cfg(d)   # no pref file → default backend (opus/claude)
    _stub(HEALTHY_CLAUDE, EXHAUSTED_GLM)
    st = usage.dual_provider_budget_status(cfg)
    chk("active_provider defaults to claude with no pref", st["active_provider"] == "claude", st["active_provider"])
    g = usage.graceful_stop_check(cfg)
    chk("GLM at 97.8% does NOT graceful-stop an Opus drain (the 18:05 incident)",
        not g["should_stop"], g["reason"])
    p = usage.pre_flight_check(cfg)
    chk("GLM at 97.8% does NOT pre-flight-hold an Opus drain", not p["should_skip"], p["reason"])

    # ---- 3) active=glm, GLM exhausted → stop fires for the ACTIVE provider ---- #
    d2 = Path(tempfile.mkdtemp()); cfg2 = _cfg(d2)
    backend_pref.set_active("glm", cfg2)
    _stub(HEALTHY_CLAUDE, EXHAUSTED_GLM)
    st2 = usage.dual_provider_budget_status(cfg2)
    chk("active_provider follows the sticky pref (glm)", st2["active_provider"] == "glm", st2["active_provider"])
    g2 = usage.graceful_stop_check(cfg2)
    chk("GLM exhausted + active=glm → graceful stop fires", g2["should_stop"], g2["reason"])
    chk("…with a GLM-attributed reason", "glm" in (g2["reason"] or "").lower(), g2["reason"])

    # ---- 4) active=claude, Claude bad → still stops (active stops preserved) ---- #
    d3 = Path(tempfile.mkdtemp()); cfg3 = _cfg(d3)
    _stub(BAD_CLAUDE, HEALTHY_GLM)
    g3 = usage.graceful_stop_check(cfg3)
    chk("Claude bad + active=claude → still stops", g3["should_stop"], g3["reason"])
    p3 = usage.pre_flight_check(cfg3)
    chk("Claude bad + active=claude → pre-flight still holds", p3["should_skip"], p3["reason"])
finally:
    usage.claude_budget_status_detailed = _orig_claude
    usage.glm_budget_status = _orig_glm

print("\n========== ACTIVE-PROVIDER BUDGET GATING QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
