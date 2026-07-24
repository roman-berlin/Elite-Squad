"""EU-501 — compact_state_branch pins the exact 4-command git sequence (offline variant).

Stubbed _git (recorder lambda returning CompletedProcess rc=0) AND stubbed
ensure_state_clone (returns a tmp dir with a shared/ subdir) so ZERO real git
processes are spawned.  Full-list equality (== EXPECTED) pins order, count,
and every argument — not just "git was called 4 times".

Mutates ONLY orchestrator.sync at runtime; restores _git / ensure_state_clone
in a finally block so later tests in run_all.py see the genuine functions.
"""
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


# ── report ────────────────────────────────────────────────────────────────────
print("\n================ EU-501 COMPACT_STATE BRANCH (OFFLINE PIN) ================")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("----------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
