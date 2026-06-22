"""EU-18 (DevOps slice): before every build, a (possibly reused) worktree is re-pinned to DEV —
bun.lock is restored from origin/<base> and `bun install --frozen-lockfile` runs. Guards against a
prior ticket's dependency drift leaking into the next one via the gitignored node_modules."""
import sys, types, tempfile
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.loop as loop
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


class FakeGit:
    def __init__(self, workdir, isolated=True, base_ref="origin/DEV"):
        self.workdir = str(workdir)
        self.isolated = isolated
        self.base_ref = base_ref


def _capture():
    """Patch subprocess.run in loop to record argv, returning success."""
    calls = []
    class R:
        returncode = 0
        stdout = ""
        stderr = ""
    def fake_run(argv, *a, **k):
        calls.append((list(argv), k.get("cwd")))
        return R()
    return calls, fake_run


app = AppConfig(name="automatixy", repo_path=".", base_branch="DEV",
                protected_branch="MAIN", backlog_backend="none")

# ---- (1) reused worktree with a bun project -> restore lock + frozen install ----
wt = Path(tempfile.mkdtemp())
(wt / "bun.lock").write_text("# lock")
(wt / "package.json").write_text("{}")
cfg = Config(apps=[app], audit_path="/tmp/x.jsonl", use_worktree=True)
git = FakeGit(wt)
calls, fake = _capture()
orig = loop.subprocess.run
loop.subprocess.run = fake
try:
    loop._repin_worktree_deps(cfg, app, git)
finally:
    loop.subprocess.run = orig

restore = [c for c in calls if c[0][:2] == ["git", "checkout"]]
install = [c for c in calls if c[0][:2] == ["bun", "install"]]
chk("restores bun.lock from base_ref", restore and restore[0][0] == ["git", "checkout", "origin/DEV", "--", "bun.lock"],
    str(restore[0][0]) if restore else "no checkout call")
chk("restore runs in the worktree", restore and restore[0][1] == str(wt))
chk("runs bun install --frozen-lockfile", install and "--frozen-lockfile" in install[0][0],
    str(install[0][0]) if install else "no install call")
chk("frozen install runs in the worktree", install and install[0][1] == str(wt))

# ---- (2) worktree mode OFF -> no-op (in-tree shares the user's checkout) ----
cfg_off = Config(apps=[app], audit_path="/tmp/x.jsonl", use_worktree=False)
calls, fake = _capture()
loop.subprocess.run = fake
try:
    loop._repin_worktree_deps(cfg_off, app, FakeGit(wt))
finally:
    loop.subprocess.run = orig
chk("use_worktree off -> no commands run", calls == [], str(calls))

# ---- (3) non-isolated git -> no-op ----
calls, fake = _capture()
loop.subprocess.run = fake
try:
    loop._repin_worktree_deps(cfg, app, FakeGit(wt, isolated=False))
finally:
    loop.subprocess.run = orig
chk("non-isolated git -> no commands run", calls == [], str(calls))

# ---- (4) not a bun project (no lockfile/manifest) -> no-op ----
empty = Path(tempfile.mkdtemp())
calls, fake = _capture()
loop.subprocess.run = fake
try:
    loop._repin_worktree_deps(cfg, app, FakeGit(empty))
finally:
    loop.subprocess.run = orig
chk("no bun.lock/package.json -> no commands run", calls == [], str(calls))

print("\n============== WORKTREE REPIN QA ==============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
