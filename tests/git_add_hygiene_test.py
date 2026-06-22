"""F14 (EU-14) git add hygiene QA: diff_against_base/commit_all run `git add -A`, which stages every
untracked, non-ignored path. The safety against stray builder artifacts reaching the feature branch -> DEV
rests ENTIRELY on the target repo's .gitignore. This harness verifies that reliance end-to-end:
  • a stray artifact COVERED by .gitignore (created mid-build) never lands in the diff OR the commit, while
  • a legitimately-new source file DOES (the trade-off: `add -A` is what surfaces intended new files).
It also pins the documentation so the reliance can't be silently dropped from git_ops.py."""
import sys, types, tempfile, subprocess
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator.git_ops import Git

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

def sh(cwd, *a): subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)
def out(cwd, *a): return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True).stdout.strip()

# --- a real repo with a solid .gitignore on the base branch (in-tree mode) ---
work = Path(tempfile.mkdtemp())
sh(work, "init")
sh(work, "config", "user.email", "t@t"); sh(work, "config", "user.name", "t")
sh(work, "checkout", "-b", "dev")
(work / ".gitignore").write_text("node_modules/\ndist/\nbuild/\n__pycache__/\n*.log\n.env\n")
(work / "base.txt").write_text("base\n")
sh(work, "add", "-A"); sh(work, "commit", "-m", "baseline + .gitignore")

g = Git(repo_path=str(work), base_branch="dev", protected_branch="main")
g.checkout_feature("feat/eu-14")

# legit NEW source file the build is supposed to surface
(work / "src").mkdir()
(work / "src" / "new_feature.py").write_text("def feature():\n    return 1\n")
# stray builder artifacts created MID-BUILD, all covered by .gitignore
(work / "node_modules").mkdir(); (work / "node_modules" / "junk.js").write_text("// vendored\n")
(work / "dist").mkdir(); (work / "dist" / "bundle.js").write_text("// built\n")
(work / "__pycache__").mkdir(); (work / "__pycache__" / "x.pyc").write_text("cache\n")
(work / "build.log").write_text("noisy build output\n")
(work / ".env").write_text("SECRET=should-never-commit\n")

# --- the review diff: legit file in, every ignored artifact out ---
diff = g.diff_against_base()
chk("diff surfaces the legit new source file", "src/new_feature.py" in diff, diff[:200])
for stray in ["node_modules/junk.js", "dist/bundle.js", "__pycache__/x.pyc", "build.log", ".env"]:
    chk(f"diff EXCLUDES stray ignored artifact: {stray}", stray not in diff)

# --- the commit: same guarantee on what actually lands on the branch ---
sha = g.commit_all("EU-14: add feature")
chk("commit_all produced a commit", bool(sha), str(sha))
committed = out(work, "ls-tree", "-r", "--name-only", "HEAD")
chk("commit contains the legit new source file", "src/new_feature.py" in committed)
for stray in ["node_modules/junk.js", "dist/bundle.js", "__pycache__/x.pyc", "build.log", ".env"]:
    chk(f"commit does NOT contain stray ignored artifact: {stray}", stray not in committed)

# --- prove the reliance is REAL: an UN-ignored stray DOES get committed (this is why .gitignore matters) ---
(work / "stray_untracked.tmp").write_text("not ignored -> add -A will stage it\n")
sha2 = g.commit_all("EU-14: second")
committed2 = out(work, "ls-tree", "-r", "--name-only", "HEAD")
chk("an un-ignored stray IS committed (dependency on .gitignore is real, not incidental)",
    "stray_untracked.tmp" in committed2, committed2)

# --- the reliance is documented in git_ops.py (guard against silent removal) ---
src = Path("./orchestrator/git_ops.py").read_text()
chk("git_ops.py documents the .gitignore reliance", ".gitignore" in src and "add -A" in src)
chk("git_ops.py explains why add -A is kept (trade-off)", "narrowing" in src.lower())

print("\n================ F14 GIT ADD HYGIENE QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
