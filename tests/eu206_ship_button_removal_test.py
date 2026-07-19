"""EU-206: Verify the per-project "Ship <app>" button is removed from the cockpit UI.

This test FAILS FIRST on the unchanged code (where the button appears) and PASSES after
the button is removed.
"""
import subprocess
import sys
import tempfile
import types
from pathlib import Path

# Mock the claude_agent_sdk
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import cockpit_views as V
from orchestrator.config import AppConfig, Config

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ── Setup: Create a git repo with commits ahead of MAIN ──
tmp = Path(tempfile.mkdtemp())


def G(*a):
    subprocess.run(["git", *a], cwd=tmp, check=True, capture_output=True, text=True)


subprocess.run(["git", "init", str(tmp)], check=True, capture_output=True)
G("config", "user.email", "t@t")
G("config", "user.name", "t")
G("checkout", "-b", "DEV")
(tmp / "a.txt").write_text("x\n")
G("add", "-A")
G("commit", "-m", "base")
G("branch", "MAIN")

# Add commits ahead of MAIN (normally would trigger ship button)
(tmp / "b.txt").write_text("y\n")
G("add", "-A")
G("commit", "-m", "feature-1: add b.txt")
(tmp / "c.txt").write_text("z\n")
G("add", "-A")
G("commit", "-m", "feature-2: add c.txt")

# Enable promote permissions (normally required for ship button)
import os
os.environ["GENERAL_COCKPIT_PROMOTE"] = "1"

app = AppConfig(
    name="automatixy",
    repo_path=str(tmp),
    base_branch="DEV",
    protected_branch="MAIN",
    backlog_backend="jira",
    backlog={"base_url": "https://acme.atlassian.net"}
)
cfg = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)

# ── TEST: Render control bar and verify NO ship button appears ──
bar_html = V._control_bar(cfg, "automatixy", healthy=True)

# The acceptance criteria: NO "Ship <app>" button should render
chk("No 'Ship automatixy' button in control bar",
    "Ship automatixy" not in bar_html,
    "Found: 'Ship automatixy' text should not appear")

chk("No ship-preview link in control bar",
    "/ship-preview" not in bar_html,
    "Found: /ship-preview link should not appear")

chk("No 'shipped' status indicator in control bar",
    "automatixy shipped" not in bar_html,
    "Found: 'shipped' status should not appear")

chk("No ship button class in control bar",
    'class="btn ship"' not in bar_html,
    "Found: ship button class should not appear")

# Verify the control bar still renders other elements (sanity check)
# EU-289 removed "+ New task" (intake is Jira-only) — sentinel on Roster/Reports instead.
chk("Control bar still renders other elements",
    "Roster" in bar_html and "Task log" in bar_html,
    "Control bar should still have other buttons")

# ── TEST: Verify with different app ──
app2 = AppConfig(
    name="testapp",
    repo_path=str(tmp),
    base_branch="DEV",
    protected_branch="MAIN",
    backlog_backend="none"
)
cfg2 = Config(apps=[app2], audit_path=str(tmp / "audit2.jsonl"), use_worktree=False)
bar_html2 = V._control_bar(cfg2, "testapp", healthy=True)

chk("No 'Ship testapp' button for testapp",
    "Ship testapp" not in bar_html2,
    "Found: ship button should not appear for any app")

# ── Report results ──
print("\n=============== EU-206: Ship Button Removal Test ===============")
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"PASSED: {passed}/{total}")

for name, ok, detail in results:
    status = "✓ PASS" if ok else "✗ FAIL"
    print(f"{status}: {name}")
    if detail and not ok:
        print(f"  → {detail}")

sys.exit(0 if passed == total else 1)
