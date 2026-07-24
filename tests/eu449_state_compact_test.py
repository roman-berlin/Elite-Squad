"""EU-501 — compact_state_branch pins the exact 4-command git sequence (offline variant).

Stubbed _git (recorder lambda returning CompletedProcess rc=0) AND stubbed
ensure_state_clone (returns a tmp dir with a shared/ subdir) so ZERO real git
processes are spawned.  Full-list equality (== EXPECTED) pins order, count,
and every argument — not just "git was called 4 times".

Mutates ONLY orchestrator.sync at runtime; restores _git / ensure_state_clone
in a finally block so later tests in run_all.py see the genuine functions.

EU-502 — integration-style: real temp-bare-origin + multi-commit history →
compact_state_branch(cfg) → verify single-commit result + byte-identical content
+ branch/file survive (EU-428 guard). Runs AFTER the offline section's finally
restores real sync._git / sync.ensure_state_clone.
"""
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

# ── SDK stub (tests/sync_test.py convention) ───────────────────────────────────
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        pass

    def __call__(s, *a, **k):
        return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import sync  # noqa: E402
from orchestrator.config import AppConfig, Config  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail) if not cond else ""))


# ═══════════════════════════════════════════════════════════════════════════════
# EU-501 — OFFLINE PIN (stubbed git, zero processes)
# ═══════════════════════════════════════════════════════════════════════════════

# ── stub cfg + env (no real repo needed) ──────────────────────────────────────
TMP_ROOT = Path(tempfile.mkdtemp(prefix="eu449-"))
HOST_ID_FILE = TMP_ROOT / "host_id"
HOST_ID_FILE.write_text("test-host")

# Minimal Config that points state_dir into our sandbox.
cfg = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(TMP_ROOT), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(TMP_ROOT / "audit.jsonl"),
    use_worktree=False,
)


def fake_state_dir(_cfg: Config) -> Path:
    """Return a clean temp dir with .git + shared/ so compact_state_branch finds a valid clone."""
    sd = TMP_ROOT / ".unit-state-eu449"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / ".git").write_text("gitdir: .\n")
    (sd / "shared").mkdir(parents=True, exist_ok=True)
    (sd / "shared" / "x.txt").write_text("payload\n")
    return sd


# ── expectations ──────────────────────────────────────────────────────────────
EXPECTED = [
    ("switch", "--orphan", "tmp-compact"),
    ("add", "shared"),
    ("commit", "-m", "compact: collapse unit-state history"),
    ("push", "--force", "origin", "HEAD:unit-state"),
]

# ── record & run ──────────────────────────────────────────────────────────────
real_git = sync._git
real_ensure = sync.ensure_state_clone
calls: list[tuple[str, ...]] = []


def rec(cwd, *a, **k):
    """Recorder lambda per the eu500 pattern."""
    calls.append(a)
    return subprocess.CompletedProcess(("git", *a), 0, "", "")


try:
    sync._git = rec
    sync.ensure_state_clone = lambda _c: fake_state_dir(_c)

    rc = sync.compact_state_branch(cfg)

    # ── assertions ──────────────────────────────────────────────────────────
    chk("_git recorder captured exactly 4 commands", len(calls) == 4,
        f"captured {len(calls)} commands: {calls}")

    chk("exact command order + arguments (full-list equality)",
        calls == EXPECTED,
        f"expected {EXPECTED}\n     got   {calls}")

    chk("return ok=True, error=None under happy-stub",
        rc["ok"] is True and rc["error"] is None,
        f"rc={rc}")

    # Note: the function sets out["step"] ONLY on failure (to report which command failed).
    # On success step stays None — this is intentional design.
    chk("step is None on success (design: only set on failure)",
        rc.get("step") is None,
        f"unexpected step={rc.get('step')}")

    chk("all recorded commands are non-empty tuples (recorder ran fully)",
        all(len(c) > 0 for c in calls),
        f"empty command found in {calls}")

finally:
    sync._git = real_git
    sync.ensure_state_clone = real_ensure


# ── report (EU-501) ──────────────────────────────────────────────────────────
eu501_total = len(results)
print("\n================ EU-501 COMPACT_STATE BRANCH (OFFLINE PIN) ================")
for name, ok, detail in results[:eu501_total]:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("----------------------------------------------------------------")
eu501_passed = sum(1 for _, ok, _ in results[:eu501_total] if ok)
print(f"  {eu501_passed}/{eu501_total} passed")
print("  RESULT:", "ALL GREEN" if eu501_passed == eu501_total else f"{eu501_total - eu501_passed} FAIL")


# ═══════════════════════════════════════════════════════════════════════════════
# EU-502 — INTEGRATION TEST (real temp git repo, real compact_state_branch)
# ═══════════════════════════════════════════════════════════════════════════════

# Uses a local bare-origin + clones with REAL git (no _git stub), exercising
# ensure_state_clone → compact_state_branch → push --force end-to-end.
# All verification runs against the BARE ORIGIN (not the local state clone
# which ends up on tmp-compact after compaction, per the docstring note at
# orchestrator/sync.py:249).

eu502_results: list[tuple[str, bool, str]] = []


def _chk502(name: str, cond, detail: str = "") -> None:
    eu502_results.append((name, bool(cond), str(detail) if not cond else ""))


_TMP2 = Path(tempfile.mkdtemp(prefix="eu502-"))

# ── seed bare origin with one default commit on main ─────────────────────────
origin = _TMP2 / "origin.git"
subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
               capture_output=True)

seed = _TMP2 / "seed"
subprocess.run(["git", "clone", str(origin), str(seed)], capture_output=True)
subprocess.run(["git", "config", "user.email", "t@t"], cwd=seed)
subprocess.run(["git", "config", "user.name", "t"], cwd=seed)
(seed / "README.md").write_text("seed")
subprocess.run(["git", "add", "."], cwd=seed)
subprocess.run(["git", "commit", "-m", "init"], cwd=seed)
subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=seed)

# ── seed clone → create orphan unit-state with ≥2 commits ────────────────────
src = _TMP2 / "src"
subprocess.run(["git", "clone", str(origin), str(src)], capture_output=True)
subprocess.run(["git", "config", "user.email", "t@t"], cwd=src)
subprocess.run(["git", "config", "user.name", "t"], cwd=src)

# Switch to orphan unit-state (first commit bootstraps the branch)
subprocess.run(["git", "switch", "--orphan", "unit-state"], cwd=src)

# Set GENERAL_HOST_ID so shared/<host>.jsonl gets a deterministic filename
KNOWN_HOST = "integration-test-host"
os.environ["GENERAL_HOST_ID"] = KNOWN_HOST

# Commit 1
first_payload = '{"event":"ticket_start","ts":"2026-07-01T10:00:00"}\n'
(src / "shared").mkdir(exist_ok=True)
(src / "shared" / f"{KNOWN_HOST}.jsonl").write_bytes(first_payload.encode())
subprocess.run(["git", "add", "shared"], cwd=src)
subprocess.run(["git", "commit", "-m", "v1: initial events"], cwd=src)
subprocess.run(["git", "push", "origin", "HEAD:unit-state"], cwd=src)

# Commit 2 — last-commit payload that compact should preserve byte-for-byte
second_payload = b'{"event":"merged","ticket_id":"AUTO-42","ts":"2026-07-01T12:00:00"}\n{"event":"ticket_start","ticket_id":"AUTO-99","ts":"2026-07-01T12:05:00"}\n'
(src / "shared" / f"{KNOWN_HOST}.jsonl").write_bytes(second_payload)
subprocess.run(["git", "add", "shared"], cwd=src)
subprocess.run(["git", "commit", "-m", "v2: more events"], cwd=src)
subprocess.run(["git", "push", "origin", "HEAD:unit-state"], cwd=src)

# Verify pre-compaction: multi-commit history exists (sanity check)
pre_count = subprocess.run(
    ["git", "rev-list", "--count", "unit-state"],
    cwd=origin, capture_output=True, text=True
)
_chk502("AC1 seed sanity: unit-state has ≥ 2 commits before compact",
        int(pre_count.stdout.strip()) >= 2,
        f"got {pre_count.stdout.strip()}")

# ── build cfg pointing INSIDE the src clone (so state_dir resolves to <src>/.unit-state/)
cfg502 = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(src), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(src / "audit.jsonl"),
    use_worktree=False,
)

# ── SANDBOX GUARD (sync_test.py :60-63): abort before sync if state_dir escapes tmp ──
_resolved_sd = sync.state_dir(cfg502).resolve()
assert str(_resolved_sd).startswith(str(_TMP2.resolve())), (
    f"REFUSING TO SYNC: state_dir resolves OUTSIDE the tmp sandbox ({_resolved_sd})")

# ── call compact with REAL git (no stubs — the finally above restored originals) ──
rc502 = sync.compact_state_branch(cfg502)

# ── verifications against the BARE ORIGIN (local clone sits on tmp-compact) ───

# AC2: ok=True, error=None
_chk502("compact returned ok=True, error=None",
        rc502["ok"] is True and rc502["error"] is None,
        f"rc={rc502}")

# AC1: single-commit result
post_count = subprocess.run(
    ["git", "rev-list", "--count", "unit-state"],
    cwd=origin, capture_output=True, text=True
)
_chk502("after compact: unit-state reports exactly 1 commit (git rev-list --count)",
        post_count.returncode == 0 and int(post_count.stdout.strip()) == 1,
        f"returncode={post_count.returncode} count={post_count.stdout.strip().rstrip()}")

# AC3: byte-identical content comparison
shown = subprocess.run(
    ["git", "show", f"unit-state:shared/{KNOWN_HOST}.jsonl"],
    cwd=origin, capture_output=True
)
_chk502("AC3: shared/<host>.jsonl bytes match seeded payload exactly",
        shown.returncode == 0 and shown.stdout == second_payload,
        f"expected {len(second_payload)} bytes, got {len(shown.stdout)} bytes"
        + (f"\nseeded={second_payload!r}\ngot    ={shown.stdout!r}" if shown.stdout != second_payload else ""))

# AC4: unit-state branch still exists (EU-428 guard — nothing deleted)
branch_check = subprocess.run(
    ["git", "rev-parse", "--verify", "unit-state"],
    cwd=origin, capture_output=True, text=True
)
_chk502("AC4: unit-state branch still exists post-compaction (EU-428 guard)",
        branch_check.returncode == 0,
        f"rev-parse exit={branch_check.returncode}")

# AC4: shared/<host>.jsonl present in tree + on disk in local clone
blob_hash = subprocess.run(
    ["git", "rev-parse", f"unit-state:shared/{KNOWN_HOST}.jsonl"],
    cwd=origin, capture_output=True, text=True
)
_chk502("AC4: shared/<host>.jsonl resolved to a valid tree blob",
        blob_hash.returncode == 0 and blob_hash.stdout.strip(),
        f"hash={blob_hash.stdout.strip()!r} rc={blob_hash.returncode}")

state_file_on_disk = (_resolved_sd / "shared" / f"{KNOWN_HOST}.jsonl")
_chk502("AC4: shared/<host>.jsonl exists on disk in local state clone",
        state_file_on_disk.is_file(),
        f"path={state_file_on_disk}")


# ── report (EU-502) ──────────────────────────────────────────────────────────
print("\n========== EU-502 COMPACT STATE — INTEGRATION (REAL GIT, SINGLE-COMMIT) ===")
for name, ok, detail in eu502_results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("----------------------------------------------------------------")
eu502_passed = sum(1 for _, ok, _ in eu502_results if ok)
print(f"  {eu502_passed}/{len(eu502_results)} passed")
print("  RESULT:", "ALL GREEN" if eu502_passed == len(eu502_results)
      else f"{len(eu502_results) - eu502_passed} FAIL")

overall_total = len(results) + len(eu502_results)
overall_passed = eu501_passed + eu502_passed
print(f"\n{'='*76}")
print(f"  OVERALL: {overall_passed}/{overall_total} (EU-501: {eu501_passed}/{eu501_total} | EU-502: {eu502_passed}/{len(eu502_results)})")
print(f"{'='*76}")
sys.exit(0 if overall_passed == overall_total else 1)
