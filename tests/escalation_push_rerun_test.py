"""Re-run robustness of the escalation branch push (2026-07-07).

Seen live on AUTO-57 (audit event ``cleanup_push_failed``): when a ticket is RE-RUN after a prior
escalated run already pushed its throwaway ``autodev/<TICKET>`` branch, the cleanup push is rejected
non-fast-forward — the fresh local branch (cut from ``origin/<base>``) shares no ancestry with the
stale remote WIP, so ``git push`` refuses ("tip of your current branch is behind its remote
counterpart"). It degraded gracefully in the cleanup path (``cleanup_push_failed`` recorded, run_end
still fired) but hard-RAISED in the sibling PR path (loop.py:1412, ``git.push(branch)`` unwrapped).

Both call sites push the SAME unit-owned throwaway branch through ``Git.push``, so the fix lives
there: on a non-fast-forward rejection, overwrite the stale prior-run WIP with a genuine
``--force-with-lease=<branch>:<observed>`` — the tip is read via ``ls-remote`` right before the push
so the lease keeps its teeth (it refuses "stale info" if the ref moves before the push lands, rather
than clobbering blindly). The force path can NEVER reach ``base`` or the protected branch.

This hermetic harness drives the REAL Git class over a temp bare origin + a real linked worktree (the
production use_worktree topology, as land_path_test.py does) and pins:

  • REPRODUCTION — a re-run's diverged ``autodev/<T>`` branch is genuinely rejected non-fast-forward
    by a plain push (the AUTO-57 bug), and the re-run WIP has no ancestry with the prior-run WIP;
  • FIX — ``git.push`` recovers without raising and origin's branch now carries THIS run's WIP,
    replacing the stale prior-run WIP;
  • NO-REGRESSION — a first-ever push of a brand-new branch still succeeds by a plain (non-force) push;
  • SAFETY — the force path never touches base or protected: ``push('main')`` is refused before any
    push, and a genuinely diverged BASE branch is refused (never force-pushed) with origin/<base> left
    byte-for-byte intact.

Every check is a hard assertion over real git state, so reverting the fix flips the FIX/SAFETY checks
to FAIL and run_all marks the harness red — not a no-op green.
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
def code(cwd, *a):
    p = subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr
def is_ancestor(cwd, anc, desc):
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
    g.setup()
    return g, origin, main_co

BR = "autodev/RERUN-1"   # a throwaway unit-owned escalation branch

def run_once(g, marker: str):
    """Mirror one escalation cleanup: cut the feature branch fresh from origin/dev, drop a WIP
    file, commit it (as _cleanup's commit_all does). Returns the local WIP sha (pre-push)."""
    g.checkout_feature(BR)
    (Path(g.workdir) / f"{marker}.txt").write_text(f"WIP {marker}\n")
    return g.commit_all(f"WIP [autodev]: {marker} needs human review")

# ===================================================================================== #
# RUN 1 (prior escalated run): fresh branch -> plain push creates it on origin.
# ===================================================================================== #
g, origin, main_co = isolated("rerun")
run1_sha = run_once(g, "run1")
try:
    g.push(BR)
    run1_push_ok, run1_err = True, ""
except GitError as e:
    run1_push_ok, run1_err = False, str(e).splitlines()[0][:100]
chk("RUN 1: first push of a brand-new escalation branch succeeds (plain, non-force)", run1_push_ok, run1_err)
chk("RUN 1: origin now carries the branch at the run-1 WIP",
    out(origin, "rev-parse", BR) == run1_sha, f"{out(origin, 'rev-parse', BR)} vs {run1_sha}")
remote_wip_1 = out(origin, "rev-parse", BR)

# cleanup between tickets (mirror _cleanup's git.discard_and_return_base())
g.discard_and_return_base()

# ===================================================================================== #
# RUN 2 (the RE-RUN): a fresh branch cut from origin/dev diverges from the stale remote WIP.
# ===================================================================================== #
run2_sha = run_once(g, "run2")
chk("RE-RUN: the fresh branch has a different WIP sha than the prior run", run2_sha != remote_wip_1)
chk("RE-RUN: the re-run WIP is NOT a descendant of the prior-run WIP (they diverged)",
    not is_ancestor(g.workdir, remote_wip_1, run2_sha))

# REPRODUCTION: a plain push of the diverged branch is rejected non-fast-forward (the AUTO-57 bug).
rc, _, perr = code(g.workdir, "push", "origin", BR)
chk("REPRODUCTION: a plain push of the re-run branch is rejected non-fast-forward",
    rc != 0 and any(m in perr.lower() for m in ("non-fast-forward", "[rejected]", "fetch first",
                                                "behind its remote")),
    f"rc={rc} err={perr.splitlines()[-1][:80] if perr else ''}")
chk("REPRODUCTION: the rejected plain push left origin at the stale prior-run WIP",
    out(origin, "rev-parse", BR) == remote_wip_1)

# FIX: git.push must recover (fetch + --force-with-lease) without raising.
try:
    g.push(BR)
    fixed_ok, fix_err = True, ""
except GitError as e:
    fixed_ok, fix_err = False, str(e).splitlines()[0][:100]
chk("FIX: git.push recovers from the non-fast-forward re-run (does not raise)", fixed_ok, fix_err)
chk("FIX: origin's branch now carries the re-run WIP (prior-run WIP replaced)",
    fixed_ok and out(origin, "rev-parse", BR) == run2_sha,
    f"{out(origin, 'rev-parse', BR)} vs {run2_sha}")
chk("FIX: the stale prior-run WIP is no longer origin's branch tip",
    out(origin, "rev-parse", BR) != remote_wip_1)
chk("FIX: the re-run WIP file is on origin's branch",
    "run2.txt" in out(origin, "ls-tree", "-r", "--name-only", BR))

# ===================================================================================== #
# SAFETY: the force path must NEVER touch base or the protected branch.
# ===================================================================================== #
# (a) protected: push('main') is refused before any push is attempted (git_ops _guard).
gg, origin_g, _ = isolated("guard")
raised_p = False
try:
    gg.push("main")
except GitError as e:
    raised_p = "protected" in str(e).lower()
chk("SAFETY: push refuses the protected branch (guard fires before any push)", raised_p)

# (b) base: a genuinely diverged BASE branch is refused — never force-pushed — via the SAME
# non-fast-forward path (in-tree Git so a local `dev` branch exists and diverges).
origin_b = seed_origin("base_guard")
co = tmp / "base_guard_co"
G(tmp, "clone", str(origin_b), str(co)); G(co, "config", "user.email", "t@t"); G(co, "config", "user.name", "t")
G(co, "checkout", "dev")
# a concurrent writer advances origin/dev...
co2 = tmp / "base_guard_co2"
G(tmp, "clone", str(origin_b), str(co2)); G(co2, "config", "user.email", "t@t"); G(co2, "config", "user.name", "t")
G(co2, "checkout", "dev")
(co2 / "remote.txt").write_text("remote advance\n"); G(co2, "add", "-A"); G(co2, "commit", "-m", "remote advance")
G(co2, "push", "origin", "dev")
# ...while our checkout makes its OWN divergent commit on dev (now non-ff vs origin/dev).
(co / "local.txt").write_text("local divergent\n"); G(co, "add", "-A"); G(co, "commit", "-m", "local divergent")
gb = Git(repo_path=str(co), base_branch="dev", protected_branch="main")   # in-tree
dev_remote_before = out(origin_b, "rev-parse", "dev")
raised_b = False
try:
    gb.push("dev")
except GitError as e:
    # Pin the guard's own message — NOT a bare "base" substring, which the temp origin path
    # (…/base_guard.git) would satisfy coincidentally and give this check no teeth.
    raised_b = "refusing to force-push the base" in str(e).lower()
chk("SAFETY: push refuses to force-push the base branch even when base itself diverged non-ff", raised_b)
chk("SAFETY: origin/<base> was NOT force-moved by the refused base push",
    out(origin_b, "rev-parse", "dev") == dev_remote_before,
    f"{out(origin_b, 'rev-parse', 'dev')} vs {dev_remote_before}")

print("\n============ ESCALATION RE-RUN PUSH QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
