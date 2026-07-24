"""EU-500 — compact_state_branch: collapse the unit-state branch's history into a single commit.

Real-git round-trip over a throwaway bare origin (sync_test.py style: the function genuinely runs
switch/add/commit/push) plus a _git recorder that pins the exact git sequence.

Pins:
  • a unit-state branch carrying 2 commits collapses to EXACTLY 1 commit on the remote, with the
    prescribed subject ("compact: collapse unit-state history").
  • the single commit still carries shared/ with the LATEST payload — nothing is deleted (EU-428
    guard): the local branch unit-state survives the rewrite, shared/ survives on disk AND in the
    remote tip.
  • _git is issued EXACTLY the four prescribed commands, in order, with exact args (switch --orphan
    tmp-compact → add shared → commit → push --force origin HEAD:unit-state).
  • a mid-sequence failure (commit) stops BEFORE the force-push and reports {ok, step, error},
    leaving shared/ intact.
  • a repo with no origin fails soft with the same error string git_sync uses — never raises.

Why the round-trip matters: `git switch --orphan` (git ≥ 2.23) removes ALL tracked files from the
working tree (git-switch man page) — in a healthy state clone shared/ IS tracked, so a bare
4-command run dies at `git add shared` ("pathspec 'shared' did not match any files"). The function
snapshots shared/ to a scratch dir across the switch; this harness would go red if that guard
regressed, or if the sequence ever touched `git branch -D` / removed shared/.
"""
import inspect
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

# stub the SDK so importing orchestrator.* is cheap + offline (sync_test.py convention)
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        pass

    def __call__(s, *a, **k):
        return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import sync
from orchestrator.config import Config, AppConfig

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail) if not cond else ""))


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True)


def mkcfg(repo: Path) -> Config:
    return Config(
        apps=[AppConfig(name="automatixy", repo_path=str(repo), base_branch="DEV",
                        protected_branch="MAIN", backlog_backend="none")],
        audit_path=str(repo / "audit.jsonl"), use_worktree=False)


# ── throwaway origin + a "mac" working clone (sync_test.py topology) ───────────────────
tmp = Path(tempfile.mkdtemp())
origin = tmp / "origin.git"
subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], capture_output=True)

git(tmp, "clone", str(origin), "seed")
seed = tmp / "seed"
git(seed, "config", "user.email", "t@t")
git(seed, "config", "user.name", "t")
(seed / "README.md").write_text("seed")
git(seed, "add", ".")
git(seed, "commit", "-m", "init")
git(seed, "push", "origin", "HEAD:main")

git(tmp, "clone", str(origin), "mac")
mac = tmp / "mac"
(mac / "audit.jsonl").write_text(
    '{"event":"ticket_start","ticket_id":"EU500-A","ts":"2026-07-24T10:00:00"}\n')
os.environ["GENERAL_HOST_ID"] = "mac"
cfg_mac = mkcfg(mac)
sd = mac / ".unit-state"

# SANDBOX GUARD (sync_test.py 2026-07-05): this harness runs REAL git_sync + compact — fetch,
# commit, PUSH, FORCE-PUSH. Abort loudly BEFORE any sync if the state dir would land outside our
# tmp sandbox — a _repo_root/state_dir regression must fail this harness, never touch the live
# .unit-state channel.
_resolved_sd = sync.state_dir(cfg_mac).resolve()
assert str(_resolved_sd).startswith(str(tmp.resolve())), (
    f"REFUSING TO SYNC: state_dir resolves OUTSIDE the tmp sandbox ({_resolved_sd}) — "
    "a _repo_root/state_dir regression would corrupt the real .unit-state channel.")

# ── 0. the AC shape: importable, callable with cfg alone ────────────────────────────────
chk("compact_state_branch is importable and takes only cfg",
    list(inspect.signature(sync.compact_state_branch).parameters) == ["cfg"],
    str(inspect.signature(sync.compact_state_branch)))

# ── 1. bootstrap real history: two syncs → two commits on remote unit-state ────────────
r1 = sync.git_sync(cfg_mac)
chk("first sync bootstraps the state clone + pushes", r1["pushed"] and (sd / ".git").exists(), str(r1))
with (mac / "audit.jsonl").open("a") as f:
    f.write('{"event":"merged","ticket_id":"EU500-B","ts":"2026-07-24T10:05:00"}\n')
r2 = sync.git_sync(cfg_mac)
chk("second sync pushes a second commit", r2["pushed"] and not r2["error"], str(r2))

before = tmp / "vbefore"
git(tmp, "clone", "--branch", sync.STATE_BRANCH, "--single-branch", str(origin), "vbefore")
n_before = git(before, "rev-list", "--count", "HEAD").stdout.strip()
chk("precondition: unit-state has multi-commit history before compaction", n_before == "2",
    f"count={n_before}")

# ── 2. the real compact run: 4 git commands, history → 1 commit, nothing deleted ───────
rc = sync.compact_state_branch(cfg_mac)
chk("compact_state_branch(cfg) returns ok with no error", rc["ok"] is True and rc["error"] is None,
    str(rc))

after = tmp / "vafter"
git(tmp, "clone", "--branch", sync.STATE_BRANCH, "--single-branch", str(origin), "vafter")
n_after = git(after, "rev-list", "--count", "HEAD").stdout.strip()
subj = git(after, "log", "-1", "--format=%s").stdout.strip()
tip_files = git(after, "ls-tree", "-r", "--name-only", "HEAD").stdout
tip_audit = (after / "shared" / "mac.jsonl").read_text() if (after / "shared" / "mac.jsonl").exists() else ""
chk("remote unit-state collapsed to exactly 1 commit", n_after == "1", f"count={n_after}")
chk("the single commit carries the prescribed subject",
    subj == "compact: collapse unit-state history", repr(subj))
chk("the single commit still contains shared/ (payload preserved, not deleted)",
    "shared/mac.jsonl" in tip_files and "EU500-A" in tip_audit and "EU500-B" in tip_audit,
    f"files={tip_files!r} audit={tip_audit!r}")

chk("EU-428 guard: local branch unit-state was NOT deleted (no git branch -D)",
    "unit-state" in git(sd, "branch", "--list", "unit-state").stdout,
    git(sd, "branch", "--list").stdout)
chk("EU-428 guard: shared/ still on disk in the state clone (no rm of shared/)",
    (sd / "shared" / "mac.jsonl").exists(), "shared/mac.jsonl missing")
chk("state clone left on tmp-compact (docstring contract)",
    git(sd, "branch", "--show-current").stdout.strip() == "tmp-compact",
    git(sd, "branch", "--show-current").stdout)

# ── 3. reconciliation: the next git_sync heals the tmp-compact checkout ─────────────────
r3 = sync.git_sync(cfg_mac)
chk("next git_sync reconciles the compacted branch without error",
    r3["pulled"] and not r3["error"], str(r3))
chk("peers still visible after compaction + reconciliation", "mac" in r3["hosts"], str(r3.get("hosts")))

# ── 4. recorder: _git sees EXACTLY the four prescribed commands, in order ──────────────
EXPECTED = [
    ("switch", "--orphan", "tmp-compact"),
    ("add", "shared"),
    ("commit", "-m", "compact: collapse unit-state history"),
    ("push", "--force", "origin", f"HEAD:{sync.STATE_BRANCH}"),
]
calls: list[tuple[str, ...]] = []
real_git = sync._git
try:
    sync._git = lambda cwd, *a, **k: calls.append(a) or subprocess.CompletedProcess(("git", *a), 0, "", "")
    rr = sync.compact_state_branch(cfg_mac)
finally:
    sync._git = real_git
chk("recorder: exactly the 4 prescribed git commands, in order, exact args",
    calls == EXPECTED and rr["ok"] is True, f"calls={calls} r={rr}")

# ── 5. failure stops BEFORE the force-push; nothing is deleted on failure ──────────────
calls.clear()


def flaky_git(cwd, *a, **k):
    calls.append(a)
    bad = a[:1] == ("commit",)
    return subprocess.CompletedProcess(("git", *a), 1 if bad else 0, "", "boom" if bad else "")


try:
    sync._git = flaky_git
    rf = sync.compact_state_branch(cfg_mac)
finally:
    sync._git = real_git
chk("commit failure → ok=False with failing step + error surfaced",
    rf["ok"] is False and rf["step"] == "commit -m compact: collapse unit-state history"
    and "boom" in str(rf["error"]), str(rf))
chk("force-push NOT issued after an earlier failure (exactly 3 commands ran)",
    len(calls) == 3 and calls[-1][:1] == ("commit",), f"calls={calls}")
chk("shared/ intact after a failed compaction", (sd / "shared" / "mac.jsonl").exists(),
    "shared/mac.jsonl missing after failed compact")

# ── 6. no origin → fails soft, same error string as git_sync, never raises ─────────────
noremote = tmp / "noremote"
subprocess.run(["git", "init", "-b", "main", str(noremote)], capture_output=True)
(noremote / "audit.jsonl").write_text('{"event":"x","ts":"2026-07-24T01:00:00"}\n')
os.environ["GENERAL_HOST_ID"] = "lonely"
rn = sync.compact_state_branch(mkcfg(noremote))
chk("no-origin compact fails soft (ok=False, error set, step untouched, no crash)",
    rn["ok"] is False and rn["error"] == "no git origin for state sync" and rn["step"] is None,
    str(rn))

print("\n================ EU-500 COMPACT-STATE-BRANCH QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("----------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
