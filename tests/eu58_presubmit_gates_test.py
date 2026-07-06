"""EU-58 / EU-97 QA — the Builder officer prompt documents the three hard pre-submit gates (axe-core
zero-violations + `bun test --coverage` + security countersignature) so they are enforced on every
ticket, and frames gate failures as the Builder's own remediation work BEFORE the Reviewer sees the
diff. EU-97 adds the security countersignature gate and the reference to officers/builder.md."""
import sys, types

REPO = "."
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, REPO)

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))

from orchestrator import builder

P = builder.BUILDER_SYSTEM
low = P.lower()

# ---- both gates are documented in the officer prompt ----
check("axe-core a11y gate is documented", "axe-core" in low)
check("a11y gate requires ZERO violations", "zero violations" in low)
check("coverage gate runs `bun test --coverage`", "bun test --coverage" in low)
check("there is a named PRE-SUBMIT GATES section", "pre-submit gates" in low)

# ---- gates are HARD / mandatory, run before Reviewer ----
check("gates are marked mandatory", "mandatory" in low)
check("gates run before the Reviewer hand-off",
      "before" in low and "reviewer" in low)
# AC#2: the coverage gate must pass with NO failing tests
check("coverage gate requires no failing tests",
      "no failing tests" in low)
# gates run BEFORE the summary is written / hand-off (ordering is explicit)
check("gates run before the summary / hand-off",
      "before you write your summary" in low or "before you finish" in low
      or "you do not finish until" in low)

# ---- gate failures are the Builder's remediation BEFORE Reviewer sees the diff ----
check("gate failures framed as Builder remediation", "remediation" in low)
check("do not hand a failing diff to Reviewer",
      "known gate failure" in low or "do not hand a diff" in low)

# ---- backend/config tickets are not forced to scan a non-existent UI ----
check("backend/config tickets exempt from a11y scan",
      "backend/config" in low and "nothing to scan" in low)

# ---- outcome of all gates must be reported (auditable) ----
check("all gate outcomes reported in the summary",
      "report the outcome of all gates" in low)

# ---- Phase-2 §2 (2026-07-06): the EU-97 §1/§2/§3 security COUNTERSIGNATURE was retired with the
# LLM security gate that consumed it. The builder prompt keeps a plain security reminder (don't
# commit secrets, parameterise, guard routes); the deterministic secret/dep scan (gate.py) now
# enforces the mechanical half.
check("builder prompt keeps a security reminder", "security" in low)
check("builder prompt no longer carries the retired §1/§2/§3 countersignature template",
      "§1-secrets" not in P and "§2-authz" not in P and "§3-injection" not in P)

# ---- the section is in the prompt the Builder actually receives ----
from orchestrator.contracts import BuildRequest, Ticket
tk = Ticket(id="EU-58", key="EU-58", summary="x", description="d", acceptance_criteria=["a"])
# (the gate text lives in the SYSTEM prompt, asserted above; the user prompt carries the ticket)
up = builder._prompt(BuildRequest(ticket=tk, branch="dev", iteration=1))
check("user prompt still renders the ticket normally", "TICKET EU-58" in up)

print("\n============ EU-58 / EU-97 PRE-SUBMIT GATES QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
