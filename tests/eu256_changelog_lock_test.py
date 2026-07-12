"""EU-276: _record_changelog's read Documentation/Development_Status.md -> rewrite must be lock-
serialized so two concurrent LIVE lands never lose an entry.

Forensics: _record_changelog (orchestrator/loop.py) used a bare path.read_text() (old `- ` entries)
-> path.write_text(header + new + old) rewrite with no lock. Two lands landing inside the same
read->write window both read the same stale `old` list, so whichever writes second overwrites the
other's entry — the file already interleaves EU + AUTO lines from real concurrent drains, so this is
not theoretical. The fix routes the read and the write through locking.locked_text_rmw so both
happen inside one held cross-thread + cross-process lock, with `old` read from the value handed to
the mutate callback INSIDE the lock, never a pre-lock snapshot.

Pins:
  1. Two threads, each landing a distinct ticket/app repeatedly and concurrently against the SAME
     target file, must both have every one of their entries survive in the final file (no lost `- `
     line) — the actual park-after-race defeat this ticket fixes.
  2. Total entry count equals the sum of both threads' writes (nothing silently clobbered).
  3. Exactly one header survives repeated concurrent rewrites (no duplicate/partial header either).
"""
import sys, types, tempfile, threading
from pathlib import Path

# Stub the Agent SDK so importing the orchestrator never reaches the network.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import loop
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))
    print(("PASS: " if c else "FAIL: ") + n + (f" - {d}" if d and not c else ""))


tmp = Path(tempfile.mkdtemp())
doc = tmp / "Documentation" / "Development_Status.md"

app_eu = AppConfig(name="Elite-Unit", repo_path=str(tmp), base_branch="dev",
                   protected_branch="main", backlog_backend="none")
app_auto = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev",
                     protected_branch="main", backlog_backend="none")
cfg = Config(apps=[app_eu, app_auto], audit_path=str(tmp / "audit.jsonl"), dry_run=False)

# ============================================================================================== #
# Two different tickets/apps landing concurrently against the same changelog file, many rounds
# each, to make the race window (if any) near-certain to be hit.
# ============================================================================================== #
print("\n=== concurrent lands: no lost entry, no clobbered file ===")
ROUNDS = 25
barrier = threading.Barrier(2)


def _land_eu() -> None:
    barrier.wait()
    for i in range(ROUNDS):
        t = Ticket(id=f"EU-{i}", key=f"EU-{i}", summary=f"eu change {i}", description="")
        loop._record_changelog(cfg, t, app_eu, f"eu summary {i}", "", today="2026-07-12", path=str(doc))


def _land_auto() -> None:
    barrier.wait()
    for i in range(ROUNDS):
        t = Ticket(id=f"AUTO-{i}", key=f"AUTO-{i}", summary=f"auto change {i}", description="")
        loop._record_changelog(cfg, t, app_auto, f"auto summary {i}", "", today="2026-07-12", path=str(doc))


t1 = threading.Thread(target=_land_eu)
t2 = threading.Thread(target=_land_auto)
t1.start(); t2.start()
t1.join(); t2.join()

body = doc.read_text(encoding="utf-8")
lines = [ln for ln in body.splitlines() if ln.startswith("- ")]

expected_eu = {f"EU-{i}" for i in range(ROUNDS)}
expected_auto = {f"AUTO-{i}" for i in range(ROUNDS)}
present_eu = {tid for tid in expected_eu if any(f"· {tid} ·" in ln for ln in lines)}
present_auto = {tid for tid in expected_auto if any(f"· {tid} ·" in ln for ln in lines)}

chk("no EU entry lost under concurrent lands", present_eu == expected_eu,
    f"missing {expected_eu - present_eu}")
chk("no AUTO entry lost under concurrent lands", present_auto == expected_auto,
    f"missing {expected_auto - present_auto}")
chk("total entry count == both threads' writes combined (nothing clobbered)",
    len(lines) == 2 * ROUNDS, f"{len(lines)} != {2 * ROUNDS}")
chk("exactly one header survives repeated concurrent rewrites",
    body.count("# Development Status") == 1, str(body.count("# Development Status")))

print("\n=============== EU-276 CHANGELOG LOCK QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
