"""EU-330 — the retired "drilling" cockpit_state key must be fully gone.

All drill entry points (the cockpit ``/api/drill`` POST + ``/drill`` page in ``server.py``) were
removed across the EU-324 sub-tickets, leaving ``cockpit_state``'s "drilling" run-flag dead. This
ticket drops the key + its default from the state schema (``_STATE_KEYS`` and ``_new_state()``) and
deletes the now-orphaned server routes that were the only readers/writers of the flag. Covers:
  - AC#1/#2: ``_STATE_KEYS`` / ``_new_state()`` / ``get_state()`` snapshots carry no "drilling" entry.
  - AC#1 (repo-wide): a grep for "drilling" across ``orchestrator/`` returns ZERO hits. This is a
    HARD check — a surviving reference (e.g. a drill route still in server.py) FAILS the harness
    rather than warning-and-continuing, so the ticket cannot land while any reader/writer remains.
"""
import subprocess
import sys
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import cockpit_state

ROOT = Path(__file__).resolve().parent.parent

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# --- AC#1/#2: the schema + a fresh state dict carry no "drilling" key/default ---
chk("_STATE_KEYS has no 'drilling' entry", "drilling" not in cockpit_state._STATE_KEYS,
    str(cockpit_state._STATE_KEYS))
fresh = cockpit_state._new_state()
chk("_new_state() dict has no 'drilling' key", "drilling" not in fresh, str(sorted(fresh)))

cockpit_state.reset_run_state()
st = cockpit_state.get_state("automatixy")
chk("get_state(app) snapshot has no 'drilling' key", "drilling" not in st, str(sorted(st)))
st_default = cockpit_state.get_state()
chk("get_state() default snapshot has no 'drilling' key", "drilling" not in st_default, str(sorted(st_default)))

# --- AC#1: repo-wide grep, HARD. Any "drilling" hit anywhere under orchestrator/ (its readers were
# the cockpit drill routes; those are now deleted) fails the harness. No warn-and-continue. ---
grep = subprocess.run(
    ["grep", "-rn", "--include=*.py", "drilling", "orchestrator/"],
    cwd=str(ROOT), capture_output=True, text=True)
hits = [ln for ln in grep.stdout.splitlines() if ln.strip()]
chk("grep -rn 'drilling' orchestrator/ returns zero hits", not hits,
    "; ".join(hits) if hits else "")

print("\n============ EU-330 DRILLING KEY REMOVAL QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
