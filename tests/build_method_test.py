"""One working method, effort scaled per ticket — the squad-mode switch is retired (2026-07-22).

Commander order. The unit used to offer three "squad" formations: full (standard pipeline), elite
(the careful iterative method + an xhigh effort FLOOR), and auto (the sizer routed L/XL → elite).
The audit record showed 44/44 tickets routed to elite and ZERO ever ran full — the choice existed
but was never exercised, and its labels actively confused the operator ("full" sounded bigger than
"elite" while being the lighter one).

The deeper point: the careful method is correct at ANY ticket size. The only thing that made it
expensive on small work was the xhigh floor bundled with it, which overrode `size_ticket()`. With
the floor gone, effort grades itself per ticket — a small ticket runs the same loop cheaply, a
large one runs it thoroughly — so the second mode had nothing left to protect.

Pins:
  1. BUILD_METHOD exists, carries the per-step report shape, and is ALWAYS composed (no condition);
  2. the planner's PLAN_ADDENDUM is likewise unconditional;
  3. the xhigh effort FLOOR is gone — a small ticket keeps the sizer's effort;
  4. a large ticket still reaches xhigh on its own merits (via size_ticket, not a mode);
  5. the retired surfaces are truly gone: no squad_pref module, no /api/squad route, no dropdown.
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

bsrc = pathlib.Path("orchestrator/builder.py").read_text(encoding="utf-8")
psrc = pathlib.Path("orchestrator/planner.py").read_text(encoding="utf-8")

# 1) the method itself
chk("BUILD_METHOD carries the per-step report shape",
    all(k in builder.BUILD_METHOD for k in ("DID:", "CHECKED:", "FOUND:", "NEXT:")))
chk("BUILD_METHOD still demands read-before-edit + per-step verification",
    "READ" in builder.BUILD_METHOD and "never skip the per-step check" in builder.BUILD_METHOD)
chk("the builder composes it UNCONDITIONALLY (no mode test)",
    "+ BUILD_METHOD," in bsrc and "if _elite_squad" not in bsrc)

# 2) the planner addendum
chk("the planner composes PLAN_ADDENDUM unconditionally",
    "+ PLAN_ADDENDUM," in psrc and "_elite" not in psrc)
chk("PLAN_ADDENDUM still demands an ordered step plan",
    "step" in planner.PLAN_ADDENDUM.lower())

# 3+4) effort now comes from the sizer alone
chk("the xhigh effort FLOOR is gone from effort_plan",
    "elite squad floor" not in bsrc and '_TURN_SCALE.get("xhigh"' not in bsrc)

def _t(summary, desc="", ac=None, tid="EU-1"):
    return types.SimpleNamespace(id=tid, summary=summary, description=desc,
                                 acceptance_criteria=ac or [], labels=[], issue_type="Task")

SMALL = _t("Fix a typo in the README")
BIG = _t("Re-architect the tenant isolation layer end-to-end",
         "Concurrently migrate every tenant boundary, with performance and integration coverage.",
         ["isolation holds under concurrency", "no perf regression", "integration tests pass"])

s_size, s_eff, _ = builder.size_ticket(SMALL)
b_size, b_eff, _ = builder.size_ticket(BIG)
chk("a SMALL ticket keeps a cheap effort (no forced xhigh)",
    s_eff not in ("xhigh", "max"), f"{s_size}/{s_eff}")
chk("a LARGE ticket earns high/xhigh from the sizer itself",
    b_eff in ("high", "xhigh", "max"), f"{b_size}/{b_eff}")
chk("…so small and large genuinely differ without any mode",
    s_eff != b_eff, f"small={s_eff} large={b_eff}")

# 5) the retired surfaces are gone for real
chk("orchestrator/squad_pref.py no longer exists",
    not pathlib.Path("orchestrator/squad_pref.py").exists())
chk("no module still imports squad_pref",
    not any("squad_pref" in p.read_text(encoding="utf-8")
            for p in pathlib.Path("orchestrator").glob("*.py")))
chk("the /api/squad route is gone",
    '"/api/squad"' not in pathlib.Path("orchestrator/server.py").read_text(encoding="utf-8"))
chk("the cockpit squad dropdown is gone",
    "_squad_selector" not in pathlib.Path("orchestrator/cockpit_views.py").read_text(encoding="utf-8"))

print("\n========== ONE BUILD METHOD, EFFORT BY SIZE ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
