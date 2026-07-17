"""EU-44 (F1): the suite runner must fail honestly.

Before this fix `run_all.py` decided a harness's pass/fail **solely on its subprocess exit code**, and
parsed the ``k/n passed`` line only to sum TOTAL CHECKS. ~28 legacy harnesses use a soft ``check()``
tally that prints ``RESULT: … FAIL`` but never ``sys.exit(1)`` — so a real failure sailed through as
GREEN and a self-build that broke soft-tallied behaviour could auto-merge to DEV showing ALL GREEN.

This pins the runner's verdict function ``run_all._verdict`` directly (deterministic, no subprocess):
a printed ``k/n passed`` with ``k < n`` is now FAILED regardless of exit code, while harnesses with no
count line are still judged on their exit code alone (so the assert-based harnesses — effort,
integration_sse, officer_names, wr — keep passing on an honest 0 exit and don't false-positive). If
someone later re-softens the gate back to "exit code only", these checks go red.

Pure import + predicate assertions — no orchestrator import, no network."""
import os
import sys

# `import run_all` resolves to tests/run_all.py regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_all

V = run_all._verdict
results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

def ok(stdout, rc):      return V(stdout, rc)[0]
def checks(stdout, rc):  return V(stdout, rc)[1]
def reason(stdout, rc):  return V(stdout, rc)[3]

# ---- 1) THE EU-44 BUG: a soft-tally harness that prints k<n + FAIL but exits 0 is now RED ----
SOFT = "  [PASS] a\n  [FAIL] b broke\n  1/2 passed\n  RESULT: 1 FAIL\n"
chk("soft-tally failure (1/2 passed, exit 0) is FAILED", ok(SOFT, 0) is False)
chk("...and the reason names the soft-tally so the operator sees why", "soft-tally" in reason(SOFT, 0))
chk("...the k passed checks still feed TOTAL CHECKS", checks(SOFT, 0) == 1)

# ---- 2) a fully-green tally that exits 0 stays GREEN (no false positive) ----
GREEN = "  3/3 passed\n  RESULT: ALL GREEN\n"
chk("all-green tally (3/3, exit 0) passes", ok(GREEN, 0) is True)
chk("all-green tally feeds its full count into TOTAL CHECKS", checks(GREEN, 0) == 3)

# ---- 3) exit code is still respected: a green tally but non-zero exit still FAILS ----
chk("green tally yet non-zero exit still fails", ok(GREEN, 1) is False)

# ---- 4) NO count line + exit 0 → GREEN (protects assert-based effort/integration_sse/officer_names/wr) ----
chk("assert-style harness (no count line, exit 0) passes", ok("ALL OK 12 officers\n", 0) is True)
chk("no count line contributes 0 to TOTAL CHECKS (nothing to sum)", checks("OK\n", 0) == 0)

# ---- 5) NO count line + non-zero exit → FAILED (a raised assertion) ----
chk("assert-style harness that raised (no count, exit 1) fails",
    ok("Traceback (most recent call last):\nAssertionError\n", 1) is False)

# ---- 6) crash before any result line (exit 1, no count) → FAILED ----
chk("crash before a result line fails", ok("", 1) is False)

# ---- 7) robustness: the LAST real tally wins; a later 'passed' mention can't shadow it ----
SHADOW = "  2/2 passed\n  see notes/passed-criteria.md for details\n"
chk("a later non-tally 'passed' line doesn't erase the verdict", ok(SHADOW, 0) is True)
chk("...and the real count is still summed", checks(SHADOW, 0) == 2)

# ---- 8) degenerate 0/0 tally is k==n, not a failure ----
chk("0/0 passed is not treated as a failure", ok("  0/0 passed\n", 0) is True)

# ---- 9) the pytest-summary branch (EU-244 defense-in-depth) — previously UNPINNED ----
# EU-373: the branch only probed "N failed". A pytest COLLECTION/import error prints no "failed" at
# all — verified 2026-07-17 against the repo venv's pytest, a bad import yields exactly
# "=== 1 error in 0.03s ===" (+ "Interrupted: 1 error during collection") and exit 2 — so _verdict
# returned GREEN on it at rc=0. Harmless today (every pytest harness does sys.exit(pytest.main(...))
# so the non-zero rc catches it), but the branch exists precisely for a harness that loses that
# sys.exit, and for that class it was blind to the entire error half.
PYTEST_ERR = ("==================== ERRORS ====================\n"
              "ERROR broken_test.py\n"
              "!!!!!! Interrupted: 1 error during collection !!!!!!\n"
              "=============================== 1 error in 0.03s ===============================\n")
chk("pytest collection error (no 'failed' line) with a lying exit 0 is FAILED", ok(PYTEST_ERR, 0) is False)
chk("...and the reason names the error summary so the operator sees why", "1 error" in reason(PYTEST_ERR, 0))
chk("pytest collection error with pytest's real exit 2 is FAILED", ok(PYTEST_ERR, 2) is False)

PYTEST_FAILED = "==================== 1 failed, 7 passed in 0.42s ====================\n"
chk("pytest 'N failed' summary with a lying exit 0 is FAILED", ok(PYTEST_FAILED, 0) is False)
chk("...and the reason names the failed count", "1 failed" in reason(PYTEST_FAILED, 0))

# No false positives: the >0 guard and the summary anchoring must both hold. A harness printing its
# own "0 failed" tally (eu195) or narrating errors in prose is NOT a pytest red run.
chk("a benign '0 failed' custom tally still passes", ok("Results: 4 passed, 0 failed\n", 0) is True)
chk("a harness narrating '3 error responses' in prose is not misread as a pytest error summary",
    ok("  [PASS] classifier handles 3 error responses gracefully\n", 0) is True)
chk("a '0 errors in 0.01s' summary is not a failure (the >0 guard)",
    ok("=========== 0 errors in 0.01s ===========\n", 0) is True)

print("\n=============== EU-44 HONEST-GATE (run_all) ===============")
passed = sum(1 for _, c, _ in results if c)
for n, c, det in results:
    print(f"  [{'PASS' if c else 'FAIL'}] {n}" + (f"  ({det})" if det and not c else ""))
print("-----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
