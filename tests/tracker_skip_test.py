"""2026-07-21 (Commander: 'tickets? 401 invalid?'): [infra-signature] pattern-tracker tickets
must never enter the build queue — not the drain, not the daily's 'Next up' (both flow through
intake.from_drain). Pins: label match, title-prefix fallback, ordinary tickets untouched, and
the filter wired at the single queue choke point."""
import sys, types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import intake
from orchestrator.contracts import Ticket

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

chk("labelled tracker is skipped",
    intake.is_tracker_ticket(Ticket(id="EU-401", key="EU-401", summary="control request timeout",
                                    description="d", labels=["infra-signature", "autofiled"])))
chk("title-prefix fallback catches label-stripped trackers",
    intake.is_tracker_ticket(Ticket(id="EU-400", key="EU-400",
                                    summary="[infra-signature] max passes — pm escalated fail",
                                    description="d")))
chk("an ordinary ticket is NOT a tracker",
    not intake.is_tracker_ticket(Ticket(id="EU-403", key="EU-403",
                                        summary="Out-of-process watchdog", description="d",
                                        labels=["autodev"])))
src = Path("orchestrator/intake.py").read_text(encoding="utf-8")
# EU-731 widened this line to `if is_tracker_ticket(ticket) or is_epic(ticket):` — an Epic is the
# same shape of mistake (a container, never buildable work). Assert the CONTRACT (the tracker filter
# runs at the from_drain choke point, so it covers the drain AND the daily's "Next up") rather than
# the exact source line, which pinned a literal and broke on a purely additive change.
_call = next((ln for ln in src.splitlines()
              if "is_tracker_ticket(ticket)" in ln and ln.strip().startswith("if ")), "")
chk("the filter sits at the from_drain choke point (drain + daily Next-up)",
    bool(_call) and "from_drain" in src and _call.strip().endswith(":"), _call.strip())

print("\n========== TRACKER SKIP QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else "FAIL")
sys.exit(0 if passed == len(results) else 1)
