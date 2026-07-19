"""EU-376 — a green dev_gate verdict is published to the base-gate cache (no duplicate suite run).

Measured 2026-07-16: every land mints a new base sha, base_gate_check is the cache's only writer,
so the next ticket's base gate ALWAYS missed and re-ran the full 178s suite on the exact commit
object the previous ticket's dev_gate proved green ~3 minutes earlier (all 14 cache entries were
distinct shas = 14 misses; EU-352's dev_gate passed 4831566 at 00:30:54, EU-353's base gate
re-proved the same commit at 00:33:58).

Pins:
  (1) publish_base_green writes a green entry base_gate_check then HITS (no suite run);
  (2) it publishes GREEN only by shape — the entry is hardcoded passed=True (a published red
      would resurrect the EU-334/EU-228 false-red-halt class);
  (3) lint parity: an app with lint_commands armed is REFUSED (the base gate runs suite+lint,
      the dev_gate runs suite only — a weaker proof must not be published);
  (4) LRU cap preserved (never grows past _RED_BASE_CACHE_MAX);
  (5) wiring: _land publishes AFTER land_trial confirms the fast-forward, green-only, keyed on
      the pre-captured merge_sha (source pins, since driving the whole _land needs a live repo);
  (6) a cache-write failure is swallowed (publishing is best-effort, never blocks a land).
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, ".")

sdk = types.ModuleType("claude_agent_sdk")
sdk.__getattr__ = lambda n: (lambda *a, **k: None)
sys.modules.setdefault("claude_agent_sdk", sdk)

from orchestrator import gate  # noqa: E402

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


ok("gate exposes publish_base_green", hasattr(gate, "publish_base_green"))

_tmp = Path(tempfile.mkdtemp())
cfg = types.SimpleNamespace(audit_path=str(_tmp / "audit.jsonl"))
app = types.SimpleNamespace(repo_path="/repo", lint_commands=[], name="EU")


class _Git:
    def __init__(self, sha):
        self._sha = sha

    def current_sha(self):
        return self._sha


suite_runs = []


def _runner(a, paths):
    suite_runs.append(1)
    return types.SimpleNamespace(passed=True, report="")


# (1) publish → the next base_gate_check on that sha HITS the cache: zero suite runs
gate.publish_base_green(app, cfg, "abc123")
passed, fp, report = gate.base_gate_check(app, cfg, _Git("abc123"), runner=_runner)
ok("(1) published green is a cache HIT — the suite never runs",
   passed and suite_runs == [], f"suite_runs={len(suite_runs)}")

# (2) the entry is green by SHAPE — passed is hardcoded True in the writer
data = json.loads((_tmp / "red_base_cache.json").read_text())
ok("(2) the published entry is passed=True with no red payload",
   data["/repo@abc123"]["passed"] is True and data["/repo@abc123"]["fp"] == "",
   str(data)[:120])

# (3) lint parity — an app with lint_commands armed must be refused
app_lint = types.SimpleNamespace(repo_path="/repo", lint_commands=["ruff check ."], name="EU")
gate.publish_base_green(app_lint, cfg, "def456")
data = json.loads((_tmp / "red_base_cache.json").read_text())
ok("(3) a lint-armed app is refused (dev_gate proof is strictly weaker)",
   "/repo@def456" not in data, str(data.keys()))

# (3b) an empty sha is refused
gate.publish_base_green(app, cfg, "")
data = json.loads((_tmp / "red_base_cache.json").read_text())
ok("(3b) an empty sha is refused", all("@" in k and not k.endswith("@") for k in data))

# (4) the LRU cap holds
for i in range(gate._RED_BASE_CACHE_MAX + 10):
    gate.publish_base_green(app, cfg, f"sha{i:04d}")
data = json.loads((_tmp / "red_base_cache.json").read_text())
ok("(4) cache never grows past _RED_BASE_CACHE_MAX",
   len(data) <= gate._RED_BASE_CACHE_MAX, f"len={len(data)}")

# (6) a write failure is swallowed — publishing is best-effort
cfg_bad = types.SimpleNamespace(audit_path="/nonexistent-dir-eu376/audit.jsonl")
try:
    gate.publish_base_green(app, cfg_bad, "abc999")
    ok("(6) a cache-write failure never raises", True)
except Exception as exc:  # noqa: BLE001
    ok("(6) a cache-write failure never raises", False, str(exc))

# (5) wiring pins on _land: after land_trial, green-only, keyed on merge_sha
src = Path("orchestrator/loop.py").read_text()
seg = src[src.find("def _land"):src.find("def _resolve") if "def _resolve" in src[src.find("def _land"):] else len(src)]
land_at = seg.find("git.land_trial(temp)")
pub_at = seg.find("publish_base_green(app, cfg, merge_sha)")
ok("(5) _land publishes AFTER land_trial confirms the fast-forward",
   land_at != -1 and pub_at != -1 and pub_at > land_at,
   "publishing before the push would cache a sha that may never become the base tip")
ok("(5b) the publish is inside the green/live path (after the LandRaceError requeue return)",
   seg.find("land_race_requeue") != -1 and seg.find("land_race_requeue") < pub_at,
   "a LandRaceError means the trial never became dev's head — its sha must not be cached "
   "(a missing requeue token would make find() return -1 and pass vacuously)")

print(f"\n{checks}/{checks} passed")
