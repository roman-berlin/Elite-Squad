"""Git/PR helpers. The diff produced here is the unit of review; the merge logic
keeps `dev` green and is hard-blocked from touching the protected branch (main)."""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class GitError(RuntimeError):
    pass


# Class-level set to track repos that have failed validation and should be parked
# for the rest of the run. Once a repo fails git validation, we skip it to avoid
# repeated crash-loops and error spam (EU-128).
_PARKED_REPOS: set[str] = set()


def clear_parked_repos() -> None:
    """Clear the parked repos set at the start of a new run.
    This ensures parking doesn't carry over between different autopilot cycles or runs."""
    _PARKED_REPOS.clear()

def reap_stale_worktrees(cfg) -> None:
    """Run on autopilot/cockpit startup to reap leaked, merged worktrees."""
    import os
    import re
    from . import loop

    for app in getattr(cfg, "apps", []) or []:
        repo_path = getattr(app, "repo_path", "")
        if not repo_path:
            continue
        repo = Path(repo_path).expanduser().resolve()
        if not repo.exists() or not (repo / ".git").exists():
            continue

        try:
            res = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=str(repo), capture_output=True, text=True)
            if res.returncode != 0:
                continue
        except OSError:
            continue

        base_ref = f"origin/{app.base_branch}"
        blocks = res.stdout.strip().split("\n\n")

        for block in blocks:
            lines = block.split("\n")
            wt_path = None
            branch_ref = None
            is_locked = False
            lock_reason = ""

            head_sha = None
            for line in lines:
                if line.startswith("worktree "):
                    wt_path = line[9:].strip()
                elif line.startswith("HEAD "):
                    head_sha = line[5:].strip()
                elif line.startswith("branch refs/heads/"):
                    branch_ref = line[18:].strip()
                elif line == "locked" or line.startswith("locked "):
                    is_locked = True
                    lock_reason = line[7:].strip() if line.startswith("locked ") else ""

            if not wt_path:
                continue

            # Target paths: .claude/worktrees/agent-* and .general-worktrees/*
            if not (".claude/worktrees/agent-" in wt_path or ".general-worktrees/" in wt_path):
                continue

            # QW7 (2026-07-05): the orchestrator creates EVERY .general-worktrees worktree with
            # `worktree add --detach` (see _fresh_worktree below), so a detached HEAD is the ONLY
            # shape production produces — classify it by its HEAD sha instead of skipping it.
            # (The Jul-1 crash orphan was exactly this: detached @ e94aa96, merged, owner dead,
            # and the old `if not branch_ref: continue` made it unreachable — structurally.)
            # Detached .claude/worktrees/agent-* paths keep the skip: their provenance is the
            # Claude harness, not ours.
            merge_probe = branch_ref or (head_sha if ".general-worktrees/" in wt_path else None)
            if not merge_probe:
                print(f"  · reaper: skipping {wt_path} (detached HEAD, unclassifiable)", flush=True)
                continue

            # Check if merged into base
            try:
                merge_res = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", merge_probe, base_ref],
                    cwd=str(repo), capture_output=True
                )
                if merge_res.returncode != 0:
                    continue
            except OSError:
                continue

            # Check if owning session is dead
            is_dead = False
            if ".general-worktrees/" in wt_path:
                is_dead = not loop.is_worktree_locked(wt_path)
            else:
                if is_locked:
                    m = re.search(r'pid\s*[:=]?\s*(\d+)', lock_reason, re.IGNORECASE)
                    if m:
                        pid = int(m.group(1))
                        try:
                            os.kill(pid, 0)
                            continue
                        except OSError:
                            is_dead = True
                    else:
                        is_dead = True
                else:
                    is_dead = True

            if is_dead:
                label = branch_ref or f"detached @ {(head_sha or '')[:9]}"
                print(f"  · reaper: cleaning up stale merged worktree {wt_path} ({label})", flush=True)
                if is_locked:
                    subprocess.run(["git", "worktree", "unlock", wt_path], cwd=str(repo))
                subprocess.run(["git", "worktree", "remove", "--force", wt_path], cwd=str(repo))
                subprocess.run(["git", "worktree", "prune"], cwd=str(repo))
                if branch_ref:   # QW7: a detached worktree has no branch to delete
                    subprocess.run(["git", "branch", "-D", branch_ref], cwd=str(repo), capture_output=True)
                # QW7: also remove the dead session's flock sidecar (<worktree>.lock) — git doesn't
                # know about it, and a stale one is the litter the Jul-1 crash left behind.
                try:
                    Path(wt_path + ".lock").unlink(missing_ok=True)
                except OSError:
                    pass


class Git:
    def __init__(self, repo_path: str, base_branch: str = "dev",
                 protected_branch: str = "main", worktree_path: Optional[str] = None):
        self.main = Path(repo_path).expanduser().resolve()   # the user's primary checkout
        self.base = base_branch
        self.protected = protected_branch
        if self.base == self.protected:
            raise GitError("base branch must not be the protected branch")
        # Isolated mode: the CTO works in its OWN linked worktree and treats
        # origin/<base> as the integration point — so it never touches the branch or
        # working tree the user has checked out. In-tree mode keeps the original
        # behaviour: operate directly in repo_path on the local base branch.
        self.isolated = worktree_path is not None
        self.repo = Path(worktree_path).expanduser().resolve() if self.isolated else self.main
        self.base_ref = f"origin/{base_branch}" if self.isolated else base_branch

    @property
    def workdir(self) -> str:
        """Directory the officers + gate should run in (the worktree when isolated)."""
        return str(self.repo)

    # -- preflight checks -------------------------------------------------- #
    @staticmethod
    def is_git_repo(path: Path | str) -> bool:
        """Check if a path is a valid git repository."""
        repo_path = Path(path).expanduser().resolve()
        git_dir = repo_path / ".git"
        # Check for either a .git directory (regular checkout) or .git file (worktree)
        return git_dir.is_dir() or git_dir.is_file()

    def _is_parked(self) -> bool:
        """Check if this repo is already parked due to prior validation failure."""
        return str(self.repo) in _PARKED_REPOS or str(self.main) in _PARKED_REPOS

    def _park_repo(self, reason: str) -> None:
        """Park this repo for the rest of the run and log the failure."""
        _PARKED_REPOS.add(str(self.repo))
        if self.isolated and str(self.repo) != str(self.main):
            _PARKED_REPOS.add(str(self.main))
        logger.warning(f"Git repo parked: {self.repo} - {reason}")
        print(f"  · skipping {self.main} — repo validation failed: {reason}", flush=True)

    def _validate_repo(self) -> bool:
        """Preflight check: verify repo exists and base_branch resolves.
        Returns False if validation fails (repo is then parked)."""
        # Check if already parked from a prior failure
        if self._is_parked():
            return False

        # Check if main repo path is a valid git repository
        if not self.is_git_repo(self.main):
            self._park_repo("not a git repository")
            return False

        # For isolated mode, also check the worktree path
        if self.isolated and not self.is_git_repo(self.repo):
            self._park_repo("worktree not a git repository")
            return False

        # Validate that base_branch resolves (especially important for isolated mode
        # which uses origin/<base> as the integration point)
        try:
            self._git(self.main, "rev-parse", "--verify", self.base_ref, check=True)
        except GitError as exc:
            self._park_repo(f"base branch '{self.base_ref}' does not resolve")
            return False

        return True

    # -- low level -------------------------------------------------------- #
    def _git(self, cwd: Path, *args: str, check: bool = True) -> str:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed:\n{proc.stderr}")
        return proc.stdout.strip()

    def _run(self, *args: str, check: bool = True) -> str:
        return self._git(self.repo, *args, check=check)

    def _run_code(self, *args: str) -> tuple[int, str, str]:
        proc = subprocess.run(["git", *args], cwd=self.repo,
                              capture_output=True, text=True)
        return proc.returncode, proc.stdout, proc.stderr

    def _code_at(self, cwd: Path, *args: str) -> tuple[int, str, str]:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        return proc.returncode, proc.stdout, proc.stderr

    def _guard(self, branch: str) -> None:
        if branch == self.protected:
            raise GitError(f"refusing to operate on protected branch '{self.protected}'")

    # -- worktree lifecycle (isolated mode only) -------------------------- #
    def setup(self) -> bool:
        """Ensure the dedicated linked worktree exists and is clean, parked on a
        detached origin/<base>. Returns True if it was freshly created (so the caller
        can run a one-time setup command like `bun install`). No-op returning False in
        in-tree mode. Raises GitError if origin/<base> can't be resolved.

        Fetch is intentionally performed BEFORE resolving or creating the worktree so
        that the worktree always starts from the latest remote state — never a stale
        local ref — regardless of how long the previous run took."""
        # Preflight validation - check if repo is valid before attempting setup
        if not self.is_git_repo(self.main):
            self._park_repo("not a git repository")
            raise GitError(f"cannot setup worktree: '{self.main}' is not a git repository")

        if not self.isolated:
            return False
        self._git(self.main, "fetch", "origin", self.base, check=False)
        check = subprocess.run(["git", "rev-parse", "--verify", "--quiet", self.base_ref],
                               cwd=self.main, capture_output=True, text=True)
        if check.returncode != 0:
            self._park_repo(f"base branch '{self.base_ref}' does not resolve")
            raise GitError(f"cannot isolate: '{self.base_ref}' does not resolve "
                           f"(no origin remote, or branch '{self.base}' was never pushed)")
        self._git(self.main, "worktree", "prune", check=False)
        if (self.repo / ".git").exists():
            # reuse an existing worktree: force it back to a clean detached base
            self._run("reset", "--hard", check=False)
            self._run("clean", "-fd", check=False)
            self._run("checkout", "--detach", self.base_ref)
            return False
        if self.repo.exists():
            shutil.rmtree(self.repo, ignore_errors=True)   # stale non-worktree dir in the way
        self.repo.parent.mkdir(parents=True, exist_ok=True)
        self._git(self.main, "worktree", "add", "--detach", str(self.repo), self.base_ref)
        return True

    # -- state ------------------------------------------------------------ #
    def ensure_clean(self) -> None:
        """Check if working tree is clean. Raises GitError if dirty.
        Safe-failing: if repo validation fails, parks the app and raises."""
        # Preflight validation - will park if this fails
        if not self._validate_repo():
            raise GitError(f"repo validation failed for {self.main}")

        try:
            status_output = self._run("status", "--porcelain", check=True)
            if status_output:
                raise GitError("working tree is dirty; commit or stash before running")
        except (GitError, OSError) as exc:
            # Git command failed - park the repo and raise
            if not isinstance(exc, GitError) or "working tree is dirty" not in str(exc):
                self._park_repo(f"git status failed")
            if not isinstance(exc, GitError):
                raise GitError(f"git status failed for {self.main}") from exc
            raise

    def current_sha(self) -> str:
        """Get current HEAD sha. Safe-failing: returns empty string on error."""
        try:
            return self._run("rev-parse", "HEAD", check=False)
        except (subprocess.SubprocessError, OSError):
            return ""

    def has_changes(self) -> bool:
        """Check if there are uncommitted changes or commits ahead of base.
        Safe-failing: returns False if git validation fails."""
        # Preflight validation - will park if this fails
        if not self._validate_repo():
            return False

        try:
            status_output = self._run("status", "--porcelain", check=True)
            revlist_output = self._run("rev-list", f"{self.base_ref}..HEAD", check=True)
            return bool(status_output) or bool(revlist_output)
        except (GitError, OSError) as exc:
            # Git command failed - park the repo and return False (safe default)
            self._park_repo(f"git check failed")
            return False

    # -- branch / diff ---------------------------------------------------- #
    def checkout_feature(self, branch: str) -> None:
        """Create (or reset) the feature branch from the freshest available base.

        Isolated mode: the branch point is always ``origin/<base>`` (fetched just
        before checkout), NOT the local base ref.  This guarantees every feature
        starts from the true remote HEAD even if the local checkout is behind, so
        concurrent tickets don't silently build on stale code."""
        self._guard(branch)
        if self.isolated:
            # Fetch origin/<base> first so the branch point is the latest remote
            # state, not whatever the local ref happened to be last time setup ran.
            self._git(self.main, "fetch", "origin", self.base, check=False)
            # Branch off origin/<base> — the user's local <base> checkout is
            # intentionally never touched (they pull it for QA after land).
            self._run("checkout", "-B", branch, self.base_ref)
            return
        self._run("checkout", self.base)
        self._run("pull", "--ff-only", check=False)   # best effort; offline ok
        if self._run("branch", "--list", branch):
            self._run("checkout", branch)
        else:
            self._run("checkout", "-b", branch)

    def diff_against_base(self) -> str:
        """Full diff of all feature work vs base. Staging first (`add -A`) is what
        makes NEW files show up — `git diff` alone omits untracked files.

        F14 hygiene — reliance on the target repo's .gitignore: `git add -A` stages
        every untracked, non-ignored path, so any stray builder artifact (caches,
        build output, dumps) written mid-build lands on the feature branch unless the
        target repo's `.gitignore` excludes it. We deliberately keep `add -A` (not a
        scoped pathspec) because narrowing risks dropping legitimately-new source
        files — surfacing them is the whole point of this diff. The safety net is
        therefore the target repo's `.gitignore`: it MUST cover the toolchain's
        artifacts (node_modules/, dist/, build/, __pycache__/, *.log, .env, etc.).
        `git add -A` honours `.gitignore` automatically, so a properly-ignored stray
        file is never staged — verified by tests/git_add_hygiene_test.py."""
        self._run("add", "-A")
        merge_base = self._run("merge-base", self.base_ref, "HEAD")
        return self._run("diff", "--cached", merge_base)

    def changed_paths(self) -> list[str]:
        """Repo-relative paths of all feature work vs base (staged, incl. new files).
        Used by the gate to detect which monorepo apps/packages a ticket touches so it
        only typechecks those. Same staging rationale as diff_against_base(). (EU-19)"""
        self._run("add", "-A")
        merge_base = self._run("merge-base", self.base_ref, "HEAD")
        out = self._run("diff", "--cached", "--name-only", merge_base)
        return [line.strip() for line in out.splitlines() if line.strip()]

    def commit_all(self, message: str) -> Optional[str]:
        # `add -A` honours the target repo's .gitignore — stray ignored builder
        # artifacts are never staged. See diff_against_base() for the F14 rationale.
        self._run("add", "-A")
        if not self._run("status", "--porcelain"):
            return None
        self._run("commit", "-m", message)
        return self.current_sha()

    def push(self, branch: str) -> None:
        self._guard(branch)
        self._run("push", "-u", "origin", branch)

    # -- merge into base (dev), keeping it green --------------------------- #
    def prepare_base(self) -> str:
        """Checkout base (dev) and fast-forward it. Returns base HEAD sha."""
        self._run("checkout", self.base)
        self._run("pull", "--ff-only", check=False)
        return self.current_sha()

    def merge_no_ff(self, feature: str, message: str) -> bool:
        """Merge `feature` into the currently checked-out base. Returns True on a
        clean merge; on conflict, aborts and returns False. Never targets main."""
        if self._run("rev-parse", "--abbrev-ref", "HEAD") == self.protected:
            raise GitError("refusing to merge into the protected branch")
        code, _, _ = self._run_code("merge", "--no-ff", "-m", message, feature)
        if code != 0:
            self._run_code("merge", "--abort")
            return False
        return True

    def reset_hard_to(self, sha: str) -> None:
        self._run("reset", "--hard", sha)

    def push_base(self) -> None:
        self._run("push", "origin", self.base)

    def revert_merge_on_base(self, merge_sha: str) -> bool:
        """Forward-only undo of a landed merge commit (SRE's rollback). Builds a branch at the
        current origin/<base>, reverts the merge (keeping the pre-merge first parent), and ff-pushes
        the revert to <base>. No force-push, no history rewrite. Returns False if the revert can't be
        applied cleanly (caller then escalates to a human)."""
        if self.base == self.protected:
            raise GitError("refusing to revert on the protected branch")
        rb = f"{self.base.replace('/', '_')}_revert"
        if self.isolated:
            self._git(self.main, "fetch", "origin", self.base, check=False)
            self._run("checkout", "-B", rb, self.base_ref)
        else:
            self._run("checkout", self.base)
            self._run("pull", "--ff-only", check=False)
            self._run("checkout", "-B", rb, self.base)
        code, _, _ = self._run_code("revert", "--no-edit", "-m", "1", merge_sha)
        if code != 0:
            self._run_code("revert", "--abort")
            self._cleanup_branch(rb)
            return False
        if self.isolated:
            self._run("push", "origin", f"{rb}:{self.base}")   # ff origin/<base> forward by the revert
            self._run("checkout", "--detach", self.base_ref)
        else:
            self._run("checkout", self.base)
            self._run("merge", "--ff-only", rb)
            self._run("push", "origin", self.base)
        self._cleanup_branch(rb)
        return True

    def _cleanup_branch(self, branch: str) -> None:
        self._run_code("branch", "-D", branch)

    # -- trial merge (keep base untouched until the final, validated merge) --- #
    def trial_merge(self, feature: str, temp: str, message: str) -> bool:
        """Create a throwaway `temp` branch from base and merge `feature` into IT
        (base is never modified). Leaves you on `temp`. Returns True on a clean
        merge; on conflict, aborts and returns False."""
        if self.isolated:
            self._git(self.main, "fetch", "origin", self.base, check=False)
            self._run("checkout", "-B", temp, self.base_ref)   # temp = origin/<base>; nothing local touched
        else:
            self._run("checkout", self.base)
            self._run("pull", "--ff-only", check=False)
            self._run("checkout", "-B", temp, self.base)       # temp = base, base untouched
        code, _, _ = self._run_code("merge", "--no-ff", "-m", message, feature)
        if code != 0:
            self._run_code("merge", "--abort")
            return False
        return True

    def abandon_trial(self, temp: str) -> None:
        """Throw away the trial branch and return to base (base unchanged)."""
        if self.isolated:
            self._run("checkout", "--detach", self.base_ref)
        else:
            self._run("checkout", self.base)
        self._run_code("branch", "-D", temp)

    def land_trial(self, temp: str) -> None:
        """The ONLY moment base changes. Isolated: fast-forward-push the validated
        trial straight to origin/<base> — the user's local base and working tree are
        never touched (they pull it for QA). In-tree: ff base locally, then push.

        Non-fast-forward recovery (isolated mode only): if the push is rejected because
        another ticket landed concurrently and advanced origin/<base>, we fetch the new
        tip, rebase ``temp`` onto it, and retry once.  On a second failure the temp
        branch is deleted (so the next ticket starts clean) and a descriptive error is
        raised — the caller should treat this as a failed land and leave the ticket for
        the next run rather than leaving a dirty tree."""
        if self.base == self.protected:
            raise GitError("refusing to land on the protected branch")
        if self.isolated:
            code, _, err = self._run_code("push", "origin", f"{temp}:{self.base}")
            if code != 0:
                # Likely a non-fast-forward rejection: fetch the latest base and
                # rebase the trial merge commit onto it, then retry the push once.
                self._git(self.main, "fetch", "origin", self.base, check=False)
                rb_code, _, rb_err = self._run_code("rebase", self.base_ref)
                if rb_code != 0:
                    self._run_code("rebase", "--abort")
                    self._run_code("branch", "-D", temp)
                    self._run("checkout", "--detach", self.base_ref)
                    raise GitError(
                        f"land_trial: push rejected and rebase of '{temp}' onto "
                        f"'{self.base_ref}' failed — left on clean {self.base_ref}.\n"
                        f"push stderr: {err}\nrebase stderr: {rb_err}"
                    )
                code2, _, err2 = self._run_code("push", "origin", f"{temp}:{self.base}")
                if code2 != 0:
                    self._run_code("branch", "-D", temp)
                    self._run("checkout", "--detach", self.base_ref)
                    raise GitError(
                        f"land_trial: push to origin/{self.base} failed after rebase "
                        f"— left on clean {self.base_ref}; retry the ticket.\n{err2}"
                    )
            self._run("checkout", "--detach", self.base_ref)     # off temp; origin/<base> now advanced
            self._run_code("branch", "-D", temp)
            return
        self._run("checkout", self.base)
        self._run("merge", "--ff-only", temp)
        self._run("push", "origin", self.base)
        self._run_code("branch", "-D", temp)

    def sync_main_base(self) -> str:
        """Isolated mode only. After a live land, bring <base> in the user's MAIN
        checkout up to date so it's QA-ready — WITHOUT disturbing their work:
          • on <base> & clean  -> fast-forward it (working tree now has the merge)
          • on another branch  -> advance the <base> ref only (working tree untouched)
          • on <base> & dirty  -> leave it; tell them to pull when ready
        Returns a short human note. Never raises (it's a convenience, not custody)."""
        if not self.isolated:
            return ""   # in-tree mode already advanced the local base during land
        main = self.main
        try:
            self._git(main, "fetch", "origin", self.base, check=False)
            cur = self._git(main, "rev-parse", "--abbrev-ref", "HEAD", check=False)
            if cur == self.base:
                if self._git(main, "status", "--porcelain", check=False):
                    return f"{self.base} has local edits in your checkout — pull it when ready"
                code, _, err = self._code_at(main, "merge", "--ff-only", self.base_ref)
                if code == 0:
                    return f"{self.base} fast-forwarded in your checkout — ready to QA"
                return f"{self.base} not advanced (diverged locally — pull manually)"
            # base not checked out: ff its ref without touching the working tree
            code, _, _ = self._code_at(main, "fetch", "origin", f"{self.base}:{self.base}")
            if code == 0:
                return f"{self.base} ref updated (you're on {cur}) — checkout {self.base} to QA"
            return f"{self.base} left as-is (you're on {cur})"
        except Exception as exc:  # noqa: BLE001 - never break the run over a convenience sync
            return f"{self.base} sync skipped ({str(exc).splitlines()[0][:50]})"

    # -- cleanup ---------------------------------------------------------- #
    def discard_and_return_base(self) -> None:
        """Reset uncommitted changes and return to base, so the next ticket starts
        clean. `clean -fd` removes untracked builder files (safe: the run refuses to
        start on a dirty tree). Keep audit_path/config OUTSIDE the target repo."""
        self._run("reset", "--hard")
        self._run("clean", "-fd")
        if self.isolated:
            self._run("checkout", "--detach", self.base_ref)
        else:
            self._run("checkout", self.base)

    def delete_local_branch(self, branch: str) -> None:
        if branch and branch != self.base and branch != self.protected:
            self._run_code("branch", "-D", branch)

    def delete_remote_branch(self, branch: str) -> None:
        """Delete the remote tracking ref for a fully-merged feature branch.

        Called after a successful land so that the remote (e.g. GitHub) does not
        accumulate stale autodev/* refs.  Silently skips base and protected branches
        to prevent an accidental ``git push origin --delete dev`` or ``--delete main``;
        also skips empty/None names and swallows non-fatal push errors (the branch may
        already be gone or the remote may not support deletion — neither is fatal)."""
        if not branch or branch == self.base or branch == self.protected:
            return
        self._run_code("push", "origin", "--delete", branch)

    # -- PR --------------------------------------------------------------- #
    def open_pr(self, branch: str, title: str, body: str) -> Optional[str]:
        """Open a PR from `branch` into base (dev) via the GitHub CLI if available."""
        self._guard(branch)
        if not shutil.which("gh"):
            return None
        proc = subprocess.run(
            ["gh", "pr", "create", "--base", self.base, "--head", branch,
             "--title", title, "--body", body],
            cwd=self.repo, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise GitError(f"gh pr create failed:\n{proc.stderr}")
        return proc.stdout.strip()
