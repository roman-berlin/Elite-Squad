"""EU-358 audit / guard: the dead `orchestrator/reaper.py` stays deleted.

`orchestrator/reaper.py` was a zero-importer, pre-EU-334 near-duplicate of
`git_ops.reap_stale_worktrees` that still carried the OLD self-reap crash (it lacked the
current_wt_root / stable-cwd self-exclusion). The live reaper is `git_ops.reap_stale_worktrees`
(pinned by reaper_test.py + reaper_self_reap_test.py). Deleting the duplicate removed a stale copy
that a future edit could have wired up by accident, reintroducing the EU-334 base-worktree self-reap.

This guard fails if the duplicate module comes back OR if anything starts importing it — so the one
canonical reaper implementation can't silently fork again.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
results: list[tuple[str, bool, str]] = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


# 1) the dead module file must not exist
chk("orchestrator/reaper.py stays deleted", not (ROOT / "orchestrator" / "reaper.py").exists())

# 2) nothing imports the dead module (a stray `from orchestrator import reaper` / `import
#    orchestrator.reaper` would resurrect the fork by reference). The live callers use
#    `git_ops.reap_stale_worktrees`, which contains no substring match for these patterns.
bad_patterns = ("from orchestrator import reaper", "import orchestrator.reaper",
                "from .reaper", "from orchestrator.reaper")
offenders: list[str] = []
for py in list((ROOT / "orchestrator").glob("*.py")) + list((ROOT / "tests").glob("*.py")):
    if py.name == Path(__file__).name:
        continue
    text = py.read_text(encoding="utf-8", errors="replace")
    for pat in bad_patterns:
        if pat in text:
            offenders.append(f"{py.name}: {pat}")
chk("no module imports the deleted orchestrator/reaper", not offenders, "; ".join(offenders))

# 3) the canonical implementation is still present where the live callers expect it
try:
    sys.path.insert(0, str(ROOT))
    from orchestrator import git_ops
    chk("git_ops.reap_stale_worktrees (the canonical reaper) exists",
        callable(getattr(git_ops, "reap_stale_worktrees", None)))
except Exception as exc:  # noqa: BLE001
    chk("git_ops import for canonical-reaper check", False, repr(exc))

print("\n============ EU-358 REAPER-DUPE GUARD ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
