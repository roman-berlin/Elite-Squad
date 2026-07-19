"""EU-381: save_error_counts gives the swallowed-exception path ONE bounded retry.

Forensics (2026-07-17, proving EU-218's "10 consecutive green full runs" AC): 9/10 full suites
were green; run 1's lone red was eu256_error_counts_lock_test's AC3 union — a harness EU-218
never touched. locked_rmw itself is provably correct (per-path threading.Lock + fcntl.flock
sidecar, locking.py), so the lost write wasn't a locking defect: one of the 40 concurrent
locked_rmw calls hit a transient flock/open OSError under maximal box load and
save_error_counts's `except (OSError, ValueError): pass` silently dropped that write — erasing
an increment and resetting park-after-3 progress (~5% full-suite-only flake).

Fix shape (the ticket's option (a) — harden production, not the test): retry the locked_rmw
ONCE after a short pause, then fall back to the original best-effort swallow. Bounded by
construction: exactly two attempts, never a loop, never an exception out of the save.

Pins:
  1. A single transient OSError from locked_rmw no longer drops the write — the retry lands it
     on disk and warms the per-drain baseline cache exactly like a first-try success.
  2. The retry is BOUNDED: a persistent failure makes exactly 2 attempts (one retry, no more),
     escapes no exception (the drain never breaks on a best-effort save), and does NOT warm the
     baseline cache (nothing was written, so there is no snapshot to remember).
  3. A clean first-try success makes exactly 1 attempt — no double-write on the happy path.
  4. This harness is picked up by tests/run_all.py's *_test.py glob.
"""
import sys
import tempfile
from pathlib import Path

# Stub the Agent SDK + requests so importing the orchestrator never reaches the network.
import types
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda *a, **k: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import autopilot, locking
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))
    print(("PASS: " if c else "FAIL: ") + n + (f" - {d}" if d and not c else ""))


tmp = Path(tempfile.mkdtemp())
REAL_RMW = locking.locked_rmw


def flaky_rmw(fail_on: set[int]):
    """A locked_rmw wrapper that raises a transient OSError on the given 1-based attempt numbers
    and delegates to the real primitive otherwise. Returns (wrapper, attempts-list)."""
    attempts = []
    def _wrapper(path, mutate_fn, **kw):
        attempts.append(1)
        if len(attempts) in fail_on:
            raise OSError("transient flock unavailable (simulated load)")
        return REAL_RMW(path, mutate_fn, **kw)
    return _wrapper, attempts


def _cfg(name: str) -> Config:
    # Each AC gets its own SUBDIRECTORY: _error_counts_file derives error_counts.json from the
    # audit path via with_name, so sibling audit files would alias one shared counts file (and one
    # shared baseline-cache key) and let AC1's write bleed into AC2's "nothing landed" assertions.
    d = tmp / name
    d.mkdir(exist_ok=True)
    app = AppConfig(name="elite-unit", repo_path=str(tmp), base_branch="dev", protected_branch="main",
                    backlog_backend="jira", backlog={"base_url": "https://x.atlassian.net", "project_key": "EU"})
    return Config(apps=[app], audit_path=str(d / "audit.jsonl"), use_worktree=False)


# ============================================================================================== #
# AC1: one transient OSError -> the retry still lands the write (the ~5% eu256 flake's shape)
# ============================================================================================== #
print("\n=== AC1: single transient failure -> write survives via the bounded retry ===")
cfg1 = _cfg("ac1")
wrapper1, attempts1 = flaky_rmw(fail_on={1})
autopilot.locking.locked_rmw = wrapper1
try:
    autopilot.save_error_counts(cfg1, {"EU-151": 2})
finally:
    autopilot.locking.locked_rmw = REAL_RMW
after1 = autopilot.load_error_counts(cfg1)
chk("a single transient OSError no longer drops the write (retry landed it)",
    after1.get("EU-151") == 2, str(after1))
chk("the transient cost exactly one extra attempt (2 total)", len(attempts1) == 2, str(len(attempts1)))
key1 = (str(autopilot._error_counts_file(cfg1)), "")
chk("the per-drain baseline cache is warmed on retry success (same as first-try success)",
    autopilot._error_counts_seen.get(key1) == {"EU-151": 2}, str(autopilot._error_counts_seen.get(key1)))

# ============================================================================================== #
# AC2: persistent failure -> exactly 2 attempts, no exception, best-effort contract kept
# ============================================================================================== #
print("\n=== AC2: persistent failure -> bounded (2 attempts), swallowed, cache untouched ===")
cfg2 = _cfg("ac2")
wrapper2, attempts2 = flaky_rmw(fail_on={1, 2, 3, 4, 5})   # would fail forever if unbounded
autopilot.locking.locked_rmw = wrapper2
raised = None
try:
    autopilot.save_error_counts(cfg2, {"EU-9": 1})
except BaseException as e:                                  # noqa: BLE001 - the pin IS "never raises"
    raised = e
finally:
    autopilot.locking.locked_rmw = REAL_RMW
chk("a persistent failure escapes no exception (drain never breaks on a best-effort save)",
    raised is None, repr(raised))
chk("the retry is bounded: exactly 2 attempts (one retry), not a loop",
    len(attempts2) == 2, str(len(attempts2)))
chk("nothing was written on double-failure", autopilot.load_error_counts(cfg2) == {},
    str(autopilot.load_error_counts(cfg2)))
key2 = (str(autopilot._error_counts_file(cfg2)), "")
chk("the baseline cache is NOT warmed when nothing landed on disk",
    key2 not in autopilot._error_counts_seen, str(autopilot._error_counts_seen.get(key2)))

# ============================================================================================== #
# AC3: clean success -> exactly 1 attempt (no gratuitous double-write on the happy path)
# ============================================================================================== #
print("\n=== AC3: clean first-try success -> exactly one attempt ===")
cfg3 = _cfg("ac3")
wrapper3, attempts3 = flaky_rmw(fail_on=set())
autopilot.locking.locked_rmw = wrapper3
try:
    autopilot.save_error_counts(cfg3, {"EU-10": 1})
finally:
    autopilot.locking.locked_rmw = REAL_RMW
chk("happy path makes exactly 1 attempt", len(attempts3) == 1, str(len(attempts3)))
chk("happy-path write landed", autopilot.load_error_counts(cfg3) == {"EU-10": 1},
    str(autopilot.load_error_counts(cfg3)))


print("\n================ EU-381 SAVE-ERROR-COUNTS RETRY QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
