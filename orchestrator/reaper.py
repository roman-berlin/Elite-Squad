import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .config import Config

def reap_stale_worktrees(cfg: 'Config') -> None:
    """Run on autopilot/cockpit startup to reap leaked, merged worktrees."""
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

            for line in lines:
                if line.startswith("worktree "):
                    wt_path = line[9:].strip()
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

            if not branch_ref:
                print(f"  · reaper: skipping {wt_path} (detached HEAD, unclassifiable)", flush=True)
                continue

            # Check if merged into base
            try:
                merge_res = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", branch_ref, base_ref],
                    cwd=str(repo), capture_output=True
                )
                if merge_res.returncode != 0:
                    print(f"  · reaper: skipping {wt_path} (branch '{branch_ref}' NOT merged into {base_ref} - in flight)", flush=True)
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
                            print(f"  · reaper: skipping {wt_path} (PID {pid} still alive)", flush=True)
                            continue
                        except OSError:
                            is_dead = True
                    else:
                        is_dead = True
                else:
                    is_dead = True

            if is_dead:
                print(f"  · reaper: cleaning up stale merged worktree {wt_path} (branch {branch_ref})", flush=True)
                if is_locked:
                    subprocess.run(["git", "worktree", "unlock", wt_path], cwd=str(repo))
                subprocess.run(["git", "worktree", "remove", "--force", wt_path], cwd=str(repo))
                subprocess.run(["git", "worktree", "prune"], cwd=str(repo))
                subprocess.run(["git", "branch", "-D", branch_ref], cwd=str(repo), capture_output=True)
