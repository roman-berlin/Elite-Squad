"""EU-797a / EU-803: hex-sweep + single-icon guard for the plan-limit banner.

After EU-797 moved banner colours into semantic CSS vars, EU-803 swept any remaining raw hex literals
in the rendered HTML and removed a duplicated warning ⚠ icon.

Assertions:
  • Zero raw #[0-9a-fA-F]{3,6} hex in the banner block returned by _plan_limit_banner()
    (the GLM continue_offer sub-block carries its own button chrome but is outside scope;
     we force it away by leaving GLM_AUTH_TOKEN unset so backends.alternates(native)==[]).
  • Exactly one '&#9888;' entity remains in the banner (was two before the inline prefix was dropped).
  • The banner still contains the semantic var(--…) tokens.
"""
import re
import sys
import types
import os
from pathlib import Path

# Stub the Agent SDK (transitively imported) so no real model is needed.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.backends import NATIVE
from orchestrator import backend_pref, cockpit_views

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# ── Ensure GLM is NOT configured so the continue_offer sub-block stays '' ──
_HAD = os.environ.get("GLM_AUTH_TOKEN")
os.environ.pop("GLM_AUTH_TOKEN", None)

# Force active backend → NATIVE (Opus hit the limit) via the per-app hook,
# matching the convention of tests/eu191_limit_prompt_test.py.
backend_pref.active = lambda cfg=None, app_name=None: NATIVE

state = {
    "plan_limit_hit": True,
    "plan_limit_reset_at": 1_780_000_000,
    "last_run": {"app": "automatixy", "tickets": ["AUTO-99"]},
}

html = cockpit_views._plan_limit_banner(state, cfg=object())

# ---- assertions -----------------------------------------------------------
chk("banner renders non-empty string", len(html) > 0)

chk("no raw hex in banner (excl. HTML numeric entities like &#9888;/&#8212;)",
    not re.search(r"#(?:[0-9a-fA-F]{3,6})", re.sub(r"&#([xX][0-9a-fA-F]+|[0-9]+);", "", html)))

chk("exactly one warning icon (&#9888;)",
    html.count("&#9888;") == 1)

chk("uses var(--badbg)", "var(--badbg)" in html)
chk("uses var(--badline)", "var(--badline)" in html)
chk("uses var(--bad)", "var(--bad)" in html)
chk("uses var(--ink)", "var(--ink)" in html)

chk("heading text preserved ('plan limit reached')",
    "plan limit reached" in html.lower())
chk("implementation paused line present",
    "implementation paused" in html.lower())

if _HAD is not None:
    os.environ["GLM_AUTH_TOKEN"] = _HAD
else:
    os.environ.pop("GLM_AUTH_TOKEN", None)

# ---- report ----------------------------------------------------------------
print("\n========== EU-797a / EU-803 HEX SWEEP + ICON COUNT QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
