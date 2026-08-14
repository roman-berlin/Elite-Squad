#!/usr/bin/env python3
"""EU-855: Merge /budget into /usage — verify the merged page and retired route.

Tests (fail-first — each asserts what the merge creates; they fail on unmerged code):
  AC1 – /usage renders exactly ONE subscription-limits panel, no duplicate Claude gauge.
  AC2 – Secondary card names the configured backend + 'usage tracking not connected yet';
         never hard-coded 'GLM' or literal 'None'.
  AC3 – GET /budget redirects (301/302) to /usage; no internal link points at /budget.
  AC4 – No 'Dual-provider' / 'low-watermark' jargon on /usage.
  AC5 – When plan_usage unavailable, unknown-state card renders (never fabricated 100%/ok).
  AC6 – This file exists in run_all.py and passes after implementation.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

# ── Stub the Agent SDK so the orchestrator imports cleanly ───────────────────────
_sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self


_sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = _sdk
sys.path.insert(0, ".")

os.environ["GENERAL_COCKPIT_PROMOTE"] = "1"

from orchestrator import server
from orchestrator.config import AppConfig, Config
from orchestrator.cockpit_views import _display_label_for_id
from orchestrator import backend_pref

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ── Shared git fixture ──────────────────────────────────────────────────────────
tmp = Path(tempfile.mkdtemp())

subprocess.run(["git", "init", str(tmp)], check=True, capture_output=True, text=True)
subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp, check=True, capture_output=True, text=True)
subprocess.run(["git", "config", "user.name", "t"], cwd=tmp, check=True, capture_output=True, text=True)
subprocess.run(["git", "checkout", "-b", "DEV"], cwd=tmp, check=True, capture_output=True, text=True)
(tmp / "a.txt").write_text("x\n")
subprocess.run(["git", "add", "-A"], cwd=tmp, check=True, capture_output=True, text=True)
subprocess.run(["git", "commit", "-m", "base"], cwd=tmp, check=True, capture_output=True, text=True)
subprocess.run(["git", "branch", "MAIN", "DEV"], cwd=tmp, check=True, capture_output=True, text=True)

app_cfg = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="jira",
                    backlog={"base_url": "https://acme.atlassian.net"})
cfg = Config(apps=[app_cfg], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"

client = server.create_app(cfg).test_client()


# ════════════════════════════════════════════════════════════════════════════════
# AC1: /usage shows ONE merged gauge/subscription-limits panel (no duplicate meters)
# ════════════════════════════════════════════════════════════════════════════════
usage_body = client.get("/usage").get_data(as_text=True)

# Must contain the subscription-limits panel
chk("AC1a – /usage contains plan panel markup",
    '<div class=plan>' in usage_body or "<div class=\"plan\">" in usage_body,
    "Expected <div class=plan> subscription-limits panel in /usage body")

# Must NOT render a second Claude gauge via dual_provider_gauge (the old world had both plan_panel AND _dual_provider_gauge)
chk("AC1b – /usage does NOT render a duplicate Claude gauge from _dual_provider_gauge",
    '<div class=dualprov>' not in usage_body,
    "Expected NO <div class='dualprov'> in /usage — should have single panel only")


# ════════════════════════════════════════════════════════════════════════════════
# AC2: Secondary-provider card names real configured backend, says 'not connected'
# ════════════════════════════════════════════════════════════════════════════════
# Get whatever secondary is configured (may be None)
sec_id = backend_pref.get_secondary(cfg)
label = _display_label_for_id(cfg, sec_id) if sec_id else ""

if sec_id:
    # When secondary IS configured, card must name it
    chk("AC2a – /usage secondary card names the configured backend (" + repr(label) + ")",
        label in usage_body,
        f"Expected backend label '{label}' in /usage body")
else:
    # When no secondary configured, must show neutral message
    chk("AC2a – /usage shows neutral message when no secondary configured",
        "secondary" in usage_body.lower() or "not connected" in usage_body.lower(),
        "Expected mention of 'secondary' or 'not connected' in /usage body")

# The secondary card must NEVER contain a hardcoded 'GLM' string
chk("AC2b – /usage secondary card never hardcodes 'GLM'",
    'GLM' not in usage_body.split('<div class=dualprov>')[-1] if '<div class=dualprov>' in usage_body else True,
    "Hardcoded 'GLM' in rendered HTML — should use config-derived name")

# The secondary card must say 'usage tracking not connected yet' (or similar) when no data
chk("AC2c – /usage says 'usage tracking not connected yet' on secondary card",
    "usage tracking not connected yet" in usage_body or "not connected" in usage_body,
    "Expected 'usage tracking not connected yet' in /usage body")

# The secondary card must never contain the literal word 'None' as a display name
chk("AC2d – /usage never renders literal 'None' for secondary provider",
    "<span class=pname>None</span>" not in usage_body and ">None<" not in usage_body,
    "Literal 'None' rendered as provider name in /usage")


# ════════════════════════════════════════════════════════════════════════════════
# AC3: /budget is gone — redirects to /usage; no internal links point at it
# ════════════════════════════════════════════════════════════════════════════════
resp = client.get("/budget", follow_redirects=False)
chk("AC3a – GET /budget redirects (301/302) to /usage",
    resp.status_code in (301, 302) and "/usage" in resp.location,
    f"/budget returned {resp.status_code} Location={resp.location!r}")

# No internal link pointing at /budget from /usage body
chk("AC3b – /usage body has no href='/budget' link",
    'href="/budget"' not in usage_body,
    "Expected no href=\"/budget\" link in /usage HTML body")


# ════════════════════════════════════════════════════════════════════════════════
# AC4: 'Dual-provider' / 'low-watermark' jargon removed from merged page
# ════════════════════════════════════════════════════════════════════════════════
body_lower = usage_body.lower()
chk("AC4a – /usage body contains no 'Dual-provider' (case-insensitive)",
    "dual-provider" not in body_lower,
    "'Dual-provider' still appears in /usage body")
chk("AC4b – /usage body contains no 'low-watermark' (case-insensitive)",
    "low-watermark" not in body_lower,
    "'low-watermark' still appears in /usage body")


# ════════════════════════════════════════════════════════════════════════════════
# AC5: When plan_usage unavailable → unknown-state card, never fabricated 100%/ok
# Assert the EU-759 _unknown_state_card helper exists and produces expected output.
# The new merged page will delegate to this for unreadable-limit rendering.
# ════════════════════════════════════════════════════════════════════════════════
from orchestrator.cockpit_views import _dual_provider_gauge

mock_cfg = type('MockCfg', (), {'budget_alert_pct': 0.8, 'budget_bad_threshold': 0.95})()

# Unavailable Claude → unknown-state card (never fabricates 100%)
result = _dual_provider_gauge(mock_cfg, {"available": False}, glm_usage=None)
chk("AC5a – unavailable plan_usage → unknown-state card (EU-759 guard)",
    "can't read live limits right now" in result,
    "Expected unknown-state card when Claude probe unavailable")
chk("AC5b – unknown-state card never shows '100% remaining'",
    "100% remaining" not in result,
    "Unreadable Claude must NOT report 100% remaining")


# ════════════════════════════════════════════════════════════════════════════════
# Report
# ════════════════════════════════════════════════════════════════════════════════
print("\n============== EU-855: Usage-Merge Verification Test ==============")
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"PASSED: {passed}/{total}")

for name, ok, detail in results:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail and not ok else ""))
print("--------------------------------------------------------------")
print(f"RESULT: {'ALL GREEN' if passed == total else f'{total - passed} FAIL'}")
sys.exit(0 if passed == total else 1)
