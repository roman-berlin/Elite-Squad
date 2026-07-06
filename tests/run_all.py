#!/usr/bin/env python3
"""The Elite Unit's own regression suite — the guard that guards the guard.

Runs every ``tests/*_test.py`` harness from the repo root, tallies the checks, and exits non-zero if any
harness fails. CI runs this on every push to ``dev`` (see ``.github/workflows/ci.yml``); the server's
self-update should refuse a red ``main``. Each harness stubs the Agent SDK and asserts a slice of the
orchestrator's behaviour — no network, no real models, fast.

A harness counts as GREEN only when it exits 0 **and**, if it printed its own ``k/n passed`` tally,
``k == n``. This is the EU-44 fix: ~28 legacy harnesses use a soft ``check()`` helper that prints
``k/n passed`` + ``RESULT: … FAIL`` but never ``sys.exit(1)``, so a genuine failure used to sail
through on the subprocess's 0 exit code and the suite reported ALL GREEN while a quarter of itself was
failure-blind. We now read that self-reported tally and fail the harness when ``k < n`` regardless of
its exit code. Harnesses that print no count line (the assert-based ones — they raise on failure) are
judged purely on their exit code, exactly as before. See ``_verdict``.

  python tests/run_all.py            # run the whole suite
  python tests/run_all.py --verbose  # also print the tail of any failing harness
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # the General repo root
TESTS = sorted(p for p in (ROOT / "tests").glob("*_test.py"))

# EU-139 gate incident (2026-07-05 22:24): a ticket run's gate executes this suite as a child of the
# orchestrator, so harnesses inherit its live credentials — and a harness that stubbed the SDK but not
# `orchestrator.notify` (eu108_sonnet_fallback_test) sent a REAL "Sonnet weekly cap hit" Telegram alert
# to the ops chat mid-gate. The suite's contract is "no network, no real models": strip outbound
# messaging credentials from every harness's environment so no test can page the Commander, no matter
# which process spawns the suite. A harness that tests notify sets its own fake env in-process.
_CHILD_ENV = {k: v for k, v in os.environ.items()
              if k not in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}


def _verdict(stdout: str, returncode: int) -> tuple[bool, int, str, str]:
    """Decide one harness's honest pass/fail from its stdout and exit code (the EU-44 gate).

    Scans for the harness's self-reported ``k/n passed`` tally (the last such line wins, so a stray
    later mention of "passed" can't shadow the real count). The verdict:

    * a tally line present and ``k < n`` → FAIL, even if the harness exited 0 (the soft-tally bug:
      ~28 harnesses print ``RESULT: … FAIL`` but never ``sys.exit(1)``);
    * a tally line present and ``k == n`` → pass iff the exit code is also 0;
    * no tally line at all → trust the exit code unchanged (the assert-based harnesses raise on
      failure, so a 0 exit is an honest pass — failing them on "no count" would be a false positive).

    Returns ``(ok, checks, line, reason)``: ``checks`` is k for the running TOTAL CHECKS total,
    ``line`` is the tally line to display, and ``reason`` explains a tally-driven failure.
    """
    line, count = "", None
    for ln in stdout.splitlines():
        m = re.search(r"(\d+)/(\d+) passed", ln)
        if m:
            line, count = ln.strip(), (int(m.group(1)), int(m.group(2)))
    if count is None:
        return returncode == 0, 0, line, ""          # no self-tally → judge on the exit code alone
    k, n = count
    if k < n:
        reason = f"soft-tally FAIL: only {k}/{n} of its own checks passed (harness exited {returncode})"
        return False, k, line, reason
    return returncode == 0, k, line, ""


def main() -> int:
    passed = failed = total_checks = 0
    red: list[str] = []
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    for t in TESTS:
        r = subprocess.run([sys.executable, str(t)], cwd=str(ROOT), capture_output=True, text=True,
                           env=_CHILD_ENV)
        ok, checks, line, reason = _verdict(r.stdout, r.returncode)
        total_checks += checks
        if ok:
            passed += 1
            print(f"  ✓ {t.name:<32} {line}")
        else:
            failed += 1
            red.append(t.name)
            print(f"  ✗ {t.name:<32} {reason or line or '(crashed before a result line)'}")
            # Always show a failing harness's tail — a bare tally made CI failures undiagnosable
            # from the run log (2026-07-05: four harnesses failed only in CI and the email showed
            # nothing but names). --verbose widens the tail.
            tail = 25 if verbose else 12
            print("\n".join(("      " + x) for x in (r.stdout + r.stderr).strip().splitlines()[-tail:]))
    print("=" * 64)
    print(f"  HARNESSES: {passed} passed / {passed + failed}     TOTAL CHECKS: {total_checks}")
    if red:
        print("  FAILED:", " ".join(red))
    else:
        print("  ALL GREEN")
    print("=" * 64)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
