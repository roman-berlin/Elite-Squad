"""Git-race guard QA: while the Commander is resolving a rebase/merge by hand in a local checkout, the
autopilot detects it and stands down for the cycle — so it never ff-pushes origin/<base> and turns his
`git pull --rebase` into a non-fast-forward (the conflict he hit three times). The markers are local, so
the check no-ops on the unattended server."""
import sys, types, tempfile
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

from orchestrator import autopilot
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

def repo_with_git(tmp: Path) -> Path:
    (tmp / ".git").mkdir(parents=True, exist_ok=True)
    return tmp

tmp = Path(tempfile.mkdtemp())
repo = repo_with_git(tmp)
cfg = Config(apps=[AppConfig(name="Elite-Unit", repo_path=str(repo), base_branch="dev",
                             protected_branch="main", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)

chk("clean repo → not mid-git (autopilot free to land)", autopilot._commander_mid_git(cfg) is None)

# a rebase in progress (the exact state Roman was stuck in)
(repo / ".git" / "rebase-merge").mkdir()
chk("mid-rebase (.git/rebase-merge) → holds, names the repo", autopilot._commander_mid_git(cfg) == "Elite-Unit")
(repo / ".git" / "rebase-merge").rmdir()
chk("rebase cleared → autopilot resumes", autopilot._commander_mid_git(cfg) is None)

# a merge in progress
(repo / ".git" / "MERGE_HEAD").write_text("abc\n")
chk("mid-merge (.git/MERGE_HEAD) → holds", autopilot._commander_mid_git(cfg) == "Elite-Unit")
(repo / ".git" / "MERGE_HEAD").unlink()

# a worktree checkout (.git is a FILE, not a dir) must not false-positive
wt = Path(tempfile.mkdtemp())
(wt / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n")
cfg_wt = Config(apps=[AppConfig(name="wt", repo_path=str(wt), base_branch="dev", protected_branch="main",
                                backlog_backend="none")], audit_path=str(wt / "a.jsonl"), use_worktree=False)
chk("a worktree (.git is a file) never false-positives", autopilot._commander_mid_git(cfg_wt) is None)

# multiple apps: the busy one is found even if it's not first
clean2 = repo_with_git(Path(tempfile.mkdtemp()))
busy2 = repo_with_git(Path(tempfile.mkdtemp()))
(busy2 / ".git" / "CHERRY_PICK_HEAD").write_text("def\n")
cfg2 = Config(apps=[AppConfig(name="clean", repo_path=str(clean2), base_branch="DEV", protected_branch="MAIN", backlog_backend="none"),
                    AppConfig(name="busy", repo_path=str(busy2), base_branch="DEV", protected_branch="MAIN", backlog_backend="none")],
              audit_path=str(clean2 / "a.jsonl"), use_worktree=False)
chk("scans all configured repos, not just the first", autopilot._commander_mid_git(cfg2) == "busy")

# server-side (no local checkout path / missing repo) → no false hold
cfg_missing = Config(apps=[AppConfig(name="ghost", repo_path=str(tmp / "does-not-exist"), base_branch="dev",
                                     protected_branch="main", backlog_backend="none")],
                     audit_path=str(tmp / "a.jsonl"), use_worktree=False)
chk("a missing/remote repo path never holds (no-ops on the server)", autopilot._commander_mid_git(cfg_missing) is None)

print("\n============ GIT-RACE GUARD QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
