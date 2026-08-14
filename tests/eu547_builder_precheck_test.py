"""EU-547 — source-pin test: the Builder contract (BUILDER_SYSTEM) carries an explicit
'PRE-CLAIM SELF-CHECK' numbered checklist covering three gates: (1) every production
change gets tests, (2) vacuous-assertion guards (.find() ordering / literal-True),
(3) stub-signature drift (**kwargs).

FAIL-FIRST: this file MUST exit non-zero when any assertion fails. Verify by forcing
one check red and running standalone."""

import sys, types

REPO = "."
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, REPO)

from orchestrator import builder

P = builder.BUILDER_SYSTEM
low = P.lower()

checks = []

# ---- AC#1: the label itself is present ----
checks.append(("PRE-CLAIM SELF-CHECK label present",
                "pre-claim self-check" in low))

# ---- AC#2: every production change requires tests ----
checks.append(("test-accompaniment rule ('tests accompany')",
                "tests accompany" in low))

# ---- AC#3: vacuous-assertion gate referenced by name AND both patterns ----
checks.append(("vacuous_assertion_guard_test mechanism name cited",
                "vacuous_assertion_guard_test" in low))
checks.append((".find() ordering pattern named",
                ".find()" in low))
checks.append(("literal-True check condition named",
                "literal-true" in low or "literal true" in low))
checks.append(("BUILD_DOCTRINE.md mechanism 3 cited",
                "mechanism 3" in low))

# ---- AC#4: stub-signature gate referenced by name AND the fix pattern ----
checks.append(("stub_signature_test mechanism name cited",
                "stub_signature_test" in low))
checks.append(("**kwargs fix pattern named",
                "**kwargs" in low))
checks.append(("BUILD_DOCTRINE.md mechanism 5 cited",
                "mechanism 5" in low))

print("\n============ EU-547 BUILDER PRE-CLAIM SELF-CHECK QA ============")
passed = sum(1 for _, ok in checks if ok)
for n, ok in checks:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}")
failed_count = len(checks) - passed
print("---------------------------------------------------")
print(f"  {passed}/{len(checks)} passed")
if failed_count:
    print(f"  RESULT: {failed_count} FAIL ❌ — missing pre-claim self-check blocks")
else:
    print("  RESULT: ALL GREEN ✅")
sys.exit(0 if failed_count == 0 else 1)
