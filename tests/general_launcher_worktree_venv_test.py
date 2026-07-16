"""./general launcher — worktree venv fallback (EU-218 red-base class, 2026-07-16).

A fresh git worktree is created WITHOUT a .venv (gitignored), and macOS has no bare
`python` on PATH, so `./general …` died with `exec: python: not found` inside every
fresh base-gate worktree. That turned any harness that shells out to ./general
(eu186_cron_patrol_fix_test) into a false "red base — gate fails on the clean base
tree", which HALTS the drain (red_base_drain_hold).

Pins:
  (1) in a worktree with NO local .venv, the launcher falls back to the MAIN
      checkout's .venv (parsed from the worktree's `.git` file: `gitdir: <main>/.git/
      worktrees/<name>`);
  (2) a worktree WITH its own .venv still prefers the local one;
  (3) the fallback parse is best-effort — a malformed .git file must not crash the
      launcher (set -euo pipefail) before it can report the real problem.

Hermetic: fake main repo + fake worktree in a tmpdir; the "venv" activate scripts
just prepend a stub `python` to PATH that prints a marker and exits 0. No git
operations, no real venv, no network.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "general"

checks = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


def _make_stub_venv(root: Path, marker: str) -> None:
    """A fake venv whose activate prepends a stub `python` printing *marker*."""
    bin_dir = root / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    py = bin_dir / "python"
    py.write_text(f"#!/bin/bash\necho {marker}\nexit 0\n")
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    (bin_dir / "activate").write_text(
        f'export PATH="{bin_dir}:$PATH"\n')


def _make_worktree(tmp: Path, main: Path, name: str = "wt") -> Path:
    """A fake linked worktree: the launcher script + a worktree-style .git FILE."""
    wt = tmp / name
    wt.mkdir()
    shutil.copy(LAUNCHER, wt / "general")
    (wt / "general").chmod((wt / "general").stat().st_mode | stat.S_IEXEC)
    (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/{name}\n")
    (wt / "config.yaml").write_text("apps: []\n")
    return wt


def _run(wt: Path) -> subprocess.CompletedProcess:
    # Strip any live venv from PATH so only the launcher's own activation counts;
    # keep /usr/bin:/bin for bash/sed. macOS has python3 there but NOT `python`,
    # which is exactly the fresh-worktree failure surface.
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp")}
    return subprocess.run(["./general", "roster"], cwd=wt, env=env,
                          capture_output=True, text=True, timeout=30)


# (1) worktree with NO local venv → falls back to the main checkout's venv
tmp = Path(tempfile.mkdtemp(prefix="launcher-venv-"))
main = tmp / "main"
main.mkdir()
_make_stub_venv(main, "MAIN_VENV_OK")
wt = _make_worktree(tmp, main)
r = _run(wt)
ok("venv-less worktree uses the main checkout's venv",
   "MAIN_VENV_OK" in r.stdout,
   f"stdout={r.stdout!r} stderr={r.stderr!r}")
ok("no 'python: not found' in a venv-less worktree",
   "python: not found" not in r.stderr, f"stderr={r.stderr!r}")

# (2) worktree WITH its own venv → local venv wins
_make_stub_venv(wt, "LOCAL_VENV_OK")
r = _run(wt)
ok("local worktree venv preferred over the main checkout's",
   "LOCAL_VENV_OK" in r.stdout and "MAIN_VENV_OK" not in r.stdout,
   f"stdout={r.stdout!r} stderr={r.stderr!r}")

# (3) malformed .git file → launcher must not die in the fallback parse itself
wt2 = _make_worktree(tmp, main, name="wt2")
(wt2 / ".git").write_text("this is not a worktree gitdir line\n")
r = _run(wt2)
ok("malformed .git file doesn't crash the fallback parse",
   "sed" not in r.stderr and "unbound variable" not in r.stderr,
   f"stderr={r.stderr!r}")

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{checks}/{checks} passed")
