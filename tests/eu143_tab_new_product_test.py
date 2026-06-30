"""EU-143 [Frontend] — "New product" in tab picker instead of separate button.

The Product button is removed from the control bar and integrated into the tab
picker as the first entry, so creating a new project is part of the tab opening flow.
"""
import sys, types, tempfile
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

from orchestrator import cockpit_views as V
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

d = Path(tempfile.mkdtemp()); (d / "audit.jsonl").write_text("")
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(d / "audit.jsonl"), use_worktree=False)

# Render the control bar (which includes the tab bar)
bar = V._control_bar(cfg, "automatixy", True)

# --- 1) tab picker contains "New product" link ---
chk("tab picker contains 'New product' link", "New product" in bar)
chk("New product link goes to /onboard", "/onboard" in bar and 'class=newprod' in bar)

# --- 2) separate Product button is removed from control bar ---
# The old button was: <a class="btn" href="/onboard" title="...">
# It should NOT exist in the output anymore
chk("separate Product button removed from control bar",
    '<a class="btn" href="/onboard"' not in bar)

# --- 3) the newprod CSS class exists for styling ---
chk("newprod CSS class is defined", ".newprod{" in bar or ".newprod{" in V._tab_bar(cfg, "automatixy"))

print("\n============ EU-143 TAB PICKER NEW PRODUCT QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
