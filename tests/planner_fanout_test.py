"""Planner fan-out + ticket-scaled effort (2026-07-22, Commander order).

Two changes to the Planner, both about spending thought in proportion to the work:

  · EFFORT: it was pinned at "high" for every ticket. size_ticket() already grades the work for
    the Builder, so the Planner now uses the same grade — floored at medium, because planning is
    cheap relative to building and a thin plan costs a whole build pass.

  · FAN-OUT: on a genuinely large ticket the Planner may launch read-only sub-agents (Task) to
    investigate different subsystems in parallel, then synthesise. Verified against the live SDK
    before building this — a probe run launched a sub-agent and got its reply back.

The gate is the important part. A fan-out is the ONE officer call that can multiply its own spend,
so all three conditions must hold: enabled in config, the ticket is L/XL, and the effective backend
is NATIVE (Task is a Claude Code capability; the GLM compat endpoint is not guaranteed to serve it,
and arming it there would burn turns on a tool that never answers).

Pins:
  1. planner effort tracks size_ticket, floored at medium — never "low", never pinned high;
  2. a large ticket gets a strictly higher effort than a small one;
  3. the fan-out addendum caps the agent count and demands independence (distinct subsystems,
     evidence over opinion, reconcile rather than average);
  4. Task is in allowed_tools ONLY behind the gate — the ungated path still disallows Task/Agent;
  5. a dollar ceiling is attached whenever the fan-out is armed;
  6. the prompts no longer claim the retired "2.4x turn budget" (a prompt that lies about its own
     budget skews the Planner's SPLIT decisions).
"""
import pathlib
import sys
import types

_sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
_sdk.__getattr__ = lambda _n: _D
sys.modules["claude_agent_sdk"] = _sdk
_req = types.ModuleType("requests")
_req.Session = lambda *a, **k: None
_req.RequestException = Exception
sys.modules["requests"] = _req
sys.path.insert(0, ".")

from orchestrator import builder, planner

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

psrc = pathlib.Path("orchestrator/planner.py").read_text(encoding="utf-8")


def _t(summary, desc="", ac=None):
    return types.SimpleNamespace(id="EU-1", summary=summary, description=desc,
                                 acceptance_criteria=ac or [], labels=[], issue_type="Task")

SMALL = _t("Fix a typo in the README")
BIG = _t("Re-architect the tenant isolation layer end-to-end",
         "Concurrently migrate every tenant boundary, with performance and integration coverage.",
         ["isolation holds under concurrency", "no perf regression", "integration tests pass"])

# ---- 1+2) effort scales with the ticket, floored at medium -----------------
def _planner_effort(ticket):
    """Mirror of the resolution in planner.plan (kept in step by pin 1c below)."""
    size, sized, _ = builder.size_ticket(ticket)
    return size, (sized if sized in ("high", "xhigh", "max") else "medium")

s_size, s_eff = _planner_effort(SMALL)
b_size, b_eff = _planner_effort(BIG)
chk("(1a) a small ticket never plans at 'low' (medium floor)", s_eff == "medium", f"{s_size}/{s_eff}")
chk("(1b) a large ticket plans at high/xhigh", b_eff in ("high", "xhigh", "max"), f"{b_size}/{b_eff}")
chk("(1c) planner.plan resolves effort from size_ticket, not a constant",
    "from .builder import size_ticket as _size" in psrc and 'effort=_p_effort' in psrc
    and 'effort="high"' not in psrc.split("async def plan")[1])
chk("(2) large plans strictly harder than small", s_eff != b_eff, f"{s_eff} vs {b_eff}")

# ---- 3) the addendum enforces independence, not just volume ----------------
add = planner._fanout_addendum(4)
chk("(3a) the agent cap appears in the instructions", "up to 4" in add)
chk("(3b) it splits by subsystem, not by task", "SPLIT BY SUBSYSTEM" in add)
chk("(3c) it demands evidence over opinion", "EVIDENCE, not opinions" in add)
chk("(3d) it reconciles disagreement instead of averaging it", "RECONCILE, don't average" in add)
chk("(3e) unverified sub-agent claims stay flagged as assumptions",
    "still a guess" in add)
chk("(3f) the Planner's own output contract is unchanged", "output contract is UNCHANGED" in add)

# ---- 4+5) the gate ---------------------------------------------------------
chk("(4a) Task is allowed ONLY on the gated branch",
    '["Read", "Grep", "Glob", "Task"] if _fanout' in psrc)
chk("(4b) the ungated branch still disallows Task AND Agent",
    '"NotebookEdit", "Task", "Agent"]' in psrc)
chk("(4c) the gate requires config + L/XL + NATIVE backend, all three",
    'getattr(cfg, "planner_fanout"' in psrc and '_p_size in ("L", "XL")' in psrc
    and "backends.NATIVE" in psrc)
chk("(4d) the gate fails CLOSED on any error", "_fanout = False" in psrc)
chk("(5) a dollar ceiling is attached when armed",
    "max_budget_usd=" in psrc and "planner_fanout_budget_usd" in psrc)

# ---- 6) the prompts stopped claiming the retired turn budget ---------------
bsrc = pathlib.Path("orchestrator/builder.py").read_text(encoding="utf-8")
chk("(6a) planner prompt no longer claims a 2.4x turn budget", "2.4x turn budget" not in psrc)
chk("(6b) neither prompt still says 'elite squad'",
    "elite squad" not in psrc.lower().split("# ")[0] + planner.PLAN_ADDENDUM.lower()
    and "elite squad" not in builder.BUILD_METHOD.lower())

print("\n========== PLANNER FAN-OUT + SCALED EFFORT ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
