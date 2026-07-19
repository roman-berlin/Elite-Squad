"""EU-191: when Opus/Claude hits its plan limit, the cockpit offers to continue on an available
alternate backend (GLM) and auto-resume the paused ticket — instead of only waiting for the reset.

Extends the existing EU-190 plan-limit banner (a passive "wait for reset" notice) with a one-click
"Continue on <backend>" button. Pins:
  • backends.alternates(current) — the runnable options other than the blocked one.
  • _plan_limit_banner renders the Continue button ONLY when limited + active backend is native + an
    alternate is available; it names the paused ticket for the resume.
  • server wires /api/continue-on-alternate to switch the sticky backend, clear the limit, and resume.
"""
import sys, types, os
from pathlib import Path

# Stub the Agent SDK (imported transitively) so import never needs a real model.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import backends, backend_pref, cockpit_views

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

_HAD_TOKEN = os.environ.get("GLM_AUTH_TOKEN")


# ============ backends.alternates() ============ #
os.environ["GLM_AUTH_TOKEN"] = "test-zai-key"
chk("alternates(native) offers glm when its token is configured",
    backends.alternates(backends.NATIVE) == [backends.GLM], backends.alternates(backends.NATIVE))
chk("alternates(glm) falls back to native (always runnable)",
    backends.NATIVE in backends.alternates(backends.GLM))
os.environ.pop("GLM_AUTH_TOKEN", None)
chk("alternates(native) is empty when glm is NOT configured (fail-closed)",
    backends.alternates(backends.NATIVE) == [], backends.alternates(backends.NATIVE))


# ============ _plan_limit_banner: the Continue-on-GLM button ============ #
# Force the active backend to native so the offer branch is exercised.
# The stub mirrors the REAL active(cfg=None, app_name=None) signature: EU-242 made the banner resolve
# the affected app's backend, and a one-arg stub raises TypeError inside the banner's `except
# Exception`, which blanks the button and fails these pins for a reason that isn't the one they test.
_orig_active = backend_pref.active
backend_pref.active = lambda cfg=None, app_name=None: backends.NATIVE
state = {"plan_limit_hit": True, "plan_limit_reset_at": 1_780_000_000,  # non-zero -> skips a live usage fetch
         "last_run": {"app": "automatixy", "tickets": ["AUTO-99"]}}

# no banner at all when the limit is NOT hit
chk("no banner when plan limit is not hit", cockpit_views._plan_limit_banner({}, cfg=object()) == "")

# GLM available -> Continue button present, names the paused ticket, keeps the wait-for-reset notice
os.environ["GLM_AUTH_TOKEN"] = "test-zai-key"
html_glm = cockpit_views._plan_limit_banner(state, cfg=object())
chk("banner shows the plan-limit warning", "plan limit reached" in html_glm.lower())
chk("banner offers Continue on GLM (button posts to /api/continue-on-alternate)",
    "/api/continue-on-alternate" in html_glm and "Continue on GLM" in html_glm)
chk("Continue button carries backend=glm", "value='glm'" in html_glm or 'value="glm"' in html_glm)
chk("Continue offer names the paused ticket for resume", "AUTO-99" in html_glm)

# GLM NOT configured -> warning still shows, but NO continue button (nothing runnable to offer)
os.environ.pop("GLM_AUTH_TOKEN", None)
html_noglm = cockpit_views._plan_limit_banner(state, cfg=object())
chk("banner still warns when no alternate is available", "plan limit reached" in html_noglm.lower())
chk("no Continue button when no alternate backend is configured",
    "/api/continue-on-alternate" not in html_noglm)

backend_pref.active = _orig_active


# ============ SOURCE: the endpoint switches + clears the limit + auto-resumes ============ #
_srv = Path("orchestrator/server.py").read_text(encoding="utf-8")
chk("server registers POST /api/continue-on-alternate", '"/api/continue-on-alternate"' in _srv)
# it must set the sticky backend, drop the plan-limit flag, and re-run the last tickets
_seg = _srv.split("def continue_on_alternate_api", 1)[-1][:2600]
chk("endpoint switches the sticky backend (backend_pref.set_active, cfg-anchored)",
    "backend_pref.set_active(bk, cfg)" in _seg)
chk("endpoint clears the plan-limit flag (set_plan_limit_hit hit=False)", "set_plan_limit_hit" in _seg and "hit=False" in _seg)
chk("endpoint auto-resumes the last run (_run_bg on the last_run tickets)", "_run_bg(" in _seg and "last_run" in _srv)
chk("run-selected records last_run for the resume", '_state["last_run"]' in _srv)
# Defect 1 (review): the accept must clear the None-keyed flag — the dashboard banner is rendered
# from the unit-wide None-keyed _state, so a per-app-only clear leaves the banner stuck on-screen.
chk("accept clears the None-keyed banner flag (banner would persist otherwise)",
    "[None, *_app_names]" in _seg, "clear loop must include None")
# Detection (review): a MANUAL cockpit run must raise the None-key flag when it trips the Opus cap.
chk("a manual run raises the plan-limit banner on an Opus cap (usage.plan_limit_hit -> set_plan_limit_hit(None))",
    "plan_limit_hit(cfg, force=True)" in _srv and "set_plan_limit_hit(None, hit=True" in _srv)

if _HAD_TOKEN is not None:
    os.environ["GLM_AUTH_TOKEN"] = _HAD_TOKEN
else:
    os.environ.pop("GLM_AUTH_TOKEN", None)

print("\n========== EU-191 PLAN-LIMIT CONTINUE-ON-ALTERNATE QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
