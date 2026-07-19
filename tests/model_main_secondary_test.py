"""MAIN + SECONDARY model (2026-07-19, Commander order) — the loud fallback EU-108/118 promised.

The mental model: a Main model (what the unit runs on), an optional Secondary (the stand-in it
switches to — LOUDLY — when the main can't run: Claude plan limit hit, GLM token missing), and
/models to add more. No secondary configured → behaviour is byte-identical to the old world
(hard block / pause).

Pins:
  (1) backend_pref.set_secondary/get_secondary persist + clear ('none'/None) the pick;
  (2) resolve_for_run: main usable → (main, None), no fallback reason;
  (3) main Claude + plan limit hit + secondary GLM usable → (glm, reason);
  (4) main Claude capped + NO secondary → (main, None) — old pause path untouched;
  (5) main GLM broken (config issues) + secondary opus → (opus, reason);
  (6) secondary == main is never used as a stand-in;
  (7) POST /api/model secondary=<id> persists; secondary=none clears;
  (8) the autopilot plan-limit branch consults resolve_for_run and only pauses when there is
      no usable secondary (source pin), and switches the drain copy back to the main model
      when the limit clears (source pin);
  (9) the toolbar renders Main model + Secondary + the ＋ Add model link to /models."""
import sys
import tempfile
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import backend_pref, backends, usage
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev",
                             protected_branch="main", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)

# ── (1) pref persistence ──
chk("(1a) no secondary by default", backend_pref.get_secondary(cfg) is None)
backend_pref.set_secondary("glm", cfg)
chk("(1b) secondary persists", backend_pref.get_secondary(cfg) == "glm")
backend_pref.set_secondary("none", cfg)
chk("(1c) 'none' clears it", backend_pref.get_secondary(cfg) is None)
backend_pref.set_secondary("glm", cfg)

_orig_plan = usage.plan_limit_hit
_orig_issues = backends.glm_config_issues

# ── (2) main usable → main, no reason ──
backend_pref.set_active("opus", cfg)
usage.plan_limit_hit = lambda c=None: {"hit": False}
backends.glm_config_issues = lambda: []
bk, why = backends.resolve_for_run(cfg, "automatixy")
chk("(2) main usable → main, no fallback", bk == "opus" and why is None, f"{bk} {why}")

# ── (3) main capped + secondary usable → secondary, with reason ──
usage.plan_limit_hit = lambda c=None: {"hit": True, "over_limits": [{"key": "weekly"}]}
bk, why = backends.resolve_for_run(cfg, "automatixy")
chk("(3) plan limit + usable secondary → the secondary carries the run",
    bk == "glm" and why is not None and "secondary" in why, f"{bk} {why}")

# ── (4) main capped + NO secondary → main unchanged (old pause path) ──
backend_pref.set_secondary(None, cfg)
bk, why = backends.resolve_for_run(cfg, "automatixy")
chk("(4) capped with no secondary → old behaviour (main, no reason)",
    bk == "opus" and why is None, f"{bk} {why}")
backend_pref.set_secondary("glm", cfg)

# ── (5) main GLM broken + secondary opus → opus ──
backend_pref.set_active("glm", cfg)
backend_pref.set_secondary("opus", cfg)
usage.plan_limit_hit = lambda c=None: {"hit": False}
backends.glm_config_issues = lambda: ["GLM_AUTH_TOKEN is not set"]
bk, why = backends.resolve_for_run(cfg, "automatixy")
chk("(5) broken GLM main + opus secondary → opus, with reason",
    bk == "opus" and why is not None, f"{bk} {why}")

# ── (6) secondary == main is not a stand-in ──
backend_pref.set_secondary("glm", cfg)
bk, why = backends.resolve_for_run(cfg, "automatixy")
chk("(6) secondary == broken main → no fallback (main returned)",
    bk == "glm" and why is None, f"{bk} {why}")

usage.plan_limit_hit = _orig_plan
backends.glm_config_issues = _orig_issues

# ── (7) /api/model secondary= round-trip ──
from orchestrator import server
backend_pref.set_active("opus", cfg)
backend_pref.set_secondary(None, cfg)
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()
client.post("/api/model", data={"secondary": "glm"})
chk("(7a) POST secondary=glm persists", backend_pref.get_secondary(cfg) == "glm")
client.post("/api/model", data={"secondary": "none"})
chk("(7b) POST secondary=none clears", backend_pref.get_secondary(cfg) is None)

# ── (8) autopilot wiring (source pins) ──
asrc = Path("orchestrator/autopilot.py").read_text(encoding="utf-8")
chk("(8a) the plan-limit branch consults resolve_for_run before pausing",
    "resolve_for_run(cfg, app_name)" in asrc and
    asrc.index("resolve_for_run(cfg, app_name)") < asrc.index("Halt autopilot when plan limits are hit"))
chk("(8b) the pause is the no-usable-secondary branch",
    "no usable secondary configured" in asrc)
chk("(8c) the drain switches back to the MAIN model when the cap clears",
    "model_fallback_cleared" in asrc)
chk("(8d) the resume path resolves main→secondary too",
    "backends.resolve_for_run(ap_cfg, app_name)" in asrc)

# ── (9) toolbar renders the new mental model ──
from orchestrator import cockpit_views
bar = cockpit_views.backend_control(cfg, "automatixy")
chk("(9a) Main model selector", "Main model" in bar)
chk("(9b) Secondary selector posting secondary=", "name=secondary" in bar)
chk("(9c) ＋ Add model links to the /models registry page", 'href="/models"' in bar)

print("\n========== MAIN + SECONDARY MODEL QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
