"""EU-454 — a post-merge verify RED evicts the stale green base-gate cache entry.

publish_base_green (EU-376) writes a green cache entry for merge_sha at _land time, BEFORE the
post-merge dev-HEAD verify (EU-453) runs. A merge that combines cleanly but breaks dev's real
state therefore already carries a green entry, and the NEXT ticket's base_gate_check HITS it
and skips re-running — red dev stays SILENT (the EU-447 hazard). evict_base_green is the
inverse: called only on a post-merge verify RED, it drops the f"{app.repo_path}@{sha}" key from
red_base_cache.json so the next pick's base gate MISSES and re-verifies against the real (red)
dev. Green verify leaves the entry as-is (preserving EU-376's ~178s-per-ticket dedup win).

Pins:
  (1) publish green -> base_gate_check HITS (no suite run); evict -> key gone, the SAME
      base_gate_check now MISSES and runs the suite exactly once;
  (2) a no-op on a key that was never written -- other entries stay intact, target stays absent,
      never raises;
  (3) never CREATES the cache file -- on a dir with no red_base_cache.json, evict returns without
      raising and the file is still absent afterward;
  (4) a cache-write failure during evict is swallowed -- prints a run-log line, never raises;
  (5) wiring is RED-only -- evict_base_green is imported in loop.py and called inside
      _postmerge_verify_flag's RED branch only (after postmerge_verify_fail; unreachable on the
      green postmerge_verify_pass branch, which returns None first) -- source-pinned like eu376 (5).
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, ".")

sdk = types.ModuleType("claude_agent_sdk")
sdk.__getattr__ = lambda n: (lambda *a, **k: None)
sys.modules.setdefault("claude_agent_sdk", sdk)

from orchestrator import gate  # noqa: E402
from orchestrator import locking  # noqa: E402

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


ok("gate exposes evict_base_green", hasattr(gate, "evict_base_green"))

_tmp = Path(tempfile.mkdtemp())
cfg = types.SimpleNamespace(audit_path=str(_tmp / "audit.jsonl"))
# workdir is part of AppConfig (Optional[str]) — the MISS path's _lockfile_candidates reads it
# (app.workdir or app.repo_path); eu376 never reached that path (it only HIT the cache).
app = types.SimpleNamespace(repo_path="/repo", workdir=None, lint_commands=[], name="EU")
cache = _tmp / "red_base_cache.json"


class _Git:
    def __init__(self, sha):
        self._sha = sha

    def current_sha(self):
        return self._sha


suite_runs = []


def _runner(a, paths):
    suite_runs.append(1)
    return types.SimpleNamespace(passed=True, report="")


# (1) publish green -> HIT; evict -> MISS that re-runs the suite exactly once.
gate.publish_base_green(app, cfg, "shaX")
suite_runs.clear()
gate.base_gate_check(app, cfg, _Git("shaX"), runner=_runner)
ok("(1a) a published green entry is a cache HIT -- the suite never runs",
   suite_runs == [], f"suite_runs={len(suite_runs)}")

gate.evict_base_green(app, cfg, "shaX")
data = json.loads(cache.read_text())
ok("(1b) evict_base_green drops the key from red_base_cache.json",
   "/repo@shaX" not in data, str(list(data.keys()))[:120])

suite_runs.clear()
gate.base_gate_check(app, cfg, _Git("shaX"), runner=_runner)
ok("(1c) after evict the same base_gate_check MISSES -- the suite runs exactly once",
   len(suite_runs) == 1, f"suite_runs={len(suite_runs)}")

# (2) a no-op on a key that was never written -- siblings survive, target stays absent.
gate.publish_base_green(app, cfg, "shaA")
gate.publish_base_green(app, cfg, "shaB")
gate.evict_base_green(app, cfg, "shaC")   # never published
data = json.loads(cache.read_text())
ok("(2) evict on an absent key is a no-op -- siblings intact, target absent, never raises",
   "/repo@shaA" in data and "/repo@shaB" in data and "/repo@shaC" not in data,
   str(list(data.keys()))[:120])

# (3) never CREATES the cache file.
_tmp3 = Path(tempfile.mkdtemp())
cfg3 = types.SimpleNamespace(audit_path=str(_tmp3 / "audit.jsonl"))
ok("(3a) the cache file does not exist before evict", not (_tmp3 / "red_base_cache.json").exists())
raised = False
try:
    gate.evict_base_green(app, cfg3, "anything")
except Exception as exc:  # noqa: BLE001
    raised = str(exc)
ok("(3b) evict on a missing cache never raises and never creates the file",
   raised is False and not (_tmp3 / "red_base_cache.json").exists(),
   f"raised={raised!r} exists={(_tmp3 / 'red_base_cache.json').exists()}")

# (4) a cache-write failure during evict is swallowed -- prints a run-log line, never raises.
# Pre-create the cache so the exists-guard passes and locked_rmw (patched to raise) actually runs.
_tmp4 = Path(tempfile.mkdtemp())
cfg4 = types.SimpleNamespace(audit_path=str(_tmp4 / "audit.jsonl"))
(_tmp4 / "red_base_cache.json").write_text('{"k": "v"}')
_orig_rmw = locking.locked_rmw


def _boom(*a, **k):
    raise RuntimeError("simulated disk-full on evict write")


locking.locked_rmw = _boom   # evict resolves `from . import locking; locking.locked_rmw(...)` at call time
buf = io.StringIO()
raised = False
try:
    with contextlib.redirect_stdout(buf):
        gate.evict_base_green(app, cfg4, "shaZ")
except Exception as exc:  # noqa: BLE001
    raised = str(exc)
finally:
    locking.locked_rmw = _orig_rmw
ok("(4) a cache-write failure during evict is swallowed (prints, never raises)",
   raised is False and "evict" in buf.getvalue().lower(),
   f"raised={raised!r} out={buf.getvalue()!r}")

# (5) wiring -- RED-only, inside _postmerge_verify_flag after the postmerge_verify_fail record.
src = Path("orchestrator/loop.py").read_text()
seg_start = src.find("def _postmerge_verify_flag")
seg = src[seg_start:seg_start + 4000] if seg_start != -1 else ""
# Anchor on the CODE calls (audit.record("...")) NOT the docstring's backtick mentions of the same
# event names, which sit above the code and would otherwise break the ordering pin (eu376 (5)/(5b)
# sidesteps this by using unique code tokens; the audit.record("..." prefix does the same here).
pass_at = seg.find('audit.record("postmerge_verify_pass"')   # green code record
green_ret = seg.find("return None", pass_at)                 # the green branch's return None
fail_at = seg.find('audit.record("postmerge_verify_fail"')   # red code record
evict_at = seg.find("evict_base_green(app, cfg, merge_sha)")
ok("(5) evict_base_green is imported in loop.py", "evict_base_green" in src)
ok("(5b) evict is wired RED-only -- in _postmerge_verify_flag after the postmerge_verify_fail "
    "record, past the green branch's return None",
   pass_at != -1 and green_ret != -1 and fail_at != -1 and evict_at != -1
   and pass_at < green_ret < fail_at < evict_at,
   "green records postmerge_verify_pass then returns None; evict sits in the red try block "
   "(expected pass_at < green_ret < fail_at < evict_at). A -1 in any token fails the chain, so all "
   "four are checked -- no vacuous pass.")

print(f"\n{checks}/{checks} passed")
