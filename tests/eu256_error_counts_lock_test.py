"""EU-274: save_error_counts must route through locking.locked_rmw (auto-split from EU-256).

Forensics: save_error_counts was a bare write_text(json.dumps(counts)) — a whole-dict overwrite of
a snapshot taken at load time. Because each drain thread's write carries a full-dict snapshot from
its own load, a concurrent write from the OTHER app's drain clobbers increments made in between: a
lost increment resets progress toward _MAX_TICKET_ERRORS, costing an extra full-build retry. Live
example: error_counts.json = {"EU-151": 1} got erased by a stale automatixy-drain write.

The fix must not just take a lock around the overwrite (that only serialises writes, it does not
refresh the stale snapshot) — it must re-read the on-disk value under the lock and MERGE, exactly
like locked_rmw's read-modify-write contract, mirroring save_blocked's pattern.

Pins:
  1. save_error_counts performs no bare write_text; it writes exclusively through locking.locked_rmw,
     wrapped in try/except (OSError, ValueError): pass matching save_blocked.
  2. A stale snapshot missing a key that's fresh on disk (EU-151) does not erase that key.
  3. Two concurrent save_error_counts calls from disjoint stale snapshots over an initially-empty
     file produce a union of both keys, never just one (the actual park-after-3 defeat).
  4. load_error_counts / _MAX_TICKET_ERRORS / the mutate call sites / the save_error_counts
     signature are unchanged.
  5. This harness is picked up by tests/run_all.py's *_test.py glob.
"""
import inspect
import sys
import tempfile
import threading
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

from orchestrator import autopilot
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))
    print(("PASS: " if c else "FAIL: ") + n + (f" - {d}" if d and not c else ""))


tmp = Path(tempfile.mkdtemp())
app = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev", protected_branch="main",
                backlog_backend="jira", backlog={"base_url": "https://x.atlassian.net", "project_key": "AUTO"})
cfg = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)


# ============================================================================================== #
# AC1: save_error_counts routes through locking.locked_rmw, not a bare write_text
# ============================================================================================== #
print("\n=== AC1: save_error_counts uses locking.locked_rmw ===")
src = inspect.getsource(autopilot.save_error_counts)
chk("save_error_counts source calls locking.locked_rmw", "locked_rmw" in src, src)
chk("save_error_counts source has no bare write_text call", ".write_text(" not in src, src)
chk("save_error_counts source keeps the best-effort except clause",
    "except (OSError, ValueError)" in src, src)

# ============================================================================================== #
# AC2: a fresh on-disk key absent from a stale snapshot survives the write (the EU-151 example)
# ============================================================================================== #
print("\n=== AC2: stale snapshot does not erase a fresh cross-drain key ===")
ef = tmp / "ac2_error_counts.json"
app2 = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev", protected_branch="main",
                 backlog_backend="jira", backlog={"base_url": "https://x.atlassian.net", "project_key": "AUTO"})
cfg2 = Config(apps=[app2], audit_path=str(tmp / "ac2_audit.jsonl"), use_worktree=False)
# Seed the on-disk file directly with the fresh EU-151 count (as the other drain would have left it).
autopilot._error_counts_file(cfg2).write_text('{"EU-151": 1}')
# This drain's own stale snapshot never saw EU-151.
stale_counts = {"AUTO-9": 2}
autopilot.save_error_counts(cfg2, stale_counts)
after = autopilot.load_error_counts(cfg2)
chk("cross-drain EU-151 count survives a stale same-time write", after.get("EU-151") == 1, str(after))
chk("this drain's own increment is still recorded", after.get("AUTO-9") == 2, str(after))

# ============================================================================================== #
# AC3: two concurrent save_error_counts calls from disjoint stale snapshots -> union, not clobber
# ============================================================================================== #
print("\n=== AC3: concurrent drains -> union of both increments (park-after-3 defeat) ===")
ef3 = tmp / "ac3_error_counts.json"
app3 = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev", protected_branch="main",
                 backlog_backend="jira", backlog={"base_url": "https://x.atlassian.net", "project_key": "AUTO"})
cfg3 = Config(apps=[app3], audit_path=str(tmp / "ac3_audit.jsonl"), use_worktree=False)
# Nothing on disk yet -> both drains' "stale" snapshots start from {}.

results_lock = threading.Lock()
barrier = threading.Barrier(2)

def _drain_eu():
    barrier.wait()
    for _ in range(20):
        autopilot.save_error_counts(cfg3, {"EU-151": 1})

def _drain_auto():
    barrier.wait()
    for _ in range(20):
        autopilot.save_error_counts(cfg3, {"AUTO-5": 1})

t1 = threading.Thread(target=_drain_eu)
t2 = threading.Thread(target=_drain_auto)
t1.start(); t2.start()
t1.join(); t2.join()

final = autopilot.load_error_counts(cfg3)
chk("concurrent drains: both keys present in the final union", "EU-151" in final and "AUTO-5" in final,
    str(final))
chk("concurrent drains: EU-151's value is intact", final.get("EU-151") == 1, str(final))
chk("concurrent drains: AUTO-5's value is intact", final.get("AUTO-5") == 1, str(final))

# ============================================================================================== #
# AC5: cold-start intentional drop — a drain's FIRST-EVER save (no warmed cache) that no longer
#      tracks a same-project ticket X must actually delete X from disk, not resurrect it. This is
#      the realistic post-restart success/park: a prior incarnation left {"EU-151": 3} on disk, the
#      fresh drain loads it, EU-151 succeeds/parks (its key popped) while a sibling EU-152 keeps
#      erroring, and the first save carries counts WITHOUT EU-151. A concurrent OTHER-app key
#      (AUTO-7) present on disk must survive untouched. Run on a brand-new thread so any per-thread
#      write-cache is guaranteed cold.
# ============================================================================================== #
print("\n=== AC5: cold-start drop of a same-project key deletes it; foreign key preserved ===")
app5 = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev", protected_branch="main",
                 backlog_backend="jira", backlog={"base_url": "https://x.atlassian.net", "project_key": "AUTO"})
cfg5 = Config(apps=[app5], audit_path=str(tmp / "ac5_audit.jsonl"), use_worktree=False)
# Stale disk state from a previous incarnation: EU-151 erroring at 3, plus a foreign AUTO-7.
autopilot._error_counts_file(cfg5).write_text('{"EU-151": 3, "AUTO-7": 2}')

def _cold_first_save():
    # This drain owns the EU project; EU-151 succeeded/parked (dropped) but EU-152 still errors.
    autopilot.save_error_counts(cfg5, {"EU-152": 1})

th5 = threading.Thread(target=_cold_first_save)
th5.start(); th5.join()
after5 = autopilot.load_error_counts(cfg5)
chk("cold-start drop: stale same-project EU-151 is gone from disk (not resurrected)",
    "EU-151" not in after5, str(after5))
chk("cold-start drop: sibling EU-152 is recorded", after5.get("EU-152") == 1, str(after5))
chk("cold-start drop: foreign AUTO-7 (concurrent drain's) is preserved", after5.get("AUTO-7") == 2,
    str(after5))

# ============================================================================================== #
# AC6: warm drop to EMPTY counts — when a drain's LAST tracked ticket parks/succeeds, `counts` goes
#      empty and carries no project prefix, so the drop is propagated via the per-thread write memory
#      instead (this is the park-after-3 counter reset). A concurrent drain's key must still survive.
# ============================================================================================== #
print("\n=== AC6: warm drop to empty counts resets own key; foreign key preserved ===")
app6 = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev", protected_branch="main",
                 backlog_backend="jira", backlog={"base_url": "https://x.atlassian.net", "project_key": "EU"})
cfg6 = Config(apps=[app6], audit_path=str(tmp / "ac6_audit.jsonl"), use_worktree=False)

def _warm_then_empty():
    autopilot.save_error_counts(cfg6, {"EU-9": 2})           # warm this thread's write memory
    # A concurrent AUTO drain lands its own key on disk between our two writes.
    cur = autopilot.load_error_counts(cfg6); cur["AUTO-3"] = 1
    autopilot._error_counts_file(cfg6).write_text(__import__("json").dumps(cur))
    autopilot.save_error_counts(cfg6, {})                    # EU-9 parked -> counts empty

th6 = threading.Thread(target=_warm_then_empty)
th6.start(); th6.join()
after6 = autopilot.load_error_counts(cfg6)
chk("warm empty-counts drop: parked EU-9 is reset (gone from disk)", "EU-9" not in after6, str(after6))
chk("warm empty-counts drop: concurrent AUTO-3 is preserved", after6.get("AUTO-3") == 1, str(after6))

# ============================================================================================== #
# AC4: load_error_counts / _MAX_TICKET_ERRORS / save_error_counts signature are unchanged
# ============================================================================================== #
print("\n=== AC4: load-side and threshold constant untouched ===")
chk("_MAX_TICKET_ERRORS is still 3", autopilot._MAX_TICKET_ERRORS == 3, str(autopilot._MAX_TICKET_ERRORS))
sig = inspect.signature(autopilot.save_error_counts)
chk("save_error_counts signature unchanged: (cfg, counts)", list(sig.parameters) == ["cfg", "counts"],
    str(list(sig.parameters)))
load_ef = tmp / "ac4_error_counts.json"
app4 = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev", protected_branch="main",
                 backlog_backend="jira", backlog={"base_url": "https://x.atlassian.net", "project_key": "AUTO"})
cfg4 = Config(apps=[app4], audit_path=str(tmp / "ac4_audit.jsonl"), use_worktree=False)
autopilot._error_counts_file(cfg4).write_text('{"AUTO-1": 2}')
loaded = autopilot.load_error_counts(cfg4)
chk("load_error_counts still returns a plain str->int dict", loaded == {"AUTO-1": 2}, str(loaded))


print("\n================ EU-274 ERROR-COUNTS LOCK QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
