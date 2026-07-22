"""EU-19 regression: the gate typechecks the apps a ticket TOUCHES, not only zeltivo-crm.

Proves (against the real, un-mocked gate with bounded shell commands):
  • An AUTO-9-style landing-page/microsite ticket runs ONLY landing-page + microsite gates.
  • A ticket that doesn't touch zeltivo-crm is NOT blocked by zeltivo-crm's (failing) gate
    — unless a changed shared package links them.
  • Gate failure messages NAME the app that failed.
  • Detection falls back to the repo-wide gate_commands when ambiguous, and the old
    single-command behaviour is unchanged when no per-app config is present.
"""
import sys, types
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import gate
from orchestrator.config import AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# A monorepo app where each component typechecks via its own command. We use plain
# shell commands (true/false) instead of real `cd ... && tsc` so the test is hermetic.
def app(by_app=None, shared=None, default=None):
    return AppConfig(
        name="automatixy", repo_path="/tmp", base_branch="DEV", workdir="/tmp",
        gate_commands=list(default or []),
        gate_commands_by_app=dict(by_app or {}),
        gate_shared_packages=dict(shared or {}),
        gate_timeout_sec=30,
    )

PASS = ["true"]                       # a component whose gate passes
FAIL = ["sh -c 'echo boom; exit 3'"]  # a component whose gate fails

# --- touched_components: maps diff paths to component names ---
tc = gate.touched_components([
    "apps/landing-page/src/Hero.tsx",
    "apps/microsite/pages/index.tsx",
    "README.md",                       # ignored (not under apps/ or packages/)
])
chk("detects landing-page + microsite from diff paths", tc == ["landing-page", "microsite"], str(tc))
chk("ignores paths outside apps/ and packages/", "README.md" not in tc)

# --- AC1: an AUTO-9-style ticket runs tsc for landing-page + microsite (and ONLY those) ---
a = app(by_app={"zeltivo-crm": FAIL, "landing-page": PASS, "microsite": PASS})
groups = gate.select_gate_groups(a, ["apps/landing-page/x.ts", "apps/microsite/y.ts"])
names = [n for n, _ in groups]
chk("AC1: runs landing-page + microsite gates", set(names) == {"landing-page", "microsite"}, str(names))
chk("AC1: does NOT run zeltivo-crm gate", "zeltivo-crm" not in names)

# --- AC2: a ticket that doesn't touch zeltivo-crm isn't blocked by its (failing) gate ---
res = gate.run_gate(a, ["apps/landing-page/x.ts", "apps/microsite/y.ts"])
chk("AC2: landing/microsite-only ticket PASSES despite broken zeltivo-crm", res.passed, res.report)

# --- AC2 (the exception): a changed shared package re-gates the apps that depend on it ---
a2 = app(by_app={"zeltivo-crm": FAIL, "landing-page": PASS},
         shared={"ui": ["zeltivo-crm", "landing-page"]})
shared_groups = [n for n, _ in gate.select_gate_groups(a2, ["packages/ui/Button.tsx"])]
chk("AC2: a changed shared package links in its dependent apps",
    set(shared_groups) == {"zeltivo-crm", "landing-page"}, str(shared_groups))
shared_res = gate.run_gate(a2, ["packages/ui/Button.tsx"])
chk("AC2: shared-package change is blocked by the linked failing app", not shared_res.passed)

# --- AC3: gate failure messages name the app that failed ---
a3 = app(by_app={"landing-page": FAIL, "microsite": PASS})
fail_res = gate.run_gate(a3, ["apps/landing-page/x.ts", "apps/microsite/y.ts"])
chk("AC3: failure is reported", not fail_res.passed)
chk("AC3: failure names the failing app (landing-page)", "[landing-page]" in (fail_res.report or ""), fail_res.report)
chk("AC3: failure does NOT name the passing app", "[microsite]" not in (fail_res.report or ""))

# --- Fallback: ambiguous detection uses the repo-wide default gate ---
a4 = app(by_app={"landing-page": PASS}, default=FAIL)
fb = gate.run_gate(a4, ["docs/notes.md", "scripts/build.sh"])   # nothing under apps/ or packages/
chk("fallback: no monorepo component changed -> repo-wide default runs", not fb.passed)
fb2 = gate.run_gate(a4, ["apps/unconfigured-app/x.ts"])          # touched but not configured
chk("fallback: touched-but-unconfigured component -> repo-wide default runs", not fb2.passed)

# --- Backward compatibility: no per-app config behaves exactly as before ---
legacy = app(default=PASS)
chk("legacy: no per-app config + no changed paths -> default gate runs (pass)",
    gate.run_gate(legacy).passed)
chk("legacy: empty everything is REFUSED (EU-432 pin — no ungated build)",
    not gate.run_gate(app()).passed)

print("\n================= GATE PER-APP (EU-19) QA =================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
