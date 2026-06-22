"""Git/PR helpers. The diff produced here is the unit of review; the merge logic
keeps `dev` green and is hard-blocked from touching the protected branch (main)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional


class GitError(RuntimeError):
    pass


class Git:
    def __init__(self, repo_path: str, base_branch: str = "dev",
                 protected_branch: str = "main", worktree_path: Optional[str] = None):
        self.main = Path(repo_path).expanduser().resolve()   # the user's primary checkout
        self.base = base_branch
        self.protected = protected_branch
        if self.base == self.protected:
            raise GitError("base branch must not be the protected branch")
        # Isolated mode: the General works in its OWN linked worktree and treats
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
        in-tree mode. Raises GitError if origin/<base> can't be resolved."""
        if not self.isolated:
            return False
        self._git(self.main, "fetch", "origin", self.base, check=False)
        check = subprocess.run(["git", "rev-parse", "--verify", "--quiet", self.base_ref],
                               cwd=self.main, capture_output=True, text=True)
        if check.returncode != 0:
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
        if self._run("status", "--porcelain"):
            raise GitError("working tree is dirty; commit or stash before running")

    def current_sha(self) -> str:
        return self._run("rev-parse", "HEAD")

    def has_changes(self) -> bool:
        return bool(self._run("status", "--porcelain")) or bool(
            self._run("rev-list", f"{self.base_ref}..HEAD", check=False)
        )

    # -- branch / diff ---------------------------------------------------- #
    def checkout_feature(self, branch: str) -> None:
        self._guard(branch)
        if self.isolated:
            # fresh feature branch off the latest origin/<base>; nothing local is touched
            self._git(self.main, "fetch", "origin", self.base, check=False)
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
        """Forward-only undo of a landed merge commit (Sentinel's rollback). Builds a branch at the
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
        never touched (they pull it for QA). In-tree: ff base locally, then push."""
        if self.base == self.protected:
            raise GitError("refusing to land on the protected branch")
        if self.isolated:
            self._run("push", "origin", f"{temp}:{self.base}")   # ff origin/<base> forward
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
