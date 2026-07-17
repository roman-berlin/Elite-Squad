"""EU-362 — /api/terminal must bound its memory while reading, and its group sweep must fire.

Two of the three resource leaks in the 2026-07-16 audit's EU-362 batch live in
``orchestrator/server.py`` and are pinned here. (The other two — the per-request Workspace leak in
``cockpit_state.workspace_for`` and the unbounded ``needs._SUMMARY_CACHE`` — live in files this
change does not own; EU-362 stays open for them.)

  1. ``proc.communicate()`` read the subprocess's ENTIRE stdout/stderr into memory and only THEN
     applied the 64KB cap, so one output-heavy command typed into the cockpit terminal ballooned
     the resident serve process by however much it printed. The cap now applies while reading:
     past 64KB the output is still drained (a full pipe would block the child forever) but never
     retained.

  2. The ``finally`` "final sweep" was dead code twice over. It was guarded by
     ``proc.poll() is None``, which is always false on the success path — the top shell has exited
     by the time ``communicate()`` returns — and even without that guard
     ``os.getpgid(proc.pid)`` raises ProcessLookupError once the process is reaped, so the
     ``killpg`` it advertises as catching "a backgrounded grandchild" could never run at all. The
     pgid is now captured at spawn, while the child is definitely alive.

Soft ``k/n passed`` tally so ``tests/run_all.py`` (the EU-44 gate) judges it honestly.
"""
import os
import sys
import tempfile
import tracemalloc
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda *a, **k: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import server  # noqa: E402
from orchestrator.config import AppConfig, Config  # noqa: E402

results: list[tuple[str, bool, str]] = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
(tmp / "audit.jsonl").write_text("", encoding="utf-8")
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()

CAP = 65536


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 1) The cap applies WHILE READING — an 8MB command must not cost 8MB of process memory
# ══════════════════════════════════════════════════════════════════════════════════════════════
# 8MB of output against a 64KB cap: the pre-fix slurp shows up as a multi-megabyte tracemalloc
# peak (communicate() materialises the whole thing as one str before the cap is applied); the
# capped reader never holds more than the cap plus a chunk. The 2MB threshold sits an order of
# magnitude away from both, so this measures the leak, not allocator noise.
_BIG = 8 * 1024 * 1024
tracemalloc.start()
_before = tracemalloc.get_traced_memory()[0]
resp = client.post("/api/terminal", data={"cmd": f"python3 -c \"print('A' * {_BIG}, end='')\""})
_peak = tracemalloc.get_traced_memory()[1] - _before
tracemalloc.stop()

chk("large output: HTTP 200", resp.status_code == 200, f"status={resp.status_code}")
j = resp.get_json() or {}
chk("large output: response is still capped at 64KB + marker",
    len(j.get("output", "")) <= CAP + len("\n... (output truncated)"),
    f"len={len(j.get('output', ''))}")
chk("large output: truncation marker present", "... (output truncated)" in j.get("output", ""),
    str(j)[:120])
chk("8MB of output never lands in memory — the cap applies while reading, not after (EU-362/1)",
    _peak < 2 * 1024 * 1024,
    f"peak={_peak / 1024 / 1024:.1f}MB for an 8MB command — the whole output was buffered first")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 2) The finally sweep actually fires on the ordinary success path
# ══════════════════════════════════════════════════════════════════════════════════════════════
_killpg_calls: list[tuple] = []
_real_killpg = os.killpg

def _spy_killpg(pgid, sig):
    _killpg_calls.append((pgid, sig))
    # Raise instead of delegating: on the success path the group is empty anyway, and a real
    # SIGKILL to a recycled pgid is the one thing this harness must never risk.
    raise ProcessLookupError("stubbed by eu362_terminal_bounds_test")

os.killpg = _spy_killpg
try:
    r = client.post("/api/terminal", data={"cmd": "echo hi"})
finally:
    os.killpg = _real_killpg

chk("fast command still returns its output unchanged (EU-146 contract)",
    r.status_code == 200 and (r.get_json() or {}).get("output") == "hi\n", str(r.get_json()))
chk("the process-group sweep FIRES on the success path (EU-362/2)",
    len(_killpg_calls) >= 1,
    "killpg was never called — the finally sweep is dead code, a backgrounded grandchild survives")
chk("the sweep SIGKILLs the group (EU-362/2)",
    all(sig == 9 for _, sig in _killpg_calls), str(_killpg_calls))
chk("the sweep targets a real pgid captured at spawn, not a reaped pid (EU-362/2)",
    all(isinstance(pg, int) and pg > 0 for pg, _ in _killpg_calls), str(_killpg_calls))


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 3) Existing /api/terminal guards are untouched
# ══════════════════════════════════════════════════════════════════════════════════════════════
r = client.post("/api/terminal", data={"cmd": ""})
chk("empty cmd: HTTP 400 (EU-149 contract)", r.status_code == 400, f"status={r.status_code}")
r = client.post("/api/terminal", data={"cmd": "echo bad\nrm -rf /"})
chk("newline in cmd: HTTP 400 (EU-149 contract)", r.status_code == 400, f"status={r.status_code}")
r = client.post("/api/terminal", data={"cmd": "echo out; echo err 1>&2"})
chk("stderr is still folded into the returned output (EU-149 contract)",
    "out" in (r.get_json() or {}).get("output", "") and "err" in (r.get_json() or {}).get("output", ""),
    str(r.get_json()))

print("\n============ EU-362 TERMINAL RESOURCE-BOUNDS QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
