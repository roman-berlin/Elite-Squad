"""EU-766: Assert that all 4 orphan pages are reachable via visible links on existing pages.

  1. /report     — link in QA cluster of the per-project control bar
  2. /meeting    — link in council page body
  3. /standup    — link in council page body
  4. /ship-preview — link in council page body AND NOT in control bar (EU-206 guard)

All checks use a Flask test_client so they run without any running server or
network dependencies. The fixture mirrors the pattern from eu206_ship_button_removal_test.py
and eu39_cockpit_regression_test.py.

Note: /budget was retired by EU-855 and now redirects to /usage. That assertion was removed.
"""
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

# Stub the Agent SDK so the orchestrator imports cleanly with no network / models.
_sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s


_sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = _sdk
sys.path.insert(0, ".")

from orchestrator import cockpit_views as V
from orchestrator import server
from orchestrator.config import AppConfig, Config

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ── Shared git fixture ────────────────────────────────────────────────────────────
tmp = Path(tempfile.mkdtemp())


def G(*a):
    subprocess.run(["git", *a], cwd=tmp, check=True, capture_output=True, text=True)


subprocess.run(["git", "init", str(tmp)], check=True, capture_output=True)
G("config", "user.email", "t@t"); G("config", "user.name", "t")
G("checkout", "-b", "DEV")
(tmp / "a.txt").write_text("x\n"); G("add", "-A"); G("commit", "-m", "base")
G("branch", "MAIN", "DEV")

os.environ["GENERAL_COCKPIT_PROMOTE"] = "1"

app_cfg = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="jira",
                    backlog={"base_url": "https://acme.atlassian.net"})
cfg = Config(apps=[app_cfg], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"

client = server.create_app(cfg).test_client()

# ── 1) /report link in QA cluster of control bar ─────────────────────────────────
bar_html = V._control_bar(cfg, "automatixy", healthy=True)
chk("/report link present in control bar next to Run QA",
    'href="/report?app=' in bar_html,
    "Expected <a href=\"/report?app=...\" inside _control_bar")

# ── 2 & 3) /meeting and /standup links in council page ──────────────────────────
council_body = client.get("/council").get_data(as_text=True)
chk("/meeting link present in council page body",
    'href="/meeting"' in council_body,
    "Expected <a href=\"/meeting\" in council page HTML")
chk("/standup link present in council page body",
    'href="/standup"' in council_body,
    "Expected <a href=\"/standup\" in council page HTML")

# ── 4) /ship-preview in council body, NOT in control bar (EU-206 still green) ───
chk("/ship-preview link present in council page body",
    'href="/ship-preview"' in council_body,
    "Expected <a href=\"/ship-preview\" in council page HTML")
chk("/ship-preview NOT in control bar (EU-206 guard)",
    "/ship-preview" not in bar_html,
    "/ship-preview must not appear in _control_bar")

# Note: /budget retired EU-855 — removed this assertion; the new /budget redirect is verified
# by tests/eu855_usage_merge_test.py AC3a instead.

# ── Report results ──────────────────────────────────────────────────────────────
print("\n=============== EU-766 Orphan Page Links Test ===============")
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"PASSED: {passed}/{total}")

for name, ok, detail in results:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail and not ok else ""))
print(f"--------------------------------------------------------------")
print(f"RESULT: {'ALL GREEN' if passed == total else f'{total - passed} FAIL'}")
sys.exit(0 if passed == total else 1)
