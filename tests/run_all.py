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
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # the General repo root
TESTS = sorted(p for p in (ROOT / "tests").glob("*_test.py"))

# EU-360: reuse the orchestrator's own credential predicate so the suite's env strip can never drift
# from the officer/gate subprocess boundary. Fallback keeps run_all import-robust (it is the
# guard-of-guards — an import error here would take the whole suite down).
sys.path.insert(0, str(ROOT))
try:
    from orchestrator.backends import is_sensitive_key as _is_sensitive_key
except Exception:  # noqa: BLE001
    def _is_sensitive_key(key: str) -> bool:
        return key.startswith(("JIRA_", "TELEGRAM_")) or key == "GENERAL_COCKPIT_PROMOTE"

# EU-139 gate incident (2026-07-05 22:24): a ticket run's gate executes this suite as a child of the
# orchestrator, so harnesses inherit its live credentials — and a harness that stubbed the SDK but not
# `orchestrator.notify` (eu108_sonnet_fallback_test) sent a REAL "Sonnet weekly cap hit" Telegram alert
# to the ops chat mid-gate. The suite's contract is "no network, no real models": strip EVERY sensitive
# credential (EU-360 widened this from just TELEGRAM_* to the full is_sensitive_key set — JIRA_*, the
# messaging tokens, GENERAL_COCKPIT_PROMOTE) from every harness's environment so no test can page the
# Commander OR reach a real Jira, no matter which process spawns the suite. A harness that tests one of
# these sets its own fake env in-process.
_CHILD_ENV = {k: v for k, v in os.environ.items() if not _is_sensitive_key(k)}
# 2026-07-15 (auth liveness): health.checks() now probes credential VALIDITY with a real
# `claude -p` round-trip (network + a real model) unless GENERAL_AUTH_PROBE=0 — force it off for
# every harness, same contract as the Telegram strip above. A harness that tests the probe stubs
# auth_probe._run_probe / clears this var in-process (auth_liveness_test.py).
_CHILD_ENV["GENERAL_AUTH_PROBE"] = "0"

# EU-360: a per-harness wall-clock ceiling so one hung harness can no longer stall the whole suite
# (subprocess.run had NO timeout — a wedged harness froze run_all, and with it any gate that shells
# out to it). Generous: the known-heavy harnesses top out ~19s. Override with GENERAL_TEST_TIMEOUT.
_HARNESS_TIMEOUT_S = int(os.environ.get("GENERAL_TEST_TIMEOUT", "300") or 300)


def _run_harness(path: Path) -> tuple[str, str, int, bool]:
    """Run one harness, bounded by _HARNESS_TIMEOUT_S. Returns (stdout, stderr, returncode, timed_out).

    Uses a new session + killpg so a harness that spawned its own grandchild (e.g. the gate tests'
    `sleep`) can't outlive the timeout. Decoding is error-tolerant (EU-360): a single invalid byte in
    a harness's output used to crash the runner mid-suite with strict UTF-8 decoding."""
    try:
        proc = subprocess.Popen(
            [sys.executable, str(path)], cwd=str(ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace", env=_CHILD_ENV,
            start_new_session=True,
        )
    except OSError as exc:
        return "", f"(could not launch harness: {exc!r})", 1, False
    try:
        stdout, stderr = proc.communicate(timeout=_HARNESS_TIMEOUT_S)
        return stdout or "", stderr or "", proc.returncode, False
    except subprocess.TimeoutExpired as texc:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            proc.kill()
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except Exception:  # noqa: BLE001 — reap raced or a grandchild still holds the pipe
            stdout = texc.stdout if isinstance(texc.stdout, str) else ""
            stderr = texc.stderr if isinstance(texc.stderr, str) else ""
        return stdout or "", stderr or "", proc.returncode or -1, True


def _verdict(stdout: str, returncode: int) -> tuple[bool, int, str, str]:
    """Decide one harness's honest pass/fail from its stdout and exit code (the EU-44 gate).

    Scans for the harness's self-reported ``k/n passed`` tally (the last such line wins, so a stray
    later mention of "passed" can't shadow the real count). The verdict:

    * a tally line present and ``k < n`` → FAIL, even if the harness exited 0 (the soft-tally bug:
      ~28 harnesses print ``RESULT: … FAIL`` but never ``sys.exit(1)``);
    * a tally line present and ``k == n`` → pass iff the exit code is also 0;
    * no tally line at all, but a pytest ``N failed`` summary line is present → FAIL regardless of
      exit code (EU-244: the pytest-style harnesses' ``__main__`` blocks now do
      ``sys.exit(pytest.main(...))``, but this is defense-in-depth for the whole class — a harness
      that regresses back to a bare ``pytest.main(...)`` call, or any future pytest harness that
      forgets ``sys.exit``, still can't sail through on the always-0 exit code);
    * no tally line and no pytest failure summary → trust the exit code unchanged (the assert-based
      harnesses raise on failure, so a 0 exit is an honest pass — failing them on "no count" would
      be a false positive).

    Returns ``(ok, checks, line, reason)``: ``checks`` is k for the running TOTAL CHECKS total,
    ``line`` is the tally line to display, and ``reason`` explains a tally-driven failure.
    """
    line, count = "", None
    for ln in stdout.splitlines():
        m = re.search(r"(\d+)/(\d+) passed", ln)
        if m:
            line, count = ln.strip(), (int(m.group(1)), int(m.group(2)))
    if count is None:
        # A pytest summary line reads "1 failed, 7 passed in 0.42s"; only a NON-ZERO failed count
        # is a red run. Requiring >0 is what keeps a benign "0 failed" tally (e.g. eu195's custom
        # "Results: 4 passed, 0 failed") from being misread as a failure.
        pf = re.search(r"(\d+) failed", stdout)
        if pf and int(pf.group(1)) > 0:
            reason = f"pytest FAIL: {pf.group(0)} (harness exited {returncode})"
            return False, 0, line, reason
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
        stdout, stderr, returncode, timed_out = _run_harness(t)
        if timed_out:
            # A timeout is an unconditional FAIL regardless of any partial tally the harness printed
            # before it wedged (EU-360) — the suite must never green a harness it had to kill.
            ok, checks, line, reason = False, 0, "", (
                f"TIMED OUT after {_HARNESS_TIMEOUT_S}s (killed — raise GENERAL_TEST_TIMEOUT if legit)")
        else:
            ok, checks, line, reason = _verdict(stdout, returncode)
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
            print("\n".join(("      " + x) for x in (stdout + stderr).strip().splitlines()[-tail:]))
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
