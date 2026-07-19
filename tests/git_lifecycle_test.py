"""EU-81 regression test: merge→push→ff-sync lifecycle.

Drives the REAL Git class through the full autopilot land path using a hermetic
bare-origin harness (same pattern as land_path_test.py — tmp bare repos, real git
commands, no mocks) and pins four post-land invariants:

  1. HAPPY PATH — worktree branches from origin/dev, commits, trial-merges, lands,
     deletes the remote feature branch, and syncs the Mac clone:
       • Mac clone is not behind origin/dev (rev-list count == 0)
       • Mac clone working tree is clean (git status --porcelain is empty)
       • No autodev/* remote tracking refs remain on origin

  2. NON-FF RETRY — a concurrent commit advances origin/dev between trial_merge and
     land_trial; the retry logic (fetch + rebase + re-push) resolves it cleanly and
     the same four invariants hold after the successful retry land.
"""
import sys, types, tempfile, subprocess
from pathlib import Path

# stub the Agent SDK so importing orchestrator.* is cheap + offline (house pattern)
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator.git_ops import Git

results = []
def chk(name: str, cond, detail: str = "") -> None:
    """Record one assertion (mirrors land_path_test.py convention)."""
    results.append((name, bool(cond), detail))

def G(cwd: Path, *args: str) -> None:
    """Run a git command; raise on failure (for harness setup only)."""
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)

def out(cwd: Path, *args: str) -> str:
    """Run a git command and return trimmed stdout (never raises)."""
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    ).stdout.strip()

tmp = Path(tempfile.mkdtemp())


def seed_origin(name: str) -> Path:
    """Bare origin with `main` (protected) and `dev` (base) at one baseline commit."""
    origin = tmp / f"{name}.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True, capture_output=True,
    )
    seed = tmp / f"{name}_seed"
    G(tmp, "clone", str(origin), str(seed))
    G(seed, "config", "user.email", "t@t")
    G(seed, "config", "user.name", "t")
    (seed / "base.txt").write_text("baseline\n")
    G(seed, "add", "-A")
    G(seed, "commit", "-m", "baseline")
    G(seed, "push", "origin", "HEAD:main")
    G(seed, "checkout", "-b", "dev")
    G(seed, "push", "origin", "dev")
    return origin


def make_isolated(name: str):
    """Create the isolated-mode topology: bare origin + Mac clone + linked worktree.

    Returns (git_obj, origin_path, mac_clone_path).
    The Git object's worktree is the dedicated linked worktree — the unit's own
    sandbox; mac_clone is the developer's primary checkout that sync_main_base()
    fast-forwards after every land."""
    origin = seed_origin(name)
    mac = tmp / f"{name}_mac"
    G(tmp, "clone", str(origin), str(mac))
    G(mac, "config", "user.email", "t@t")
    G(mac, "config", "user.name", "t")
    wt = tmp / f"{name}_wt"
    g = Git(
        repo_path=str(mac),
        base_branch="dev",
        protected_branch="main",
        worktree_path=str(wt),
    )
    g.setup()
    return g, origin, mac


def assert_post_land_invariants(
    prefix: str, g: Git, origin: Path, mac: Path, feature_branch: str
) -> None:
    """Assert the three invariants the ticket specifies after a successful land.

    • Mac clone is not behind origin/dev (rev-list count == 0)
    • Mac clone working tree is clean (git status --porcelain empty)
    • No autodev/* remote tracking refs exist on origin
    """
    # Refresh the Mac clone's remote view so rev-list sees the latest origin/dev.
    G(mac, "fetch", "origin", "dev")

    behind_count = out(mac, "rev-list", "--count", "dev..origin/dev")
    chk(
        f"[{prefix}] Mac clone is not behind origin/dev (count == 0)",
        behind_count == "0",
        f"behind by {behind_count} commit(s)",
    )

    status = out(mac, "status", "--porcelain")
    chk(
        f"[{prefix}] Mac clone working tree is clean",
        status == "",
        f"dirty: {status!r}",
    )

    remote_branches = out(origin, "branch", "--list", "autodev/*")
    chk(
        f"[{prefix}] No autodev/* branches remain on origin",
        remote_branches == "",
        f"found: {remote_branches!r}",
    )


# ============================================================================ #
# 1) HAPPY PATH                                                                 #
#    worktree → feature commit → trial merge → land → delete remote branch     #
#    → sync Mac clone                                                           #
# ============================================================================ #
g1, origin1, mac1 = make_isolated("happy")

feature = "autodev/eu-81"
g1.checkout_feature(feature)

(Path(g1.workdir) / "feature.txt").write_text("added by eu-81\n")
sha = g1.commit_all("EU-81: add feature file")
chk("happy-path: commit_all returned a SHA", bool(sha), str(sha))

temp = "autodev/_trial"
clean = g1.trial_merge(feature, temp, "Merge autodev/eu-81 into dev (EU-81)")
chk("happy-path: trial_merge returns True (clean merge)", clean)

dev_before = out(origin1, "rev-parse", "dev")
g1.land_trial(temp)
dev_after = out(origin1, "rev-parse", "dev")
chk("happy-path: land_trial advanced origin/dev", dev_after != dev_before)

g1.delete_local_branch(feature)
g1.delete_remote_branch(feature)

sync_note = g1.sync_main_base()
chk("happy-path: sync_main_base returns a non-empty note", bool(sync_note), sync_note)

assert_post_land_invariants("happy", g1, origin1, mac1, feature)

# Verify the feature content actually landed on origin/dev
chk(
    "happy-path: feature.txt is present on origin/dev",
    "feature.txt" in out(origin1, "ls-tree", "-r", "--name-only", "dev"),
)

# ============================================================================ #
# 2) NON-FF RACE → LandRaceError, NOTHING lands (EU-259)                        #
#    Another commit lands on origin/dev between trial_merge and land_trial.     #
#    Pre-EU-259 land_trial rebased the trial onto the new base and re-pushed —  #
#    but that COMBINED tree was never gated (a gate bypass) and left the        #
#    caller's merge_sha pointing at a phantom commit. The correct contract:     #
#    land_trial raises LandRaceError, lands NOTHING, and leaves a clean base;   #
#    the loop requeues and the next drain re-trials + re-gates against the new  #
#    base. Only an honest fast-forward ever advances origin/dev.                #
# ============================================================================ #
from orchestrator.git_ops import LandRaceError  # noqa: E402

g2, origin2, mac2 = make_isolated("noff")

feature2 = "autodev/eu-81b"
g2.checkout_feature(feature2)

(Path(g2.workdir) / "eu81b.txt").write_text("eu-81b content\n")
sha2 = g2.commit_all("EU-81b: add separate file")
chk("non-ff: commit_all returned a SHA", bool(sha2), str(sha2))

temp2 = "autodev/_trial"
clean2 = g2.trial_merge(feature2, temp2, "Merge autodev/eu-81b into dev (EU-81b)")
chk("non-ff: trial_merge returns True", clean2)

# Simulate a CONCURRENT land: push a non-conflicting commit to origin/dev from
# an independent clone *after* trial_merge was created but *before* land_trial.
interloper = tmp / "interloper"
G(tmp, "clone", str(origin2), str(interloper))
G(interloper, "config", "user.email", "t@t")
G(interloper, "config", "user.name", "t")
G(interloper, "checkout", "-B", "dev", "origin/dev")
(interloper / "concurrent.txt").write_text("concurrent commit\n")
G(interloper, "add", "-A")
G(interloper, "commit", "-m", "concurrent ticket landed first")
G(interloper, "push", "origin", "dev")

dev_before2 = out(origin2, "rev-parse", "dev")  # the concurrent commit — the ONLY thing on dev

# land_trial must REFUSE to land the ungated combined tree: it raises LandRaceError.
raised_land_race = False
try:
    g2.land_trial(temp2)
    chk("non-ff: land_trial did NOT silently land the ungated tree", False, "no exception raised")
except LandRaceError:
    raised_land_race = True
    chk("non-ff: land_trial raises LandRaceError on a base-advanced race", True)
except Exception as e:   # noqa: BLE001
    chk("non-ff: land_trial raised the WRONG exception type", False, f"{type(e).__name__}: {e}")

if raised_land_race:
    dev_after2 = out(origin2, "rev-parse", "dev")
    chk("non-ff: origin/dev UNCHANGED — nothing ungated landed",
        dev_after2 == dev_before2, f"{dev_before2[:9]} -> {dev_after2[:9]}")
    # the local checkout is clean and detached at the (old) base — the trial branch is gone
    branches = out(g2.repo, "branch", "--list", temp2)
    chk("non-ff: the trial branch was cleaned up", temp2 not in branches, branches)
    # The feature branch still holds the work — the loop requeues and the NEXT drain re-trials it
    # (setup() re-fetches + re-detaches the worktree to the fresh base, then trial_merge + gate run
    # again). That loop-level re-trial + re-gate is pinned in tests/land_race_requeue_test.py; here
    # we assert only the git-level contract: land_trial refused, landed nothing, and left clean state.
    chk("non-ff: the local checkout is not left on the trial branch (detached/clean)",
        temp2 not in out(g2.repo, "rev-parse", "--abbrev-ref", "HEAD"))
    # The concurrent commit is untouched on origin/dev — we neither clobbered nor rebased over it.
    chk("non-ff: the concurrent commit is still the origin/dev tip",
        out(origin2, "rev-parse", "dev") == dev_before2)
    chk("non-ff: EU-81b's file did NOT land (gate never validated it against the new base)",
        "eu81b.txt" not in out(origin2, "ls-tree", "-r", "--name-only", "dev"))


# ============================================================================ #
# Results                                                                       #
# ============================================================================ #
print("\n================ EU-81 GIT LIFECYCLE QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    suffix = f"  ({detail})" if detail and not ok else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{suffix}")
print("----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
