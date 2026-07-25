"""EU-526 — _gc_is_due / _mark_gc_done / _gc_sentinel plumbing tests.

Real tmp sentinel files (mtime via os.utime). Zero git subprocesses.
Follows the harness convention of tests/eu449_state_compact_test.py.

Checks:
  AC1 — fresh clone, no sentinel → True
  AC2 — after _mark_gc_done(), sentinel fresh → False
  AC3 — force=True always returns True
  AC4 — mtime boundary: old→True, fresh→False
  AC5 — constants exist + no subprocess spawned (monkeypatch _git)
"""
import os
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path

# ── SDK stub (tests/eu449_state_compact_test.py convention) ───────────────
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


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

_INTERVAL_S = sync._GC_INTERVAL_HOURS * 3600


def _make_cfg(root):
    """Build a Config whose audit_path lives under *root*."""
    return Config(
        apps=[AppConfig(name="automatixy", repo_path=str(root), base_branch="DEV",
                        protected_branch="MAIN", backlog_backend="none")],
        audit_path=str(root / "audit.jsonl"),
        use_worktree=False,
    )


def _ensure_clean(cfg):
    """Remove .unit-state/ so the next _gc_* call starts from zero."""
    sd = sync.state_dir(cfg)
    if sd.exists():
        shutil.rmtree(sd, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# AC5 (constants) — verified before dependent helpers
# ═══════════════════════════════════════════════════════════════════════════

chk("AC5: _GC_INTERVAL_HOURS == 24",
    sync._GC_INTERVAL_HOURS == 24, f"got {sync._GC_INTERVAL_HOURS}")
chk("AC5: _GC_SENTINEL_NAME == '.last_gc'",
    sync._GC_SENTINEL_NAME == ".last_gc", f"got {sync._GC_SENTINEL_NAME!r}")


# ═══════════════════════════════════════════════════════════════════════════
# AC1 — fresh clone with no .unit-state/.last_gc → True
# ═══════════════════════════════════════════════════════════════════════════

root_a = Path(tempfile.mkdtemp(prefix="eu526-a-"))
cfg_a = _make_cfg(root_a)

# state_dir(cfg_a) should not exist (no .unit-state created yet)
sd_a = sync.state_dir(cfg_a)
_chk_a = sd_a.exists()
chk("AC1 precondition: no .unit-state dir present",
    not _chk_a, f".unit-state unexpectedly exists at {sd_a}")

chk("AC1: _gc_is_due(cfg) → True when sentinel is absent",
    sync._gc_is_due(cfg_a) is True, f"got {sync._gc_is_due(cfg_a)}")


# ═══════════════════════════════════════════════════════════════════════════
# AC2 — after _mark_gc_done() → sentinel exists, within window → False
# ═══════════════════════════════════════════════════════════════════════════

root_b = Path(tempfile.mkdtemp(prefix="eu526-b-"))
cfg_b = _make_cfg(root_b)
_ensure_clean(cfg_b)

sync._mark_gc_done(cfg_b)
sentinel_b = sync._gc_sentinel(cfg_b)
chk("AC2: _mark_gc_done creates the sentinel file on disk",
    sentinel_b.is_file(), f"path={sentinel_b} exists={sentinel_b.exists()}")

chk("AC2: _gc_is_due(cfg) → False immediately after _mark_gc_done()",
    sync._gc_is_due(cfg_b) is False, f"got {sync._gc_is_due(cfg_b)}")


# ═══════════════════════════════════════════════════════════════════════════
# AC3 — force=True overrides regardless of sentinel state
# ═══════════════════════════════════════════════════════════════════════════

# With fresh sentinel present (right after _mark_gc_done)
chk("AC3a: _gc_is_due(cfg, force=True) → True with fresh sentinel",
    sync._gc_is_due(cfg_b, force=True) is True,
    f"got {sync._gc_is_due(cfg_b, force=True)}")

# With NO sentinel at all
root_c = Path(tempfile.mkdtemp(prefix="eu526-c-"))
cfg_c = _make_cfg(root_c)
chk("AC3b: _gc_is_due(cfg, force=True) → True with no sentinel",
    sync._gc_is_due(cfg_c, force=True) is True,
    f"got {sync._gc_is_due(cfg_c, force=True)}")


# ═══════════════════════════════════════════════════════════════════════════
# AC4 — mtime boundary: old sentinel → True; recent (but < interval) → False
# ═══════════════════════════════════════════════════════════════════════════

root_d = Path(tempfile.mkdtemp(prefix="eu526-d-"))
cfg_d = _make_cfg(root_d)
_ensure_clean(cfg_d)
sd_d = sync.state_dir(cfg_d)
sd_d.mkdir(parents=True, exist_ok=True)
sentinel_d = sd_d / sync._GC_SENTINEL_NAME

# AC4a: backdate sentinel past the interval threshold
sentinel_d.touch()
stale_time = time.time() - (_INTERVAL_S + 60)
os.utime(sentinel_d, (stale_time, stale_time))
chk("AC4a: _gc_is_due(cfg) → True when mtime > interval old",
    sync._gc_is_due(cfg_d) is True,
    f"got {sync._gc_is_due(cfg_d)}, aged by {_INTERVAL_S + 60:.0f}s vs {_INTERVAL_S:.0f}s threshold")

# AC4b: set mtime to interval - 1h (still within the throttle window)
fresh_time = time.time() - (_INTERVAL_S - 3600)
os.utime(sentinel_d, (fresh_time, fresh_time))
chk("AC4b: _gc_is_due(cfg) → False when mtime < interval old",
    sync._gc_is_due(cfg_d) is False,
    f"got {sync._gc_is_due(cfg_d)}, aged by {_INTERVAL_S - 3600:.0f}s vs {_INTERVAL_S:.0f}s threshold")


# ═══════════════════════════════════════════════════════════════════════════
# AC5 (subprocess guard) — _git must never be called by these helpers
# ═══════════════════════════════════════════════════════════════════════════

root_e = Path(tempfile.mkdtemp(prefix="eu526-e-"))
cfg_e = _make_cfg(root_e)
_ensure_clean(cfg_e)

real_git = sync._git
_git_calls = []


def _recorder_fail(cwd, *a, **kw):
    """Recorder that records the call AND makes the test fail if invoked."""
    _git_calls.append(a)
    raise AssertionError(f"_gc_is_due / _mark_gc_done must not spawn subprocesses — _git called with: {a}")


try:
    sync._git = _recorder_fail

    # All three code paths that _gc_is_due can take:
    r_absent = sync._gc_is_due(cfg_e)                    # OSError path → True
    sync._mark_gc_done(cfg_e)                             # creates sentinel via pathlib
    r_present = sync._gc_is_due(cfg_e)                   # stat + compare → False
    r_force = sync._gc_is_due(cfg_e, force=True)         # short-circuit → True

    chk("AC5c: _gc_is_due / _mark_gc_done never call _git (no subprocess)",
        len(_git_calls) == 0, f"_git was called {len(_git_calls)} times: {_git_calls}")

    # Sanity: values are still correct despite monkeypatch
    chk("AC5d: values still correct with _git monkeypatched",
        r_absent is True and r_present is False and r_force is True,
        f"got absent={r_absent} present={r_present} force={r_force}")

finally:
    sync._git = real_git


# ═══════════════════════════════════════════════════════════════════════════
# EU-527 — gc_state_clone integration (stubbed _git, sentinel-driven flow)
# ═══════════════════════════════════════════════════════════════════════════

class _CP:
    """Minimal fake ``CompletedProcess[str]`` for stubbing _git."""

    def __init__(self, rc=0, stdout="", stderr="") -> None:
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def _success_stub(cwd, *a, **kw) -> _CP:
    return _CP(rc=0, stdout="")


def _fail_stub(cwd, *a, **kw) -> _CP:
    return _CP(rc=1, stdout="", stderr="fatal: gc failed")


def _raise_stub(cwd, *a, **kw) -> None:
    # Deliberately NOT an OSError / subprocess.SubprocessError: a
    # UnicodeDecodeError is exactly what _git's text=True decoding can raise
    # on non-UTF8 git output. AC6 uses this stub to prove the broadened
    # `except Exception` catch in gc_state_clone really swallows it.
    raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")


# ── AC1: gc fires when due (no sentinel), calls _mark_gc_done after success ──

root_1 = Path(tempfile.mkdtemp(prefix="eu527-a1-"))
cfg_1 = _make_cfg(root_1)
sd_1 = sync.state_dir(cfg_1)
sd_1.mkdir(parents=True, exist_ok=True)
(sd_1 / ".git").mkdir(exist_ok=True)          # simulate existing clone
real_git = sync._git
_g1_calls = []


def _recorder_1(cwd, *a, **kw) -> _CP:
    _g1_calls.append((cwd, a, kw))
    return _success_stub(cwd, *a, **kw)


try:
    sync._git = _recorder_1
    r = sync.gc_state_clone(cfg_1)

    chk("EU-527 AC1a: returns ok=True, ran=True on successful gc",
        r.get("ok") is True and r.get("ran") is True,
        f"got {r}")

    chk("EU-527 AC1b: _git called exactly once with correct args",
        len(_g1_calls) == 1,
        f"called {len(_g1_calls)} times: {_g1_calls}")

    chk("EU-527 AC1c: _git called with state_dir cwd and ('gc', '--prune=now')",
        _g1_calls[0][0] == sd_1 and _g1_calls[0][1] == ("gc", "--prune=now"),
        f"cwd={_g1_calls[0][0]} args={_g1_calls[0][1]}")

    chk("EU-527 AC1d: _mark_gc_done was called (sentinel exists)",
        sync._gc_sentinel(cfg_1).is_file(),
        f"sentinel missing at {sync._gc_sentinel(cfg_1)}")

finally:
    sync._git = real_git


# ── AC2: skipped when not due (sentinel fresh) ──

root_2 = Path(tempfile.mkdtemp(prefix="eu527-a2-"))
cfg_2 = _make_cfg(root_2)
sd_2 = sync.state_dir(cfg_2)
sd_2.mkdir(parents=True, exist_ok=True)
(sd_2 / ".git").mkdir(exist_ok=True)
sync._mark_gc_done(cfg_2)                    # make it "not due"
real_git = sync._git
_g2_calls = []


def _recorder_2(cwd, *a, **kw) -> _CP:
    _g2_calls.append((cwd, a, kw))
    return _success_stub(cwd, *a, **kw)


try:
    sync._git = _recorder_2
    r = sync.gc_state_clone(cfg_2)

    chk("EU-527 AC2a: returns ok=True, ran=False when not due",
        r.get("ok") is True and r.get("ran") is False,
        f"got {r}")

    chk("EU-527 AC2b: does NOT call _git when not due",
        len(_g2_calls) == 0,
        f"_git called {len(_g2_calls)} times")

finally:
    sync._git = real_git


# ── AC3: force=True bypasses throttle and fires gc ──

root_3 = Path(tempfile.mkdtemp(prefix="eu527-a3-"))
cfg_3 = _make_cfg(root_3)
sd_3 = sync.state_dir(cfg_3)
sd_3.mkdir(parents=True, exist_ok=True)
(sd_3 / ".git").mkdir(exist_ok=True)
sync._mark_gc_done(cfg_3)                    # fresh sentinel → normally not due
real_git = sync._git
_g3_calls = []


def _recorder_3(cwd, *a, **kw) -> _CP:
    _g3_calls.append((cwd, a, kw))
    return _success_stub(cwd, *a, **kw)


try:
    sync._git = _recorder_3
    r = sync.gc_state_clone(cfg_3, force=True)

    chk("EU-527 AC3a: force=True fires gc even when sentinel is fresh",
        r.get("ok") is True and r.get("ran") is True,
        f"got {r}")

    chk("EU-527 AC3b: _git called once with force=True despite fresh sentinel",
        len(_g3_calls) == 1,
        f"called {len(_g3_calls)} times")

finally:
    sync._git = real_git


# ── AC4: _git raising → caught, no sentinel refresh ──

root_4 = Path(tempfile.mkdtemp(prefix="eu527-a4-"))
cfg_4 = _make_cfg(root_4)
sd_4 = sync.state_dir(cfg_4)
sd_4.mkdir(parents=True, exist_ok=True)
(sd_4 / ".git").mkdir(exist_ok=True)
real_git = sync._git


def _raising_stubs_4(cwd, *a, **kw) -> None:
    raise OSError("disk full")


try:
    sync._git = _raising_stubs_4
    r = sync.gc_state_clone(cfg_4)

    chk("EU-527 AC4a: returns ok=False with error string when _git raises",
        r.get("ok") is False and "disk full" in str(r.get("error", "")),
        f"got {r}")

    chk("EU-527 AC4b: _mark_gc_done NOT called on failure (sentinel absent or old)",
        sync._gc_sentinel(cfg_4).exists() is False or
        time.time() - sync._gc_sentinel(cfg_4).stat().st_mtime < 1,
        "sentinel unexpectedly created/touched after failure")

finally:
    sync._git = real_git


# ── AC5: _git returning nonzero rc → error dict, no sentinel refresh ──

root_5 = Path(tempfile.mkdtemp(prefix="eu527-a5-"))
cfg_5 = _make_cfg(root_5)
sd_5 = sync.state_dir(cfg_5)
sd_5.mkdir(parents=True, exist_ok=True)
(sd_5 / ".git").mkdir(exist_ok=True)
real_git = sync._git
_g5_calls = []


def _fail_recorder(cwd, *a, **kw) -> _CP:
    _g5_calls.append((cwd, a, kw))
    return _fail_stub(cwd, *a, **kw)


try:
    sync._git = _fail_recorder
    r = sync.gc_state_clone(cfg_5)

    chk("EU-527 AC5a: returns ok=False when _git returns nonzero rc",
        r.get("ok") is False,
        f"got {r}")

    chk("EU-527 AC5b: error includes stderr text",
        "gc failed" in str(r.get("error", "")),
        f"error={r.get('error')}")

    chk("EU-527 AC5c: sentinel NOT refreshed after nonzero-return failure",
        sync._gc_sentinel(cfg_5).exists() is False or
        time.time() - sync._gc_sentinel(cfg_5).stat().st_mtime < 1,
        "sentinel unexpectedly touched after failed gc")

finally:
    sync._git = real_git


# ── AC6: _git raising a NON-OSError/SubprocessError → broad catch swallows it ──

root_6 = Path(tempfile.mkdtemp(prefix="eu527-a6-"))
cfg_6 = _make_cfg(root_6)
sd_6 = sync.state_dir(cfg_6)
sd_6.mkdir(parents=True, exist_ok=True)
(sd_6 / ".git").mkdir(exist_ok=True)
real_git = sync._git

try:
    sync._git = _raise_stub              # raises UnicodeDecodeError (a ValueError)
    try:
        r = sync.gc_state_clone(cfg_6)
        _propagated = None
    except Exception as exc:             # noqa: BLE001 — test must detect ANY propagation
        r = {}
        _propagated = exc

    chk("EU-527 AC6a: UnicodeDecodeError from _git is swallowed, never propagates",
        _propagated is None,
        f"gc_state_clone raised {type(_propagated).__name__}: {_propagated}")

    chk("EU-527 AC6b: returns ok=False with the decode error reported",
        r.get("ok") is False and "invalid start byte" in str(r.get("error", "")),
        f"got {r}")

    chk("EU-527 AC6c: _mark_gc_done NOT called after raised failure (no sentinel)",
        sync._gc_sentinel(cfg_6).exists() is False,
        f"sentinel unexpectedly present at {sync._gc_sentinel(cfg_6)}")

finally:
    sync._git = real_git


# ── Also verify the skip-due-no-clone path ──

root_nc = Path(tempfile.mkdtemp(prefix="eu527-noclone-"))
cfg_nc = _make_cfg(root_nc)
sd_nc = sync.state_dir(cfg_nc)
sd_nc.mkdir(parents=True, exist_ok=True)   # dir exists but NO .git

_r_nc = sync.gc_state_clone(cfg_nc)
chk("EU-527: skips with 'no-clone' when .git is missing",
    _r_nc.get("ok") is True and _r_nc.get("ran") is False and _r_nc.get("reason") == "no-clone",
    f"got {_r_nc}")


# ═══════════════════════════════════════════════════════════════════════════
# Report
# ═══════════════════════════════════════════════════════════════════════════

total = len(results)
passed = sum(1 for _, ok, _ in results if ok)

print(f"\n{'='*72}")
print(f"  EU-526 — GC THROTTLE PLUMBING  ({passed}/{total} checks passed)")
print(f"{'='*72}")
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("-" * 72)
print(f"  RESULT:", "ALL GREEN" if passed == total else f"{total - passed} FAIL")
print(f"{'='*72}\n")

sys.exit(0 if passed == total else 1)
