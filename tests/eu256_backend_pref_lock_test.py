"""EU-275: set_active must read-modify-write atomically under locking.locked_rmw.

Before the fix, set_active did an UNLOCKED ``_load()`` -> mutate -> ``write_text()``. A concurrent
global write and per-app write racing against the same file could clobber one another (last writer
wins, dropping the other side's update), and a reader mid-truncate could observe ``{}`` — silently
falling back to the config.yaml default backend (EU-223 risk: a run lands on the wrong paid/free
backend with no error surfaced).

These checks pin:
  1. set_active persists via locking.locked_rmw (a spy proves it's called; the function source no
     longer calls write_text directly).
  2. Global / per-app / _INHERIT writes still produce the identical `data` shape as before.
  3. Two "threads" (real threading.Thread) racing a global set_active and a per-app set_active
     against the same file: BOTH updates survive — neither clobbers the other.
  4. set_active is best-effort: locked_rmw raising OSError or ValueError is swallowed (returns None,
     never raises).
"""
import sys, types, json, tempfile, threading, re
from pathlib import Path

# ── Stub the Agent SDK before any orchestrator import (house convention) ──
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import backend_pref, backends, locking
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

def _tmp_cfg(d: Path) -> Config:
    return Config(apps=[AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV",
                                  protected_branch="MAIN", backlog_backend="none")],
                  audit_path=str(d / "state" / "audit.jsonl"), use_worktree=False)


# ============ 1) set_active goes through locking.locked_rmw, not a bare write_text ============ #
_src = Path("orchestrator/backend_pref.py").read_text(encoding="utf-8")
_m = re.search(r"def set_active\(.*?\n(?=\ndef |\Z)", _src, re.S)
_set_active_src = _m.group(0) if _m else ""
chk("set_active source calls locking.locked_rmw", "locked_rmw" in _set_active_src, _set_active_src)
chk("set_active source no longer calls write_text directly",
    "write_text" not in _set_active_src, _set_active_src)

_calls = []
_orig_locked_rmw = locking.locked_rmw
def _spy_locked_rmw(path, mutate_fn, **kw):
    _calls.append(path)
    return _orig_locked_rmw(path, mutate_fn, **kw)
d0 = Path(tempfile.mkdtemp())
cfg0 = _tmp_cfg(d0)
backend_pref.locking.locked_rmw = _spy_locked_rmw
try:
    backend_pref.set_active("glm", cfg0)
    chk("set_active(cfg) actually invokes locking.locked_rmw at runtime", len(_calls) == 1, _calls)
finally:
    backend_pref.locking.locked_rmw = _orig_locked_rmw

# ============ 2) identical data shape for global / per-app / _INHERIT writes ============ #
d1 = Path(tempfile.mkdtemp())
cfg1 = _tmp_cfg(d1)
backend_pref.set_active("glm", cfg1)
chk("global write sets only 'backend'", backend_pref._load(cfg1) == {"backend": "glm"},
    backend_pref._load(cfg1))

backend_pref.set_active("opus", cfg1, app_name="automatixy")
chk("per-app write sets apps[app] and preserves global",
    backend_pref._load(cfg1) == {"backend": "glm", "apps": {"automatixy": "opus"}},
    backend_pref._load(cfg1))

backend_pref.set_active(None, cfg1, app_name="automatixy")
chk("_INHERIT pop removes the app override, preserves global",
    backend_pref._load(cfg1) == {"backend": "glm", "apps": {}},
    backend_pref._load(cfg1))

chk("get(cfg) reflects the global pref", backend_pref.get(cfg1) == backends.GLM)
chk("get_apps(cfg) is empty after inherit-pop", backend_pref.get_apps(cfg1) == {})


# ============ 3) interleaved global + per-app writes from two threads: union survives ============ #
d2 = Path(tempfile.mkdtemp())
cfg2 = _tmp_cfg(d2)

barrier = threading.Barrier(2)

def _write_global():
    barrier.wait()
    backend_pref.set_active("glm", cfg2)

def _write_per_app():
    barrier.wait()
    backend_pref.set_active("opus", cfg2, app_name="automatixy")

t1 = threading.Thread(target=_write_global)
t2 = threading.Thread(target=_write_per_app)
t1.start(); t2.start()
t1.join(); t2.join()

chk("interleaved: global write survived", backend_pref.get(cfg2) == backends.GLM,
    backend_pref._load(cfg2))
chk("interleaved: per-app write survived (union, neither clobbered)",
    backend_pref.get_apps(cfg2).get("automatixy") == backends.NATIVE,
    backend_pref._load(cfg2))


# ============ 4) best-effort: locked_rmw raising OSError/ValueError is swallowed ============ #
d3 = Path(tempfile.mkdtemp())
cfg3 = _tmp_cfg(d3)

def _boom_oserror(path, mutate_fn, **kw):
    raise OSError("disk full")
def _boom_valueerror(path, mutate_fn, **kw):
    raise ValueError("bad json")

backend_pref.locking.locked_rmw = _boom_oserror
try:
    ret = backend_pref.set_active("glm", cfg3)
    chk("set_active swallows OSError from locked_rmw (best-effort)", ret is None)
except Exception as e:
    chk("set_active swallows OSError from locked_rmw (best-effort)", False, repr(e))
finally:
    backend_pref.locking.locked_rmw = _orig_locked_rmw

backend_pref.locking.locked_rmw = _boom_valueerror
try:
    ret = backend_pref.set_active("glm", cfg3)
    chk("set_active swallows ValueError from locked_rmw (best-effort)", ret is None)
except Exception as e:
    chk("set_active swallows ValueError from locked_rmw (best-effort)", False, repr(e))
finally:
    backend_pref.locking.locked_rmw = _orig_locked_rmw


print("\n========== EU-275 BACKEND-PREF LOCKED-WRITE QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
