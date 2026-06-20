#!/usr/bin/env python3
"""The Elite Unit's own regression suite — the guard that guards the guard.

Runs every ``tests/*_test.py`` harness from the repo root, tallies the checks, and exits non-zero if any
harness fails. CI runs this on every push to ``dev`` (see ``.github/workflows/ci.yml``); the server's
self-update should refuse a red ``main``. Each harness stubs the Agent SDK and asserts a slice of the
orchestrator's behaviour — no network, no real models, fast.

  python tests/run_all.py            # run the whole suite
  python tests/run_all.py --verbose  # also print the tail of any failing harness
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # the General repo root
TESTS = sorted(p for p in (ROOT / "tests").glob("*_test.py"))


def main() -> int:
    passed = failed = total_checks = 0
    red: list[str] = []
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    for t in TESTS:
        r = subprocess.run([sys.executable, str(t)], cwd=str(ROOT), capture_output=True, text=True)
        line = ""
        for ln in r.stdout.splitlines():
            if "passed" in ln and "/" in ln:
                line = ln.strip()
        m = re.search(r"(\d+)/(\d+) passed", line)
        if m:
            total_checks += int(m.group(1))
        if r.returncode == 0:
            passed += 1
            print(f"  ✓ {t.name:<32} {line}")
        else:
            failed += 1
            red.append(t.name)
            print(f"  ✗ {t.name:<32} {line or '(crashed before a result line)'}")
            if verbose:
                print("\n".join(("      " + x) for x in (r.stdout + r.stderr).strip().splitlines()[-25:]))
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
