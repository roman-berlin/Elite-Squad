"""Squad modes (2026-07-19 Commander order): full / elite / auto — the small elite squad.

Pins:
  (1) store: default 'full'; set/get round-trip for all three; unknown values ignored;
  (2) resolve_for_ticket: pinned full/elite pass through; auto routes by the sizer —
      a small ticket → full, a big (L/XL) ticket → elite;
  (3) DEFAULT SAFETY: with no squad file, nothing changes — _elite_squad is False and
      effort_plan's reason carries no elite mention (the existing pipeline is byte-identical);
  (4) elite Engineer: effort_plan floors the turn budget at xhigh (2.4x turns via turns_for),
      already-max tickets are not touched, and the reason says why;
  (5) elite prompt layers exist and are wired: builder composes ELITE_METHOD (with the per-step
      report shape + read-before-edit + never-skip-checks rules) only under _elite_squad; the
      planner composes ELITE_PLAN_ADDENDUM (ordered steps + assumptions up front) the same way;
  (6) the cockpit renders the Squad selector (three options) and POST /api/squad persists it;
  (7) loop audits `squad_selected` for elite attempts (source pin at the _attempt head).
"""
import sys
import tempfile
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        s.__dict__.update(k)

    def __call__(s, *a, **k):
        return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import builder, squad_pref
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


def _cfg():
    tmp = Path(tempfile.mkdtemp())
    return Config(apps=[AppConfig(name="Elite-Unit", repo_path=".", base_branch="dev",
                                  protected_branch="main", backlog_backend="none")],
                  audit_path=str(tmp / "audit.jsonl"), use_worktree=False)


SMALL = Ticket(id="EU-1", key="EU-1", summary="fix typo", description="tiny", app="Elite-Unit")
BIG = Ticket(id="EU-2", key="EU-2", summary="Rework the data layer architecture",
             description=("Large migration and refactor across the persistence architecture. " * 30),
             acceptance_criteria=["a", "b", "c", "d", "e"], app="Elite-Unit")

# (1) store
cfg = _cfg()
chk("(1a) default mode is 'full'", squad_pref.get_mode(cfg) == "full")
for m in ("elite", "auto", "full"):
    squad_pref.set_mode(m, cfg)
    chk(f"(1b) round-trip '{m}'", squad_pref.get_mode(cfg) == m)
squad_pref.set_mode("banana", cfg)
chk("(1c) unknown value ignored", squad_pref.get_mode(cfg) == "full")

# (2) resolution
squad_pref.set_mode("elite", cfg)
chk("(2a) pinned elite wins for any ticket", squad_pref.resolve_for_ticket(cfg, SMALL)[0] == "elite")
squad_pref.set_mode("full", cfg)
chk("(2b) pinned full wins for any ticket", squad_pref.resolve_for_ticket(cfg, BIG)[0] == "full")
squad_pref.set_mode("auto", cfg)
chk("(2c) auto: small ticket → full", squad_pref.resolve_for_ticket(cfg, SMALL)[0] == "full",
    squad_pref.resolve_for_ticket(cfg, SMALL))
chk("(2d) auto: big ticket → elite", squad_pref.resolve_for_ticket(cfg, BIG)[0] == "elite",
    (builder.size_ticket(BIG), squad_pref.resolve_for_ticket(cfg, BIG)))

# (3) default safety — a FRESH cfg (no squad file) changes nothing
cfg2 = _cfg()
chk("(3a) no squad file → _elite_squad False", not builder._elite_squad(cfg2, BIG))
eff, why = builder.effort_plan(cfg2, 1, BIG)
chk("(3b) no squad file → effort_plan untouched (no elite mention)", "elite" not in why, why)

# (4) elite effort floor
squad_pref.set_mode("elite", cfg)
eff_small, why_small = builder.effort_plan(cfg, 1, SMALL)
chk("(4a) elite floors a small ticket's effort at xhigh", eff_small == "xhigh", (eff_small, why_small))
chk("(4b) the reason names the floor", "elite squad floor" in why_small, why_small)
chk("(4c) xhigh grants the 2.4x turn budget",
    builder.turns_for(cfg, "xhigh") == int(60 * 2.4), builder.turns_for(cfg, "xhigh"))
pinned = Ticket(id="EU-3", key="EU-3", summary="s [effort:max]", description="d", app="Elite-Unit")
eff_max, why_max = builder.effort_plan(cfg, 1, pinned)
chk("(4d) an already-max ticket is not lowered", eff_max == "max", (eff_max, why_max))

# (5) prompt layers — content + composition
chk("(5a) ELITE_METHOD demands the per-step report shape",
    "DID:" in builder.ELITE_METHOD and "CHECKED:" in builder.ELITE_METHOD
    and "NEXT:" in builder.ELITE_METHOD)
chk("(5b) …and read-before-edit + never-skip-checks",
    "READ" in builder.ELITE_METHOD and "never skip the per-step check" in builder.ELITE_METHOD)
bsrc = Path("orchestrator/builder.py").read_text(encoding="utf-8")
chk("(5c) builder composes ELITE_METHOD only under _elite_squad",
    "+ (ELITE_METHOD if _elite_squad(cfg, req.ticket) else \"\")" in bsrc)
from orchestrator import planner
chk("(5d) planner addendum: ordered steps + assumptions up front",
    "ORDERED list" in planner.ELITE_PLAN_ADDENDUM
    and "assumption" in planner.ELITE_PLAN_ADDENDUM.lower())
psrc = Path("orchestrator/planner.py").read_text(encoding="utf-8")
chk("(5e) planner composes the addendum only when elite",
    "ELITE_PLAN_ADDENDUM if _elite else" in psrc)

# (6) cockpit + endpoint
from orchestrator import cockpit_views
squad_pref.set_mode("auto", cfg)
htmlout = cockpit_views.backend_control(cfg)
chk("(6a) the Squad selector renders with all three options",
    "Squad" in htmlout and "Full squad" in htmlout and "Elite squad" in htmlout
    and "Auto" in htmlout)
chk("(6b) the active mode is selected", "value='auto' selected" in htmlout, htmlout[-400:])
ssrc = Path("orchestrator/server.py").read_text(encoding="utf-8")
chk("(6c) POST /api/squad persists via squad_pref.set_mode",
    '@app.post("/api/squad")' in ssrc and "squad_pref.set_mode(sq, cfg)" in ssrc)

# (7) loop audits elite selection
lsrc = Path("orchestrator/loop.py").read_text(encoding="utf-8")
chk("(7) _attempt audits squad_selected for elite runs",
    'audit.record("squad_selected"' in lsrc and "resolve_for_ticket(cfg, ticket)" in lsrc)

print("\n========== SQUAD MODES QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
