"""Test the Builder's task-adaptive effort sizing. Stubs the SDK (not used by the sizer)."""
import sys
import types

# --- stub claude_agent_sdk so orchestrator.builder imports without the real SDK ---
sdk = types.ModuleType("claude_agent_sdk")


class _Dummy:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self


sdk.__getattr__ = lambda name: _Dummy          # any `from claude_agent_sdk import X`
sys.modules["claude_agent_sdk"] = sdk

sys.path.insert(0, ".")
from orchestrator.builder import size_ticket, effort_plan, effort_for
from orchestrator.config import normalize_effort, EFFORT_LEVELS
from orchestrator.contracts import Ticket

print("=== normalize_effort ===")
assert normalize_effort("ultra") == "xhigh", normalize_effort("ultra")
assert normalize_effort("ultracode") == "xhigh"
assert normalize_effort("ultrathink") == "xhigh"
assert normalize_effort("MAX") == "max"
assert normalize_effort("maximum") == "max"
assert normalize_effort("Medium") == "medium"
assert normalize_effort("xhigh") == "xhigh"
assert normalize_effort("bogus") == "high"          # unknown -> default
assert all(lv in EFFORT_LEVELS for lv in ("low", "medium", "high", "xhigh", "max"))
print("normalize OK:", {k: normalize_effort(k) for k in ("ultra", "max", "med", "xh", "bogus")})

ns = types.SimpleNamespace
CFG = ns(adaptive_effort=True, builder_effort="high", escalate_effort_on_retry=True)


def tk(summary, desc="", ac=None, labels=None, itype=None):
    return Ticket(id="X-1", key="X-1", summary=summary, description=desc,
                  acceptance_criteria=ac or [], labels=labels or [], issue_type=itype)


cases = [
    ("typo",     tk("Fix typo in footer copy", "Change 'recieve' to 'receive'."),            "low"),
    ("css",      tk("Adjust padding on login button", "Bump padding to 12px."),              "low"),
    ("bug-short", tk("Fix null pointer on dashboard", "Crashes sometimes.", labels=["bug"]), "low"),
    ("medium",   tk("Add CSV export to leads table",
                    "Add an export button that downloads the current leads view as CSV. "
                    "Respect active filters and column order." * 2,
                    ac=["Button visible on /leads", "Exports filtered rows", "UTF-8 CSV"]),  None),
    ("heavy",    tk("Refactor auth to tenant-isolated RBAC and migrate the permissions schema",
                    "Re-architect authorization across all services. Introduce row-level "
                    "tenant isolation, migrate the permissions schema, and ensure backward "
                    "compatibility. Security-critical. " * 4,
                    ac=["RBAC enforced", "tenant isolation", "schema migrated", "no breaking change",
                        "audit log", "tests for each role"]),                                 "high"),
    ("epic",     tk("Billing system overhaul", "Payment + invoicing rewrite.", labels=["epic"]), "high"),
    ("pin-label", tk("Refactor auth & migrate schema (heavy) but pinned low",
                     "Security-critical migration.", labels=["effort-low"]),                 "low"),
    ("pin-marker", tk("Tiny copy tweak", "Change wording. [effort:max] please."),            "max"),
    ("pin-ultra-label", tk("Heavy security migration", "Re-architect auth.",
                           labels=["effort-ultra"]),                                          "xhigh"),
    ("pin-ultra-marker", tk("Tiny tweak", "Change wording. [effort:ultra]"),                  "xhigh"),
]

print("=== sizing ===")
ok = True
for name, t, expect in cases:
    size, eff, why = size_ticket(t)
    flag = ""
    if expect and eff != expect:
        flag = f"  <-- EXPECTED {expect}"; ok = False
    print(f"{name:12} -> {eff:7} [{size}]  ({why[:70]}){flag}")

print("\n=== escalation on retry (medium base) ===")
med = tk("Add CSV export", "moderate", ac=["a", "b"])
base = size_ticket(med)[1]
e1 = effort_for(CFG, 1, med)
e2 = effort_for(CFG, 2, med)
e3 = effort_for(CFG, 3, med)
print(f"base={base}  pass1={e1}  pass2={e2}  pass3={e3}")
order = ["low", "medium", "high", "max"]
assert order.index(e2) >= order.index(e1) and order.index(e3) >= order.index(e2), "must not de-escalate"
assert e2 != e1 or e1 == "max", "retry should bump effort"

print("\n=== xhigh pin escalates toward max on retry ===")
xt = tk("x", "Re-architect auth, security-critical migration.", labels=["effort-ultra"])
xe1, xe2 = effort_for(CFG, 1, xt), effort_for(CFG, 2, xt)
print(f"pinned ultra: pass1={xe1}  pass2={xe2}")
assert xe1 == "xhigh", xe1
assert xe2 == "max", xe2

print("\n=== adaptive OFF -> configured default ===")
off = ns(adaptive_effort=False, builder_effort="high", escalate_effort_on_retry=True)
d = effort_for(off, 1, cases[0][1])  # a typo ticket, but sizing is off
print(f"typo ticket, adaptive off -> {d} (expect high)")
assert d == "high", "with adaptive off, must use builder_effort"

print("\n=== plan reason string ===")
print(effort_plan(CFG, 2, cases[4][1]))

print("\n=== auto-sizing never reaches max/xhigh (only explicit pins do) ===")
auto_efforts = set()
for cname, ctk, _exp in cases:
    if "pin" in cname:
        continue
    auto_efforts.add(size_ticket(ctk)[1])
print("auto efforts seen:", auto_efforts)
assert "max" not in auto_efforts and "xhigh" not in auto_efforts, "auto-sizing must cap at high"

print("\n=== deliberate-halt detection ===")
from orchestrator.loop import _is_deliberate_halt
auto13 = ("HALTED before any write. Preconditions FAILED — the base tree does not contain P2. "
          "I have made no writes. Holding for your direction. FOR THE COMMANDER.")
assert _is_deliberate_halt(auto13) is True, "should detect the AUTO-13 halt"
assert _is_deliberate_halt("Implemented the change in config.ts; added a test. Done.") is False
assert _is_deliberate_halt("") is False
print("halt detection OK")

print("\nALL OK" if ok else "\nSOME EXPECTATIONS FAILED")
assert ok, "sizing expectations failed"
