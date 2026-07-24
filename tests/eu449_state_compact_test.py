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

EU-496 — _should_compact_state throttle + best-effort exception wrapping in git_sync
(iteration 2). Stubbed groups A–E drive the skip/trigger/cadence/raise/pull_only paths
with recorders; group G proves the verdict is re-evaluated on EVERY call (no
process-lifetime cache left to freeze the throttle in a long-lived process); group F
drives leg (a) through the REAL _git against a temp bare origin whose unit-state branch
carries more commits than the default threshold — the depth-50 bootstrap clone is
shallow, so the gate must unshallow before counting for the threshold to be reachable
at all — then asserts the gate does NOT re-fire immediately after a real compaction.
Group D captures stdout to prove the swallow log carries the exception detail.
Restores real functions in every finally so no later harness sees mutated sync.
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
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

    chk("_git recorder captured exactly 4 commands", len(calls) == 4,
        f"captured {len(calls)} commands: {calls}")
    chk("exact command order + arguments (full-list equality)",
        calls == EXPECTED, f"expected {EXPECTED}\n     got   {calls}")
    chk("return ok=True, error=None under happy-stub",
        rc["ok"] is True and rc["error"] is None, f"rc={rc}")
    chk("step is None on success (design: only set on failure)",
        rc.get("step") is None, f"unexpected step={rc.get('step')}")
    chk("all recorded commands are non-empty tuples (recorder ran fully)",
        all(len(c) > 0 for c in calls), f"empty command found in {calls}")

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

eu502_results: list[tuple[str, bool, str]] = []


def _chk502(name: str, cond, detail: str = "") -> None:
    eu502_results.append((name, bool(cond), str(detail) if not cond else ""))


_TMP2 = Path(tempfile.mkdtemp(prefix="eu502-"))

origin = _TMP2 / "origin.git"
subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], capture_output=True)

seed = _TMP2 / "seed"
subprocess.run(["git", "clone", str(origin), str(seed)], capture_output=True)
subprocess.run(["git", "config", "user.email", "t@t"], cwd=seed)
subprocess.run(["git", "config", "user.name", "t"], cwd=seed)
(seed / "README.md").write_text("seed")
subprocess.run(["git", "add", "."], cwd=seed)
subprocess.run(["git", "commit", "-m", "init"], cwd=seed)
subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=seed)

src = _TMP2 / "src"
subprocess.run(["git", "clone", str(origin), str(src)], capture_output=True)
subprocess.run(["git", "config", "user.email", "t@t"], cwd=src)
subprocess.run(["git", "config", "user.name", "t"], cwd=src)
subprocess.run(["git", "switch", "--orphan", "unit-state"], cwd=src)

KNOWN_HOST = "integration-test-host"
os.environ["GENERAL_HOST_ID"] = KNOWN_HOST

first_payload = '{"event":"ticket_start","ts":"2026-07-01T10:00:00"}\n'
(src / "shared").mkdir(exist_ok=True)
(src / "shared" / f"{KNOWN_HOST}.jsonl").write_bytes(first_payload.encode())
subprocess.run(["git", "add", "shared"], cwd=src)
subprocess.run(["git", "commit", "-m", "v1: initial events"], cwd=src)
subprocess.run(["git", "push", "origin", "HEAD:unit-state"], cwd=src)

second_payload = b'{"event":"merged","ticket_id":"AUTO-42","ts":"2026-07-01T12:00:00"}\n{"event":"ticket_start","ticket_id":"AUTO-99","ts":"2026-07-01T12:05:00"}\n'
(src / "shared" / f"{KNOWN_HOST}.jsonl").write_bytes(second_payload)
subprocess.run(["git", "add", "shared"], cwd=src)
subprocess.run(["git", "commit", "-m", "v2: more events"], cwd=src)
subprocess.run(["git", "push", "origin", "HEAD:unit-state"], cwd=src)

pre_count = subprocess.run(
    ["git", "rev-list", "--count", "unit-state"],
    cwd=origin, capture_output=True, text=True
)
_chk502("AC1 seed sanity: unit-state has ≥ 2 commits before compact",
        int(pre_count.stdout.strip()) >= 2, f"got {pre_count.stdout.strip()}")

cfg502 = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(src), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(src / "audit.jsonl"),
    use_worktree=False,
)

_resolved_sd = sync.state_dir(cfg502).resolve()
assert str(_resolved_sd).startswith(str(_TMP2.resolve())), (
    f"REFUSING TO SYNC: state_dir resolves OUTSIDE the tmp sandbox ({_resolved_sd})")

rc502 = sync.compact_state_branch(cfg502)

_chk502("compact returned ok=True, error=None",
        rc502["ok"] is True and rc502["error"] is None, f"rc={rc502}")

post_count = subprocess.run(
    ["git", "rev-list", "--count", "unit-state"],
    cwd=origin, capture_output=True, text=True
)
_chk502("after compact: unit-state reports exactly 1 commit (git rev-list --count)",
        post_count.returncode == 0 and int(post_count.stdout.strip()) == 1,
        f"returncode={post_count.returncode} count={post_count.stdout.strip().rstrip()}")

shown = subprocess.run(
    ["git", "show", f"unit-state:shared/{KNOWN_HOST}.jsonl"],
    cwd=origin, capture_output=True
)
_chk502("AC3: shared/<host>.jsonl bytes match seeded payload exactly",
        shown.returncode == 0 and shown.stdout == second_payload,
        f"expected {len(second_payload)} bytes, got {len(shown.stdout)} bytes")

branch_check = subprocess.run(
    ["git", "rev-parse", "--verify", "unit-state"],
    cwd=origin, capture_output=True, text=True
)
_chk502("AC4: unit-state branch still exists post-compaction (EU-428 guard)",
        branch_check.returncode == 0, f"rev-parse exit={branch_check.returncode}")

blob_hash = subprocess.run(
    ["git", "rev-parse", f"unit-state:shared/{KNOWN_HOST}.jsonl"],
    cwd=origin, capture_output=True, text=True
)
_chk502("AC4: shared/<host>.jsonl resolved to a valid tree blob",
        blob_hash.returncode == 0 and blob_hash.stdout.strip(),
        f"hash={blob_hash.stdout.strip()!r} rc={blob_hash.returncode}")

state_file_on_disk = (_resolved_sd / "shared" / f"{KNOWN_HOST}.jsonl")
_chk502("AC4: shared/<host>.jsonl exists on disk in local state clone",
        state_file_on_disk.is_file(), f"path={state_file_on_disk}")


# ── report (EU-502) ──────────────────────────────────────────────────────────
print("\n========== EU-502 COMPACT STATE — INTEGRATION (REAL GIT, SINGLE-COMMIT) ===")
for name, ok, detail in eu502_results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("----------------------------------------------------------------")
eu502_passed = sum(1 for _, ok, _ in eu502_results if ok)
print(f"  {eu502_passed}/{len(eu502_results)} passed")
print("  RESULT:", "ALL GREEN" if eu502_passed == len(eu502_results)
      else f"{len(eu502_results) - eu502_passed} FAIL")


# ═══════════════════════════════════════════════════════════════════════════════
# EU-496 — _should_compact_state throttle + best-effort exception wrapping
# ═══════════════════════════════════════════════════════════════════════════════

eu496_results: list[tuple[str, bool, str]] = []


def _chk496(name: str, cond, detail: str = "") -> None:
    eu496_results.append((name, bool(cond), str(detail) if not cond else ""))


# Each subgroup builds its own isolated sandbox so cross-group state cannot leak.
def _eu496_sandbox():
    t = Path(tempfile.mkdtemp(prefix="eu496-"))
    return t


def _eu496_cfg(root):
    return Config(
        apps=[AppConfig(name="automatixy", repo_path=str(root), base_branch="DEV",
                        protected_branch="MAIN", backlog_backend="none")],
        audit_path=str(root / "audit.jsonl"),
        use_worktree=False,
    )


# Global restore targets for EU-496.
_real_git_496 = sync._git
_real_ensure_496 = sync.ensure_state_clone
_real_compact_496 = sync.compact_state_branch


def _reset_env_496():
    for k in ("GENERAL_STATE_COMPACT_THRESHOLD", "GENERAL_STATE_COMPACT_DAYS",
              "GENERAL_SYNC_PULL_ONLY"):
        os.environ.pop(k, None)


def _fake_sd(root):
    """Minimal .git dir + shared/ so compact_state_branch/ensure_state_clone find a valid clone."""
    sd = root / ".unit-state-fake"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / ".git").write_text("gitdir: .\n")
    (sd / "shared").mkdir(parents=True, exist_ok=True)
    return sd


# Helper: build a CompletedProcess-like return that _should_compact_state parses.
def _cp(rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(("git",), rc, stdout, stderr)


# ──────────────────────────────────────────────────────────────────────────────
# Test group A: Skip path — _should_compact_state → False, compact NOT called
# ──────────────────────────────────────────────────────────────────────────────

root_a = _eu496_sandbox()
sc_a = root_a / "last_state_compact.txt"

try:
    _reset_env_496()

    # Fresh sidecar + low commit count → False (natural path — no cache to force it).
    sc_a.write_text(str(int(time.time())))  # current timestamp

    def _rec_a(cwd, *a, **kw):
        if a == ("rev-list", "--count", "unit-state"):
            return _cp(0, "10\n")
        return _cp(0)

    sync._git = _rec_a
    sync.ensure_state_clone = lambda _c: _fake_sd(root_a)

    r = sync._should_compact_state(_eu496_cfg(root_a))
    _chk496("A1: _should_compact_state → False under normal conditions",
            r is False, f"got {r}")

    # Drive git_sync and verify compact_state_branch is NOT called.
    compact_seen_a = []

    def _compact_nop(*args, **kw):
        compact_seen_a.append(1)
        return {"ok": True, "step": None, "error": None}

    try:
        sync.compact_state_branch = _compact_nop
        sync._git = _rec_a  # ensure fetch/pull/rc=0 path
        # ensure_state_clone already stubbed above — but git_sync calls it internally;
        # make sure the latest stub (from above) is active by re-setting:
        sync.ensure_state_clone = lambda _c: _fake_sd(root_a)

        result_sync = sync.git_sync(_eu496_cfg(root_a))
        _chk496("A2: git_sync returns a valid dict when compact is skipped",
                isinstance(result_sync, dict), f"got {type(result_sync).__name__}")
        _chk496("A3: compact_state_branch NOT called when _should_compact_state → False",
                len(compact_seen_a) == 0, f"was called {len(compact_seen_a)} times")
    finally:
        sync.compact_state_branch = _real_compact_496
finally:
    sync._git = _real_git_496
    sync.ensure_state_clone = _real_ensure_496


# ──────────────────────────────────────────────────────────────────────────────
# Test group B: Trigger path — commit-count threshold exceeded
# ──────────────────────────────────────────────────────────────────────────────

root_b = _eu496_sandbox()

try:
    _reset_env_496()

    # Ensure no sidecar → prevents cadence leg from overriding.
    sc_b = root_b / "last_state_compact.txt"
    if sc_b.exists():
        sc_b.unlink()

    # Default threshold = 150, commit count = 151 → True (fires naturally off the stubbed count).
    def _rec_b(cwd, *a, **kw):
        if a == ("rev-list", "--count", "unit-state"):
            return _cp(0, "151\n")
        return _cp(0)

    sync._git = _rec_b
    sync.ensure_state_clone = lambda _c: _fake_sd(root_b)

    r = sync._should_compact_state(_eu496_cfg(root_b))
    _chk496("B1: _should_compact_state → True when commit count > 150",
            r is True, f"got {r}")

    # Env override: threshold=5, count=6 → True (the gate re-reads env on every call).
    os.environ["GENERAL_STATE_COMPACT_THRESHOLD"] = "5"

    def _rec_b_low(cwd, *a, **kw):
        if a == ("rev-list", "--count", "unit-state"):
            return _cp(0, "6\n")
        return _cp(0)

    sync._git = _rec_b_low
    r = sync._should_compact_state(_eu496_cfg(root_b))
    _chk496("B2: _should_compact_state → True with lowered env threshold (6 > 5)",
            r is True, f"got {r}")
    del os.environ["GENERAL_STATE_COMPACT_THRESHOLD"]

    # Drive git_sync: the over-threshold count triggers compaction naturally (no cache forcing).
    compact_seen_b = []

    def _compact_record_b(*args, **kw):
        compact_seen_b.append(1)
        return {"ok": True, "step": None, "error": None}

    try:
        sync.compact_state_branch = _compact_record_b
        sync._git = _rec_b       # git_sync fetch/pull → rc=0; rev-list count → 151 (trigger)
        sync.ensure_state_clone = lambda _c: _fake_sd(root_b)

        result_sync = sync.git_sync(_eu496_cfg(root_b))
        _chk496("B3: compact_state_branch called EXACTLY ONCE when triggered",
                len(compact_seen_b) == 1,
                f"called {len(compact_seen_b)} times")
        _chk496("B4: git_sync returns a dict and does not crash when compact is triggered",
                isinstance(result_sync, dict), f"got {type(result_sync).__name__}")

        # Verify sidecar updated after successful compaction.
        if sc_b.exists():
            ts_val = float(sc_b.read_text(encoding="utf-8", errors="replace").strip())
            age = time.time() - ts_val
            _chk496("B5: sidecar timestamp written (within 60 s) after successful compact",
                    age < 60, f"sidecar age={age:.1f}s")
    finally:
        sync.compact_state_branch = _real_compact_496
finally:
    sync._git = _real_git_496
    sync.ensure_state_clone = _real_ensure_496


# ──────────────────────────────────────────────────────────────────────────────
# Test group C: Weekly cadence leg (old sidecar / absent sidecar)
# ──────────────────────────────────────────────────────────────────────────────

root_c = _eu496_sandbox()

try:
    _reset_env_496()

    def _rec_c(cwd, *a, **kw):
        if a == ("rev-list", "--count", "unit-state"):
            return _cp(0, "5\n")  # low count — rely on cadence only
        return _cp(0)

    sync._git = _rec_c
    sync.ensure_state_clone = lambda _c: _fake_sd(root_c)
    sc_c = root_c / "last_state_compact.txt"

    # C1: Sidecar absent → False (fresh clone, no storm).
    if sc_c.exists():
        sc_c.unlink()
    r = sync._should_compact_state(_eu496_cfg(root_c))
    _chk496("C1: _should_compact_state → False when sidecar absent (fresh clone)",
            r is False, f"got {r}")

    # C2: Sidecar 8 days old → True.
    old_ts = int(time.time()) - (8 * 86400)
    sc_c.write_text(str(old_ts))
    r = sync._should_compact_state(_eu496_cfg(root_c))
    _chk496("C2: _should_compact_state → True when sidecar > 7 days old (weekly cadence)",
            r is True, f"got {r}")

    # C3: Sidecar 6 days old → False.
    recent_ts = int(time.time()) - (6 * 86400)
    sc_c.write_text(str(recent_ts))
    r = sync._should_compact_state(_eu496_cfg(root_c))
    _chk496("C3: _should_compact_state → False when sidecar < 7 days old",
            r is False, f"got {r}")

finally:
    sync._git = _real_git_496
    sync.ensure_state_clone = _real_ensure_496


# ──────────────────────────────────────────────────────────────────────────────
# Test group D: Exception from compact_state_branch is swallowed by git_sync
# ──────────────────────────────────────────────────────────────────────────────

root_d = _eu496_sandbox()

try:
    _reset_env_496()

    def _rec_d(cwd, *a, **kw):
        if a == ("rev-list", "--count", "unit-state"):
            return _cp(0, "10\n")  # low count
        return _cp(0)

    sync._git = _rec_d
    sync.ensure_state_clone = lambda _c: _fake_sd(root_d)
    sc_d = root_d / "last_state_compact.txt"
    if sc_d.exists():
        sc_d.unlink()

    # First confirm _should → False under normal conditions.
    r = sync._should_compact_state(_eu496_cfg(root_d))
    _chk496("D1: _should_compact_state → False under normal conditions",
            r is False, f"got {r}")

    # Now trigger NATURALLY (over-threshold count, no cache) and inject an exception
    # into compact_state_branch.
    def _rec_d_hot(cwd, *a, **kw):
        if a == ("rev-list", "--count", "unit-state"):
            return _cp(0, "999\n")  # over threshold → gate fires
        return _cp(0)

    _cr = [False]  # mutable container for nested scope

    def _compact_raise(*args, **kw):
        _cr[0] = True
        raise RuntimeError("boom — compact fails hard")

    try:
        sync.compact_state_branch = _compact_raise
        sync._git = _rec_d_hot
        sync.ensure_state_clone = lambda _c: _fake_sd(root_d)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result_sync = sync.git_sync(_eu496_cfg(root_d))
        logged = buf.getvalue()

        _chk496("D2: git_sync returns a valid dict (not crashed) when compact raises",
                isinstance(result_sync, dict), f"got {type(result_sync).__name__}")
        _chk496("D3: compact_state_branch raised RuntimeError (swallow verified by design)",
                _cr[0] is True, f"raised={_cr[0]}")
        # The error must be logged WITH its detail (type + message), not propagated.
        _chk496("D4: swallow log carries the exception detail (type + message)",
                "RuntimeError" in logged and "boom" in logged, f"log={logged!r}")
        _chk496("D5: sync completed normally — transport result intact, no error set",
                result_sync.get("pulled") is True and result_sync.get("error") is None,
                f"out={result_sync}")
    finally:
        sync.compact_state_branch = _real_compact_496
finally:
    sync._git = _real_git_496
    sync.ensure_state_clone = _real_ensure_496


# ──────────────────────────────────────────────────────────────────────────────
# Test group E: pull_only blocks compaction entirely
# ──────────────────────────────────────────────────────────────────────────────

root_e = _eu496_sandbox()

try:
    _reset_env_496()
    os.environ["GENERAL_SYNC_PULL_ONLY"] = "1"
    sc_e = root_e / "last_state_compact.txt"
    sc_e.write_text(str(int(time.time()) - 100 * 86400))  # very old

    def _rec_e(cwd, *a, **kw):
        if a == ("rev-list", "--count", "unit-state"):
            return _cp(0, "10\n")
        return _cp(0)

    sync._git = _rec_e
    sync.ensure_state_clone = lambda _c: _fake_sd(root_e)

    r = sync._should_compact_state(_eu496_cfg(root_e))
    _chk496("E1: _should_compact_state → False when pull_only() is True (VPS read-only)",
            r is False, f"got {r}")

    # Verify git_sync under pull_only — pulled=True, pushed=None, no compact attempt.
    compact_seen_e = []

    def _compact_track_e(*args, **kw):
        compact_seen_e.append(1)
        return {"ok": True, "step": None, "error": None}

    try:
        sync.compact_state_branch = _compact_track_e
        sync._git = _rec_e
        sync.ensure_state_clone = lambda _c: _fake_sd(root_e)

        result_sync = sync.git_sync(_eu496_cfg(root_e))
        _chk496("E2: git_sync pulled=True, pushed=None under pull_only",
                result_sync.get("pulled") is True
                and result_sync.get("pushed") is None,
                f"out={result_sync}")
        _chk496("E3: compact_state_branch NOT called under pull_only",
                len(compact_seen_e) == 0, f"called {len(compact_seen_e)} times")
    finally:
        sync.compact_state_branch = _real_compact_496
finally:
    del os.environ["GENERAL_SYNC_PULL_ONLY"]
    sync._git = _real_git_496
    sync.ensure_state_clone = _real_ensure_496


# ──────────────────────────────────────────────────────────────────────────────
# Test group F: REAL-HISTORY leg (a) — temp bare origin with >threshold commits,
# driven through the genuine _git / ensure_state_clone / compact_state_branch.
# The origin is a file:// URL so the depth-50 bootstrap clone is actually shallow
# (git ignores --depth for plain-path local clones) — proving the gate unshallows
# before counting, which is the only way the default threshold 150 is reachable.
# ──────────────────────────────────────────────────────────────────────────────

root_f = Path(tempfile.mkdtemp(prefix="eu496-realhist-"))

try:
    _reset_env_496()
    # Real functions — nothing stubbed in this group.
    sync._git = _real_git_496
    sync.ensure_state_clone = _real_ensure_496
    sync.compact_state_branch = _real_compact_496

    # Bare origin carrying a unit-state branch with 155 commits (> default threshold 150).
    bare_f = root_f / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "unit-state", str(bare_f)], capture_output=True)
    fseed = root_f / "fseed"
    subprocess.run(["git", "init", "-b", "unit-state", str(fseed)], capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=fseed)
    subprocess.run(["git", "config", "user.name", "t"], cwd=fseed)
    (fseed / "shared").mkdir()
    (fseed / "shared" / "test.jsonl").write_text('{"event":"seed","ts":"2026-07-01T00:00:00"}\n')
    subprocess.run(["git", "add", "shared"], cwd=fseed)
    subprocess.run(["git", "commit", "-qm", "v1"], cwd=fseed)
    loop_f = subprocess.run(
        ["bash", "-c", "for i in $(seq 2 155); do git commit --allow-empty -qm c$i || exit 1; done"],
        cwd=fseed)
    subprocess.run(["git", "remote", "add", "origin", f"file://{bare_f}"], cwd=fseed)
    push_f = subprocess.run(["git", "push", "origin", "unit-state"], cwd=fseed, capture_output=True)
    origin_cnt = subprocess.run(["git", "rev-list", "--count", "unit-state"], cwd=bare_f,
                                capture_output=True, text=True)
    _chk496("F1: seed sanity — origin unit-state has exactly 155 commits (> threshold 150)",
            loop_f.returncode == 0 and push_f.returncode == 0
            and (origin_cnt.stdout or "").strip() == "155",
            f"loop_rc={loop_f.returncode} push_rc={push_f.returncode} "
            f"count={(origin_cnt.stdout or '').strip()!r}")

    # Repo root: just enough for _origin_url() — a git dir whose origin points at the bare repo
    # via file:// so the bootstrap clone honours --depth (a plain path silently ignores it).
    froot = root_f / "froot"
    subprocess.run(["git", "init", "-b", "main", str(froot)], capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", f"file://{bare_f}"], cwd=froot,
                   capture_output=True)
    cfg_f = _eu496_cfg(froot)

    sd_f = sync.state_dir(cfg_f).resolve()
    assert str(sd_f).startswith(str(root_f.resolve())), (
        f"REFUSING TO SYNC: state_dir resolves OUTSIDE the tmp sandbox ({sd_f})")

    sd_f = sync.ensure_state_clone(cfg_f)
    shallow_pre = subprocess.run(["git", "rev-parse", "--is-shallow-repository"], cwd=sd_f,
                                 capture_output=True, text=True)
    window_cnt = subprocess.run(["git", "rev-list", "--count", "unit-state"], cwd=sd_f,
                                capture_output=True, text=True)
    _chk496("F2: bootstrap clone is shallow — raw count capped at the depth-50 window",
            shallow_pre.stdout.strip() == "true" and window_cnt.stdout.strip() == "50",
            f"shallow={shallow_pre.stdout.strip()!r} count={window_cnt.stdout.strip()!r}")

    r_f = sync._should_compact_state(cfg_f)
    shallow_post = subprocess.run(["git", "rev-parse", "--is-shallow-repository"], cwd=sd_f,
                                  capture_output=True, text=True)
    _chk496("F3: _should_compact_state → True off the REAL count (155 > 150) via the real _git",
            r_f is True, f"got {r_f}")
    _chk496("F4: the gate unshallowed the clone — shallow graft gone, history completed",
            shallow_post.stdout.strip() == "false", f"shallow={shallow_post.stdout.strip()!r}")

    # AC1 end-to-end: after a successful compaction the gate must NOT re-fire on the next sync.
    sync.compact_state_branch_now(cfg_f)
    origin_post = subprocess.run(["git", "rev-list", "--count", "unit-state"], cwd=bare_f,
                                 capture_output=True, text=True)
    _chk496("F5: compaction really ran — origin unit-state collapsed to exactly 1 commit",
            origin_post.returncode == 0 and (origin_post.stdout or "").strip() == "1",
            f"count={(origin_post.stdout or '').strip()!r} rc={origin_post.returncode}")
    r_f2 = sync._should_compact_state(cfg_f)
    _chk496("F6: AC1 — gate → False immediately after compaction (no every-sync re-fire)",
            r_f2 is False, f"got {r_f2}")

    # Real-history SKIP leg: an origin with a handful of commits must NOT fire the gate.
    bare_g = root_f / "origin-small.git"
    subprocess.run(["git", "init", "--bare", "-b", "unit-state", str(bare_g)], capture_output=True)
    sseed = root_f / "sseed"
    subprocess.run(["git", "init", "-b", "unit-state", str(sseed)], capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=sseed)
    subprocess.run(["git", "config", "user.name", "t"], cwd=sseed)
    (sseed / "shared").mkdir()
    (sseed / "shared" / "test.jsonl").write_text('{"event":"seed"}\n')
    subprocess.run(["git", "add", "shared"], cwd=sseed)
    subprocess.run(["git", "commit", "-qm", "v1"], cwd=sseed)
    subprocess.run(["bash", "-c", "for i in $(seq 2 10); do git commit --allow-empty -qm c$i; done"],
                   cwd=sseed)
    subprocess.run(["git", "remote", "add", "origin", f"file://{bare_g}"], cwd=sseed)
    subprocess.run(["git", "push", "origin", "unit-state"], cwd=sseed, capture_output=True)
    sroot = root_f / "sroot"
    subprocess.run(["git", "init", "-b", "main", str(sroot)], capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", f"file://{bare_g}"], cwd=sroot,
                   capture_output=True)
    r_s = sync._should_compact_state(_eu496_cfg(sroot))
    _chk496("F7: real history under threshold (10 commits, no sidecar) → False",
            r_s is False, f"got {r_s}")
finally:
    sync._git = _real_git_496
    sync.ensure_state_clone = _real_ensure_496
    sync.compact_state_branch = _real_compact_496
    _reset_env_496()


# ──────────────────────────────────────────────────────────────────────────────
# Test group G: the throttle re-evaluates on every call — no process-lifetime
# cache to freeze the verdict when git_sync runs repeatedly in one process.
# ──────────────────────────────────────────────────────────────────────────────

root_g = _eu496_sandbox()

try:
    _reset_env_496()

    def _rec_g(cwd, *a, **kw):
        if a == ("rev-list", "--count", "unit-state"):
            return _cp(0, "10\n")
        return _cp(0)

    sync._git = _rec_g
    sync.ensure_state_clone = lambda _c: _fake_sd(root_g)
    sc_g = root_g / "last_state_compact.txt"

    # Same cfg, same process: fresh sidecar → False, then aged sidecar → True. A cached
    # verdict would return the stale False on the second call.
    sc_g.write_text(str(int(time.time())))  # fresh
    r1 = sync._should_compact_state(_eu496_cfg(root_g))
    _chk496("G1: fresh sidecar + low count → False", r1 is False, f"got {r1}")

    sc_g.write_text(str(int(time.time()) - 8 * 86400))  # age past the cadence window
    r2 = sync._should_compact_state(_eu496_cfg(root_g))
    _chk496("G2: SAME process, aged sidecar → True — verdict re-evaluated, not frozen",
            r2 is True, f"got {r2}")

    _chk496("G3: no process-lifetime _COMPACT_CACHE left in orchestrator.sync",
            not hasattr(sync, "_COMPACT_CACHE"), "module still defines _COMPACT_CACHE")
finally:
    sync._git = _real_git_496
    sync.ensure_state_clone = _real_ensure_496


# ── report (EU-496) ──────────────────────────────────────────────────────────
print("\n============ EU-496 _SHOULD_COMPACT_STATE THROTTLE + BEST-EFFORT WRAP ========")
for name, ok, detail in eu496_results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("----------------------------------------------------------------")
eu496_passed = sum(1 for _, ok, _ in eu496_results if ok)
print(f"  {eu496_passed}/{len(eu496_results)} passed")
print("  RESULT:", "ALL GREEN" if eu496_passed == len(eu496_results)
      else f"{len(eu496_results) - eu496_passed} FAIL")

# Final summary
overall_total = len(results) + len(eu502_results) + len(eu496_results)
overall_passed = eu501_passed + eu502_passed + eu496_passed
print(f"\n{'='*76}")
print(f"  OVERALL: {overall_passed}/{overall_total} (EU-501: {eu501_passed}/{eu501_total} | "
      f"EU-502: {eu502_passed}/{len(eu502_results)} | EU-496: {eu496_passed}/{len(eu496_results)})")
print(f"{'='*76}")
sys.exit(0 if overall_passed == overall_total else 1)
