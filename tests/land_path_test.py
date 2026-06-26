"""EU-45 (F8) land-path QA: the trial-merge → validate → ff-push-to-origin/<base> sequence is the ONLY
moment DEV changes and the single hardest-to-reverse operation in the system (it pushes to a shared
branch), yet it had zero regression coverage — a repo-wide grep for trial_merge/land_trial/abandon_trial/
merge_no_ff found no test references. This hermetic harness drives the REAL Git class over a temp bare
origin + a real linked worktree (the same isolated/use_worktree topology the unit runs in production, as
sync_test.py does) and pins the three behaviours the ticket calls out:

  • a CLEAN trial in isolated mode fast-forward-pushes origin/<base> forward (DEV advances to the exact
    validated merge commit, old DEV an ancestor — no rewrite) and retires the throwaway trial + the
    feature branch, while nothing else on origin moves (the protected branch is never touched);
  • a CONFLICTING trial leaves origin/<base> byte-for-byte untouched (the PR path is taken precisely
    because trial_merge returned False) and abandon_trial cleans up before the feature branch is pushed;
  • the never-touch-the-protected-branch guards fire: land_trial raises when base == protected
    (git_ops.py:241-242), merge_no_ff raises when the protected branch is checked out, and Git() itself
    refuses base == protected at construction.

Every check is a hard assertion over real git state, so a regression in the land/guard logic flips a
check to FAIL and run_all marks the harness red (the F1 dependency this ticket rests on) — not a no-op green.
"""
import sys, types, tempfile, subprocess
from pathlib import Path

# stub the Agent SDK so importing orchestrator.* is cheap + offline (house pattern; see sibling tests)
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator.git_ops import Git, GitError

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

def G(cwd, *a):
    subprocess.run(["git", *a], cwd=str(cwd), check=True, capture_output=True, text=True)
def out(cwd, *a):
    return subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True).stdout.strip()
def is_ancestor(cwd, anc, desc):
    """True iff `anc` is an ancestor of `desc` — i.e. the move from anc→desc was a fast-forward."""
    return subprocess.run(["git", "merge-base", "--is-ancestor", anc, desc],
                          cwd=str(cwd), capture_output=True, text=True).returncode == 0

tmp = Path(tempfile.mkdtemp())

def seed_origin(name: str) -> Path:
    """A bare origin carrying `main` (protected) and `dev` (base), both at one baseline commit."""
    origin = tmp / f"{name}.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    seed = tmp / f"{name}_seed"
    G(tmp, "clone", str(origin), str(seed))
    G(seed, "config", "user.email", "t@t"); G(seed, "config", "user.name", "t")
    (seed / "base.txt").write_text("base\n")
    G(seed, "add", "-A"); G(seed, "commit", "-m", "baseline")
    G(seed, "push", "origin", "HEAD:main")
    G(seed, "checkout", "-b", "dev"); G(seed, "push", "origin", "dev")
    return origin

def isolated(name: str):
    """Build a real isolated-mode Git: a user 'main' checkout + a dedicated linked worktree on
    detached origin/dev — exactly the production (use_worktree) topology."""
    origin = seed_origin(name)
    main_co = tmp / f"{name}_main"
    G(tmp, "clone", str(origin), str(main_co))
    G(main_co, "config", "user.email", "t@t"); G(main_co, "config", "user.name", "t")
    wt = tmp / f"{name}_wt"
    g = Git(repo_path=str(main_co), base_branch="dev", protected_branch="main", worktree_path=str(wt))
    fresh = g.setup()
    return g, origin, main_co, fresh

# ===================================================================================== #
# 1) CLEAN trial → land: origin/dev fast-forwards, trial + feature branches retired
# ===================================================================================== #
g, origin, main_co, fresh = isolated("clean")
chk("isolated setup created a fresh linked worktree on origin/dev", fresh and (Path(g.workdir) / ".git").exists())

dev_before = out(origin, "rev-parse", "dev")
main_before = out(origin, "rev-parse", "main")

g.checkout_feature("feat/eu-45")
(Path(g.workdir) / "feature.txt").write_text("the landed feature\n")
sha = g.commit_all("EU-45: add feature")
chk("commit_all produced a feature commit", bool(sha), str(sha))

temp = "feat/_trial"
clean = g.trial_merge("feat/eu-45", temp, "Merge feat/eu-45 into dev (EU-45)")
chk("clean trial_merge returns True", clean)
chk("trial_merge did NOT touch origin/dev yet (no push until land)", out(origin, "rev-parse", "dev") == dev_before)

merge_sha = g.current_sha()   # the validated --no-ff merge commit on the trial (loop.py lands THIS)
g.land_trial(temp)            # the ONLY moment DEV changes
g.delete_local_branch("feat/eu-45")   # loop retires the feature branch after a land

dev_after = out(origin, "rev-parse", "dev")
chk("land_trial advanced origin/dev (DEV moved)", dev_after != dev_before)
chk("origin/dev advanced by fast-forward only (old DEV is an ancestor — no history rewrite)",
    is_ancestor(origin, dev_before, dev_after))
chk("the validated trial merge commit was landed verbatim onto origin/dev (ff, no rewrite)",
    dev_after == merge_sha, f"{dev_after} vs {merge_sha}")
chk("landed feature is now on origin/dev", "feature.txt" in out(origin, "ls-tree", "-r", "--name-only", "dev"))
chk("landed merge has the feature commit as an ancestor", sha in out(origin, "rev-list", "dev"))
chk("land_trial retired the throwaway trial branch", not out(g.workdir, "branch", "--list", temp))
chk("feature branch retired after land", not out(g.workdir, "branch", "--list", "feat/eu-45"))
chk("land NEVER touched the protected branch (origin/main unchanged)", out(origin, "rev-parse", "main") == main_before)

# ===================================================================================== #
# 2) CONFLICTING trial → base untouched, PR path taken (trial_merge False)
# ===================================================================================== #
g2, origin2, main2, _ = isolated("conflict")
g2.checkout_feature("feat/conflict")
(Path(g2.workdir) / "shared.txt").write_text("FEATURE side\n")
g2.commit_all("EU-45: feature edit to shared.txt")

# advance origin/dev with a CONFLICTING edit to the same new path (from the user's main checkout)
G(main2, "fetch", "origin", "dev")
G(main2, "checkout", "-B", "advance", "origin/dev")
(main2 / "shared.txt").write_text("BASE side\n")
G(main2, "add", "-A"); G(main2, "commit", "-m", "concurrent base edit")
G(main2, "push", "origin", "advance:dev")
G(main2, "checkout", "main")
dev_pinned = out(origin2, "rev-parse", "dev")

temp2 = "feat/_trial"
clean2 = g2.trial_merge("feat/conflict", temp2, "Merge feat/conflict into dev")
chk("conflicting trial_merge returns False (cannot merge cleanly)", not clean2)
chk("conflicting trial left origin/dev untouched (base safe)", out(origin2, "rev-parse", "dev") == dev_pinned)
chk("origin/dev still holds the BASE side of the conflict, not the feature's",
    out(origin2, "show", "dev:shared.txt") == "BASE side")

g2.abandon_trial(temp2)       # PR path: throw the trial away, base unchanged
chk("abandon_trial deleted the trial branch", not out(g2.workdir, "branch", "--list", temp2))
chk("abandon_trial left the worktree on detached origin/dev",
    out(g2.workdir, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD")
chk("abandon_trial still left origin/dev untouched", out(origin2, "rev-parse", "dev") == dev_pinned)
g2.push("feat/conflict")      # the loop pushes the feature branch and opens the PR
chk("feature branch pushed for the PR path",
    bool(out(main2, "ls-remote", "--heads", str(origin2), "feat/conflict")))

# ===================================================================================== #
# 3) never-touch-the-protected-branch guards
# ===================================================================================== #
# (a) land_trial refuses when base == protected (git_ops.py:241-242). The constructor normally forbids
# this, so we force the invariant to prove the in-method guard is real defense-in-depth.
gp = Git(repo_path=str(main_co), base_branch="dev", protected_branch="main", worktree_path=str(tmp / "clean_wt"))
gp.base = "main"; gp.protected = "main"   # simulate a misconfig pointing land at the protected branch
raised = False
try:
    gp.land_trial("whatever")
except GitError as e:
    raised = "protected" in str(e).lower()
chk("land_trial raises GitError when base == protected", raised)

# (b) merge_no_ff refuses when the protected branch is the one checked out (in-tree mode)
intree = tmp / "intree"
G(tmp, "clone", str(seed_origin("intree_origin")), str(intree))
G(intree, "config", "user.email", "t@t"); G(intree, "config", "user.name", "t")
G(intree, "checkout", "main")
gi = Git(repo_path=str(intree), base_branch="dev", protected_branch="main")   # in-tree (no worktree)
raised2 = False
try:
    gi.merge_no_ff("dev", "should never merge into main")
except GitError as e:
    raised2 = "protected" in str(e).lower()
chk("merge_no_ff raises GitError when the protected branch is checked out", raised2)

# (c) Git() itself refuses to be configured with base == protected
raised3 = False
try:
    Git(repo_path=str(intree), base_branch="main", protected_branch="main")
except GitError:
    raised3 = True
chk("Git() refuses base == protected at construction", raised3)

print("\n================ EU-45 LAND-PATH QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
