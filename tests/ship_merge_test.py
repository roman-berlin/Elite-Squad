"""Ship (promote_app) QA: a REAL merge of DEV into MAIN — MAIN keeps its own commits (e.g. earlier PR
merges), DEV's commits are added — done in a throwaway worktree so a DIRTY checkout and DIVERGED
branches both work. This is the case the old fast-forward Ship could never handle."""
import sys, types, tempfile, subprocess, os
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")
os.environ["GENERAL_COCKPIT_PROMOTE"] = "1"

from orchestrator import sync
from orchestrator.config import AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

def sh(cwd, *a): subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)
def out(cwd, *a): return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True).stdout.strip()

# --- a DIVERGED app repo (like Automatixy: MAIN has its own "PR merge" commits) + a DIRTY tree ---
root = Path(tempfile.mkdtemp())
origin = root / "o.git"; subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
work = root / "w"; subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
sh(work, "config", "user.email", "t@t"); sh(work, "config", "user.name", "t")
sh(work, "checkout", "-b", "MAIN")
(work / "base.txt").write_text("base\n"); sh(work, "add", "-A"); sh(work, "commit", "-m", "baseline")
sh(work, "push", "-u", "origin", "MAIN")
sh(work, "checkout", "-b", "DEV")
(work / "feature.txt").write_text("dev feature\n"); sh(work, "add", "-A"); sh(work, "commit", "-m", "AUTO-14: feature")
(work / "feature2.txt").write_text("dev feature2\n"); sh(work, "add", "-A"); sh(work, "commit", "-m", "AUTO-15: more")
sh(work, "push", "-u", "origin", "DEV")
# a commit that lives ONLY on MAIN (a production hotfix / earlier PR merge) -> divergence
sh(work, "checkout", "MAIN")
(work / "hotfix.txt").write_text("prod hotfix\n"); sh(work, "add", "-A"); sh(work, "commit", "-m", "Merge pull request #43 from DEV")
sh(work, "push", "origin", "MAIN")
sh(work, "checkout", "DEV")
# dirty the working tree (Automatixy is full of test artifacts)
(work / "base.txt").write_text("base + uncommitted churn\n")

chk("setup: DEV and MAIN have diverged (the old ff Ship would reject this)",
    int(out(work, "rev-list", "--count", "origin/DEV..origin/MAIN")) >= 1
    and int(out(work, "rev-list", "--count", "origin/MAIN..origin/DEV")) >= 2)

app = AppConfig(name="automatixy", repo_path=str(work), base_branch="DEV", protected_branch="MAIN",
                backlog_backend="none")
main_before = out(work, "rev-parse", "origin/MAIN")
r = sync.promote_app(app)
chk("ship ok — real merge of DEV into MAIN", r.get("ok"), str(r))

# refresh remote-tracking refs to inspect origin/MAIN after the push
sh(work, "fetch", "origin")
main_after = out(work, "rev-parse", "origin/MAIN")
chk("origin/MAIN advanced", main_after != main_before)
# MAIN now contains BOTH dev's commits AND its own hotfix
files_on_main = out(work, "ls-tree", "-r", "--name-only", "origin/MAIN")
chk("MAIN kept its own commit (hotfix.txt still there)", "hotfix.txt" in files_on_main)
chk("DEV's work is now on MAIN (feature.txt + feature2.txt)",
    "feature.txt" in files_on_main and "feature2.txt" in files_on_main)
chk("the merge is a real merge commit (2 parents)",
    len(out(work, "rev-list", "--parents", "-1", "origin/MAIN").split()) == 3)
chk("nothing left to ship now", sync.app_promote_status(app)["ahead"] == 0)

# --- the user's checkout was never touched ---
chk("user still on DEV", out(work, "rev-parse", "--abbrev-ref", "HEAD") == "DEV")
chk("user's dirty change preserved (worktree untouched)",
    "uncommitted churn" in (work / "base.txt").read_text())
chk("no leftover ship worktrees", "general-ship-" not in out(work, "worktree", "list"))

# --- already in sync -> ok, no-op ---
r2 = sync.promote_app(app)
chk("re-ship when in sync -> ok, nothing to do", r2.get("ok") and r2.get("ahead_before") == 0)

print("\n================ SHIP (real merge) QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
