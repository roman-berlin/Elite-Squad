"""EU-497 — pull-only-consumer regression: compaction is transparent to the VPS sync path.

Two legs:
  Leg 1 (offline pin, all git stubbed): GENERAL_SYNC_PULL_ONLY=1 →
    replace sync._git with a recorder returning CompletedProcess rc=0;
    stub sync.ensure_state_clone with _fake_sd (tmp dir + .git + shared/);
    call sync.git_sync(cfg) and assert EXACT full-list equality of recorded git commands
    ('fetch','origin','unit-state') → ('reset','--hard','FETCH_HEAD') — proving no merge/rebase/
    pull/force-fetch fallback exists on the consumer path, and EU-496 compaction issues zero
    git calls under pull_only (pull_only() checked BEFORE _should_compact_state even runs).

  Leg 2 (real local git, zero network — EU-502 precedent): build a temp bare origin over file://,
    seed unit-state with N≥3 commits carrying known shared/<host>.jsonl bytes;
    create a CONSUMER via froot-style repo (git init + remote add origin file://bare) using the
    REAL ensure_state_clone + one pull-only git_sync to land the pre-rewrite tip and capture
    shared/<host>.jsonl bytes on disk;
    from a SEPARATE WRITER root run compact_state_branch(cfg_writer) to rewrite origin to a single
    commit (assert rev-list --count == 1);
    re-sync the consumer (pull-only git_sync again) — asserts pulled=True AND error=None AND
    pushed=None (no ancestry/non-fast-forward failure surfaces) AND
    shared/<host>.jsonl bytes byte-identical to pre-rewrite capture.

All git calls touch ONLY tempfile.mkdtemp paths via file:// URLs (no network).
Restores sync._git / sync.ensure_state_clone and pops all GENERAL_* env keys in finally blocks
so later run_all harnesses see genuine functions and clean env.
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
# LEG 1: OFFLINE PIN — stubbed git, zero processes
# ═══════════════════════════════════════════════════════════════════════════════

_ROOT1 = Path(tempfile.mkdtemp(prefix="eu497-l1-"))

cfg1 = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(_ROOT1), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(_ROOT1 / "audit.jsonl"),
    use_worktree=False,
)


def _fake_sd1(_cfg: Config) -> Path:
    """Minimal .git dir + shared/ so git_sync finds a valid clone."""
    sd = _ROOT1 / ".unit-state-eu497l1"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / ".git").write_text("gitdir: .\n")
    (sd / "shared").mkdir(parents=True, exist_ok=True)
    return sd


_EXPECTED_L1 = [
    ("fetch", "origin", "unit-state"),
    ("reset", "--hard", "FETCH_HEAD"),
]

real_git = sync._git
real_ensure = sync.ensure_state_clone
calls_l1: list[tuple[str, ...]] = []


def _rec_l1(cwd, *a, **k):
    """Recorder: append the command tuple and return rc=0."""
    calls_l1.append(a)
    return subprocess.CompletedProcess(("git", *a), 0, "", "")


try:
    os.environ["GENERAL_SYNC_PULL_ONLY"] = "1"
    os.environ["GENERAL_HOST_ID"] = "vps-test-host"

    sync._git = _rec_l1
    sync.ensure_state_clone = _fake_sd1

    r = sync.git_sync(cfg1)

    # AC1: Exact git command sequence — full-list equality pins order, count, every argument.
    chk("AC1-L1: exact git commands (full-list equality)",
        calls_l1 == _EXPECTED_L1,
        f"expected {_EXPECTED_L1}\n     got   {calls_l1}")

    # AC1b: proved no merge/rebase/pull/force-fetch fallback,
    # and no compaction git call (pull_only() gate blocks _should_compact_state entirely).
    chk("AC1b-L1: NO extra commands beyond fetch+reset (length)",
        len(calls_l1) == 2,
        f"got {len(calls_l1)} commands")

    # pushed must be None (read-only consumer never attempts a push).
    chk("pushed is None (read-only consumer)",
        r.get("pushed") is None,
        f"pushed={r.get('pushed')!r}")

    # pulled must be True (the fetch + reset succeeded).
    chk("pulled is True (consumer landed peers' audits)",
        r.get("pulled") is True,
        f"pulled={r.get('pulled')!r}")

    # No error surfaced from the fetch/reset cycle.
    chk("error is None (no non-fast-forward / ancestry error)",
        r.get("error") is None,
        f"error={r.get('error')!r}")

finally:
    sync._git = real_git
    sync.ensure_state_clone = real_ensure
    os.environ.pop("GENERAL_SYNC_PULL_ONLY", None)
    os.environ.pop("GENERAL_HOST_ID", None)


# ═══════════════════════════════════════════════════════════════════════════════
# LEG 2: REAL LOCAL GIT — temp bare origin over file://, zero network
# ═══════════════════════════════════════════════════════════════════════════════

_results_l2: list[tuple[str, bool, str]] = []


def _chk2(name: str, cond, detail: str = "") -> None:
    _results_l2.append((name, bool(cond), str(detail) if not cond else ""))


_TMP2 = Path(tempfile.mkdtemp(prefix="eu497-l2-"))
KNOWN_HOST = "eu497-vps"

os.environ["GENERAL_HOST_ID"] = KNOWN_HOST
os.environ["GENERAL_STATE_COMPACT_THRESHOLD"] = "10"   # high enough not to trigger
os.environ["GENERAL_STATE_COMPACT_DAYS"] = "1"          # old sidecar would fire otherwise

# ── Setup: bare origin with unit-state branch carrying N ≥ 3 commits ───────
bare = _TMP2 / "origin.git"
subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)],
               capture_output=True)

# Seed a working clone to create commits on unit-state
seed = _TMP2 / "seed"
subprocess.run(["git", "clone", str(bare), str(seed)], capture_output=True)
subprocess.run(["git", "config", "user.email", "t@t"], cwd=seed)
subprocess.run(["git", "config", "user.name", "t"], cwd=seed)

# Create unit-state orphan + seed with 3 commits of known payload
subprocess.run(["git", "switch", "--orphan", "unit-state"], cwd=seed, capture_output=True)
(p1 := (seed / "shared")).mkdir(exist_ok=True)
payload1 = b'{"event":"ticket_start","ticket_id":"AUTO-1","ts":"2026-07-01T10:00:00"}\n'
(p1 / f"{KNOWN_HOST}.jsonl").write_bytes(payload1)
subprocess.run(["git", "add", "shared"], cwd=seed, capture_output=True)
subprocess.run(["git", "commit", "-m", "v1: initial events"], cwd=seed, capture_output=True)
subprocess.run(["git", "push", "origin", "HEAD:unit-state"], cwd=seed, capture_output=True)

payload2 = b'{"event":"merged","ticket_id":"AUTO-1","ts":"2026-07-01T10:05:00"}\n'
(p1 / f"{KNOWN_HOST}.jsonl").write_bytes(payload2)
subprocess.run(["git", "add", "shared"], cwd=seed, capture_output=True)
subprocess.run(["git", "commit", "-m", "v2: merged"], cwd=seed, capture_output=True)
subprocess.run(["git", "push", "origin", "HEAD:unit-state"], cwd=seed, capture_output=True)

payload3 = b'{"event":"build_complete","ticket_id":"AUTO-2","ts":"2026-07-01T11:00:00"}\n'
(p1 / f"{KNOWN_HOST}.jsonl").write_bytes(payload3)
subprocess.run(["git", "add", "shared"], cwd=seed, capture_output=True)
subprocess.run(["git", "commit", "-m", "v3: build complete"], cwd=seed, capture_output=True)
subprocess.run(["git", "push", "origin", "HEAD:unit-state"], cwd=seed, capture_output=True)

pre_count = subprocess.run(
    ["git", "rev-list", "--count", "unit-state"],
    cwd=bare, capture_output=True, text=True)
_chk2("AC2-setup: origin unit-state has ≥ 3 commits pre-rewrite",
      int(pre_count.stdout.strip()) >= 3,
      f"got {pre_count.stdout.strip()}")

# ── Consumer: clone + first pull-only git_sync to land pre-rewrite tip ─────
consumer_root = _TMP2 / "consumer"
subprocess.run(["git", "init", "-b", "main", str(consumer_root)], capture_output=True)
subprocess.run(["git", "remote", "add", "origin", f"file://{bare}"],
               cwd=consumer_root, capture_output=True)

consumer_cfg = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(consumer_root), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(consumer_root / "audit.jsonl"),
    use_worktree=False,
)

# SANDBOX GUARD
_resolved = sync.state_dir(consumer_cfg).resolve()
assert str(_resolved).startswith(str(_TMP2.resolve())), (
    f"REFUSING TO SYNC: state_dir resolves OUTSIDE the tmp sandbox ({_resolved})")

# Run pull-only consumer — this bootstraps the state clone + pulls peers
os.environ["GENERAL_SYNC_PULL_ONLY"] = "1"
r_consume1 = sync.git_sync(consumer_cfg)

if (_resolved / "shared" / f"{KNOWN_HOST}.jsonl").exists():
    pre_bytes = (_resolved / "shared" / f"{KNOWN_HOST}.jsonl").read_bytes()
else:
    pre_bytes = b""

_chk2("AC2: consumer lands without error after first pull-only sync",
      r_consume1.get("error") is None and r_consume1.get("pulled") is True,
      f"r={r_consume1}")

_chk2("AC2: consumer sees pre-rewrite content on disk",
      pre_bytes == payload3,
      f"expected {len(payload3)} bytes, got {len(pre_bytes)} bytes")

os.environ.pop("GENERAL_SYNC_PULL_ONLY", None)

# ── Writer: compact_state_branch rewrites origin to a single commit ───────
writer_root = _TMP2 / "writer"
subprocess.run(["git", "init", "-b", "main", str(writer_root)], capture_output=True)
subprocess.run(["git", "remote", "add", "origin", f"file://{bare}"],
               cwd=writer_root, capture_output=True)

writer_cfg = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(writer_root), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(writer_root / "audit.jsonl"),
    use_worktree=False,
)

_resolved_w = sync.state_dir(writer_cfg).resolve()
assert str(_resolved_w).startswith(str(_TMP2.resolve())), (
    f"REFUSING TO SYNC: writer state_dir resolves OUTSIDE the tmp sandbox ({_resolved_w})")

compact_rc = sync.compact_state_branch(writer_cfg)
_chk2("Compact returned ok=True, error=None",
      compact_rc.get("ok") is True,
      f"rc={compact_rc}")

post_count = subprocess.run(
    ["git", "rev-list", "--count", "unit-state"],
    cwd=bare, capture_output=True, text=True)
_chk2("After compact: origin unit-state collapsed to exactly 1 commit",
      post_count.returncode == 0 and int(post_count.stdout.strip()) == 1,
      f"count={post_count.stdout.strip().rstrip()}")

# ── Consumer second pull-only sync: should succeed, no ancestry error ────
os.environ["GENERAL_SYNC_PULL_ONLY"] = "1"
r_consume2 = sync.git_sync(consumer_cfg)

_chk2("AC3: post-rewrite pull-only sync succeeds (pulled=True, error=None, pushed=None)",
      r_consume2.get("pulled") is True
      and r_consume2.get("error") is None
      and r_consume2.get("pushed") is None,
      f"r={r_consume2}")

if (_resolved / "shared" / f"{KNOWN_HOST}.jsonl").exists():
    post_bytes = (_resolved / "shared" / f"{KNOWN_HOST}.jsonl").read_bytes()
else:
    post_bytes = b""

_chk2("AC4: post-rewrite shared/<host>.jsonl is byte-identical to pre-rewrite",
      post_bytes == pre_bytes,
      f"pre={len(pre_bytes)} bytes, post={len(post_bytes)} bytes")

os.environ.pop("GENERAL_SYNC_PULL_ONLY", None)

# ── Cleanup ────────────────────────────────────────────────────────────────
sync._git = real_git
sync.ensure_state_clone = real_ensure
os.environ.pop("GENERAL_STATE_COMPACT_THRESHOLD", None)
os.environ.pop("GENERAL_STATE_COMPACT_DAYS", None)

# ── Report ─────────────────────────────────────────────────────────────────
print("\n========== EU-497 PULL-ONLY CONSUMER REGRESSION ===============")
for name, ok, detail in results + _results_l2:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
total_l1 = sum(1 for _, ok, _ in results if ok)
total_l2 = sum(1 for _, ok, _ in _results_l2 if ok)
pass_l1 = len([x for x in results if x[1]])
pass_l2 = len([x for x in _results_l2 if x[1]])
passed = pass_l1 + pass_l2
total = len(results) + len(_results_l2)
print("-----------------------------------------------------------")
print(f"  {passed}/{total} passed  (leg1: {pass_l1}/{len(results)}, leg2: {pass_l2}/{len(_results_l2)})")
print("  RESULT:", "ALL GREEN" if passed == total else f"{total - passed} FAIL")
sys.exit(0 if passed == total else 1)
