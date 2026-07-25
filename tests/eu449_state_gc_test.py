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
