"""2026-07-21 (VPS incident): EU-386's dirty-tree guard must not let INERT junk pin a host to
stale code.

Two harmless leftovers on the server — a timestamped config backup (`config.yaml.bak-<ts>`, which
the old `*.bak` ignore pattern missed) and a pre-rename officer charter — made EVERY self-update
restart REFUSE. The safety guard had become the EU-335 stale-code bug: the VPS could never respawn
onto its own landed code, with only a warning to show for it.

Contract now:
  · tree_forensics() REPORTS everything (the boot warning stays honest);
  · respawn_blocking_paths() blocks only what could change behaviour — any TRACKED modification,
    or an UNTRACKED file with an executable suffix;
  · _maybe_self_restart consults the blocking view, not the reporting view;
  · .gitignore covers timestamped backups so they never reach the tree in the first place.
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
sys.path.insert(0, ".")

from orchestrator import autopilot as ap

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

_orig = subprocess.run
def _stub(out):
    return lambda cmd, *a, **k: types.SimpleNamespace(returncode=0, stdout=out, stderr="")

def _probe(porcelain):
    subprocess.run = _stub(porcelain)
    try:
        return ap.tree_forensics(), ap.respawn_blocking_paths()
    finally:
        subprocess.run = _orig

# (1) the exact VPS case
(dirty, paths), block = _probe("?? config.yaml.bak-1783537662\n?? officers/frontend-engineer.md\n")
chk("(1a) inert untracked junk still REPORTS dirty (boot warning stays honest)",
    dirty is True and len(paths) == 2, str(paths))
chk("(1b) …and does NOT block the self-update respawn", block == [], str(block))

# (2) untracked code CAN change behaviour → still blocks
for suffix in (".py", ".sh", ".js"):
    (_d, _p), b = _probe(f"?? orchestrator/stray{suffix}\n")
    chk(f"(2) an untracked {suffix} file blocks the respawn", b == [f"orchestrator/stray{suffix}"], str(b))

# (3) tracked modifications — the original EU-386 contract, unchanged
(_d3, _p3), block3 = _probe(" M orchestrator/loop.py\nA  tests/new_test.py\nD  officers/old.md\n")
chk("(3) every tracked modification still blocks (un-gated WIP)", len(block3) == 3, str(block3))

# (4) a clean tree blocks nothing and reports nothing
(d4, p4), block4 = _probe("")
chk("(4) clean tree: no report, no block", d4 is False and p4 == [] and block4 == [])

# (5) the refusal site consults the BLOCKING view
src = Path("orchestrator/autopilot.py").read_text(encoding="utf-8")
chk("(5) _maybe_self_restart gates on respawn_blocking_paths()",
    "paths = respawn_blocking_paths()" in src and "dirty, paths = tree_forensics()\n    if dirty:" not in src)

# (6) timestamped backups can't reach the tree at all
gi = Path(".gitignore").read_text(encoding="utf-8")
chk("(6) .gitignore covers timestamped backups (*.bak-*)", "*.bak-*" in gi)

print("\n========== RESPAWN-BLOCKING QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
