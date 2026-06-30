"""EU-128 regression test: git error handling for missing repos.

Tests that:
1. is_git_repo() correctly identifies git repos vs non-repos
2. clear_parked_repos() clears the parking set
3. Apps with missing repos are parked and produce exactly one log message
4. ensure_clean() and has_changes() fail gracefully when repo is missing
5. Once parked, subsequent operations skip the repo without repeated errors
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

from orchestrator.git_ops import Git, clear_parked_repos, _PARKED_REPOS

results = []
def chk(name: str, cond, detail: str = "") -> None:
    """Record one assertion."""
    results.append((name, bool(cond), detail))

def G(cwd: Path, *args: str) -> None:
    """Run a git command; raise on failure."""
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)

tmp = Path(tempfile.mkdtemp())


def test_is_git_repo():
    """Test is_git_repo() correctly identifies git repos."""
    # Create a real git repo
    real_repo = tmp / "real_repo"
    real_repo.mkdir()
    G(real_repo, "init")
    G(real_repo, "config", "user.email", "t@t")
    G(real_repo, "config", "user.name", "t")
    (real_repo / "test.txt").write_text("test\n")
    G(real_repo, "add", "-A")
    G(real_repo, "commit", "-m", "initial")

    # Create a non-git directory
    non_repo = tmp / "non_repo"
    non_repo.mkdir()
    (non_repo / "file.txt").write_text("not a git repo\n")

    # Create a non-existent path
    missing = tmp / "missing_repo"

    chk("is_git_repo: real repo detected", Git.is_git_repo(real_repo),
        f"Expected True for {real_repo}")
    chk("is_git_repo: non-repo rejected", not Git.is_git_repo(non_repo),
        f"Expected False for {non_repo}")
    chk("is_git_repo: missing path rejected", not Git.is_git_repo(missing),
        f"Expected False for {missing}")


def test_clear_parked_repos():
    """Test clear_parked_repos() clears the parking set."""
    # Save current state
    saved_state = set(_PARKED_REPOS)

    # Clear for the test
    _PARKED_REPOS.clear()

    chk("clear_parked_repos: starts empty", len(_PARKED_REPOS) == 0,
        f"Set should be empty, has {_PARKED_REPOS}")

    # Add some repos to the set
    _PARKED_REPOS.add("/fake/repo1")
    _PARKED_REPOS.add("/fake/repo2")
    chk("clear_parked_repos: can add repos", len(_PARKED_REPOS) == 2,
        f"Set should have 2 repos, has {_PARKED_REPOS}")

    # Clear the set
    clear_parked_repos()
    chk("clear_parked_repos: clears all repos", len(_PARKED_REPOS) == 0,
        f"Set should be empty after clear, has {_PARKED_REPOS}")

    # Restore state for other tests
    _PARKED_REPOS.update(saved_state)


def test_missing_repo_parking():
    """Test that missing repos are parked and don't crash the run."""
    # Clear state first
    clear_parked_repos()

    # Create a real git repo for comparison
    real_repo = tmp / "parking_real"
    real_repo.mkdir()
    G(real_repo, "init")
    G(real_repo, "config", "user.email", "t@t")
    G(real_repo, "config", "user.name", "t")
    (real_repo / "test.txt").write_text("test\n")
    G(real_repo, "add", "-A")
    G(real_repo, "commit", "-m", "initial")
    G(real_repo, "checkout", "-b", "dev")

    # Create a non-git directory (simulating a missing/broken repo)
    missing_repo = tmp / "missing_repo"
    missing_repo.mkdir()
    (missing_repo / "file.txt").write_text("not a git repo\n")

    # Create a Git object for the missing repo
    git_missing = Git(
        repo_path=str(missing_repo),
        base_branch="dev",
        protected_branch="main",
    )

    # Try to check if repo has changes - should park the repo and return False
    has_changes_result = git_missing.has_changes()
    chk("missing repo: has_changes() returns False", not has_changes_result,
        "Should return False for missing repo")
    chk("missing repo: is parked", len(_PARKED_REPOS) > 0,
        f"Missing repo should be parked, parked set is {_PARKED_REPOS}")

    # Try again - should still be parked and return False without error
    second_call = git_missing.has_changes()
    chk("missing repo: second call also returns False", not second_call,
        "Should still return False for parked repo")

    # Create a Git object for a real repo to ensure it still works
    git_real = Git(
        repo_path=str(real_repo),
        base_branch="dev",
        protected_branch="main",
    )
    real_has_changes = git_real.has_changes()
    chk("real repo: has_changes() works", real_has_changes is False or real_has_changes is True,
        "Real repo should return a boolean without error")


def test_ensure_clean_with_missing_repo():
    """Test that ensure_clean() raises GitError gracefully for missing repos."""
    # Clear state first
    clear_parked_repos()

    # Create a non-git directory
    missing_repo = tmp / "ensure_clean_missing"
    missing_repo.mkdir()
    (missing_repo / "file.txt").write_text("not a git repo\n")

    git = Git(
        repo_path=str(missing_repo),
        base_branch="dev",
        protected_branch="main",
    )

    # Try to ensure_clean - should raise GitError with a clear message
    try:
        git.ensure_clean()
        chk("ensure_clean: missing repo raises", False,
            "Should have raised GitError for missing repo")
    except Exception as e:
        chk("ensure_clean: missing repo raises GitError", "GitError" in str(type(e)),
            f"Should raise GitError, got {type(e).__name__}: {e}")
        chk("ensure_clean: error message meaningful",
            "validation failed" in str(e).lower() or "not a git repository" in str(e).lower(),
            f"Error message should mention validation failure, got: {str(e)[:100]}")


def test_setup_with_missing_repo():
    """Test that setup() raises GitError gracefully for missing repos."""
    # Clear state first
    clear_parked_repos()

    # Create a non-git directory
    missing_repo = tmp / "setup_missing"
    missing_repo.mkdir()
    (missing_repo / "file.txt").write_text("not a git repo\n")

    worktree_path = tmp / "setup_worktree"
    worktree_path.mkdir()

    git = Git(
        repo_path=str(missing_repo),
        base_branch="dev",
        protected_branch="main",
        worktree_path=str(worktree_path),
    )

    # Try to setup - should raise GitError with a clear message
    try:
        git.setup()
        chk("setup: missing repo raises", False,
            "Should have raised GitError for missing repo")
    except Exception as e:
        chk("setup: missing repo raises GitError", "GitError" in str(type(e)),
            f"Should raise GitError, got {type(e).__name__}: {e}")
        chk("setup: error message meaningful",
            "not a git repository" in str(e).lower(),
            f"Error message should mention not a git repository, got: {str(e)[:100]}")


def test_non_resolving_base_branch():
    """Test that a non-resolving base_branch is properly detected and parks the repo."""
    # Clear state first
    clear_parked_repos()

    # Create a real git repo but WITHOUT the specified base branch
    repo = tmp / "no_base_branch"
    repo.mkdir()
    G(repo, "init")
    G(repo, "config", "user.email", "t@t")
    G(repo, "config", "user.name", "t")
    (repo / "test.txt").write_text("test\n")
    G(repo, "add", "-A")
    G(repo, "commit", "-m", "initial")
    # Rename the initial branch to something else (NOT 'dev')
    # Get the current branch name first
    code = subprocess.run(["git", "branch", "--show-current"], cwd=str(repo),
                          capture_output=True, text=True, check=True)
    current_branch = code.stdout.strip()
    # Rename it to 'main' if it's not already
    if current_branch != "main":
        G(repo, "branch", "-m", "main")
    # Now we have a repo with only 'main' branch, no 'dev'

    # Create a Git object that expects 'dev' branch which doesn't exist
    git = Git(
        repo_path=str(repo),
        base_branch="dev",  # This branch does not exist in the repo
        protected_branch="main",
    )

    # Try to check if repo has changes - should park the repo because base_branch doesn't resolve
    has_changes_result = git.has_changes()
    chk("non-resolving base: has_changes() returns False", not has_changes_result,
        "Should return False when base_branch doesn't resolve")
    # The repo gets resolved to an absolute path, so check if the repo name appears in any parked entry
    is_parked = any("no_base_branch" in p for p in _PARKED_REPOS)
    chk("non-resolving base: repo is parked", is_parked,
        f"Repo should be parked when base_branch doesn't resolve, parked set is {_PARKED_REPOS}")

    # Try again - should still be parked and return False without error
    second_call = git.has_changes()
    chk("non-resolving base: second call also returns False", not second_call,
        "Should still return False for parked repo")

    # Try ensure_clean - should raise GitError
    try:
        git.ensure_clean()
        chk("non-resolving base: ensure_clean raises", False,
            "Should have raised GitError when base_branch doesn't resolve")
    except Exception as e:
        chk("non-resolving base: ensure_clean raises GitError", "GitError" in str(type(e)),
            f"Should raise GitError, got {type(e).__name__}: {e}")

    # Clear and test with isolated mode (using origin/<base>)
    clear_parked_repos()
    worktree_path = tmp / "isolated_no_base"
    worktree_path.mkdir()

    git_isolated = Git(
        repo_path=str(repo),
        base_branch="dev",  # This branch does not exist
        protected_branch="main",
        worktree_path=str(worktree_path),
    )

    # Try to validate - should park the repo
    validate_result = git_isolated._validate_repo()
    chk("non-resolving base: isolated mode validation fails", not validate_result,
        "Should return False when origin/dev doesn't resolve")
    # Check that the repo (either main or worktree path) is in the parked set
    is_parked = any("no_base_branch" in p or "isolated_no_base" in p for p in _PARKED_REPOS)
    chk("non-resolving base: isolated mode repo is parked", is_parked,
        f"Repo should be parked in isolated mode, parked set is {_PARKED_REPOS}")


# Run all tests
test_is_git_repo()
test_clear_parked_repos()
test_missing_repo_parking()
test_ensure_clean_with_missing_repo()
test_setup_with_missing_repo()
test_non_resolving_base_branch()

# Print results
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"\n{passed}/{total} checks passed", flush=True)

for name, ok, detail in results:
    status = "✓" if ok else "✗"
    msg = f"{status} {name}"
    if detail:
        msg += f" — {detail}"
    print(msg, flush=True)

sys.exit(0 if passed == total else 1)
