"""EU-760: Merge /budget into /usage — acceptance tests.

Covers:
  (1) Secondary card names the configured backend (never hardcoded "GLM"); a configured
      backend with no connected tracking (Qwen today) gets the honest
      "usage tracking not connected" card — never 'unconfigured' / "isn't set up yet",
      and never GLM's ledger numbers under the Qwen name.
  (2) /budget redirects to /usage (3xx); the standalone budget_page endpoint is gone.
  (3) /usage renders a single gauge (plan_usage called exactly once per request;
      exactly one provider card — no duplicate meters).
  (4) No 'Dual-provider' or 'low-watermark' jargon in /usage HTML.
  (5) Unknown-state honesty when plan_usage has no limits (not fabricated 100%).
"""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

# ── minimal SDK stubs so server / warroom can import cleanly ────────────────────
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self


sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)

req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules.setdefault("requests", req)

sys.path.insert(0, ".")

from orchestrator import server, sync
from orchestrator.config import AppConfig, Config

# ── shared test config ──────────────────────────────────────────────────────────
_TMP = Path(tempfile.mkdtemp())
_CFG = Config(
    apps=[
        AppConfig(
            name="automatixy",
            repo_path=str(_TMP),
            base_branch="DEV",
            protected_branch="MAIN",
            backlog_backend="none",
        )
    ],
    audit_path=str(_TMP / "a.jsonl"),
    use_worktree=False,
)
_CFG.detected_auth = lambda: "test"
sync.can_promote = lambda: False

_APP = server.create_app(_CFG)
_CLIENT = _APP.test_client()

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


# =============================================================================
# (1) Secondary card names the CONFIGURED backend — never hardcoded "GLM" —
#     and a configured-but-untracked secondary (Qwen) shows the honest
#     "usage tracking not connected" state, NOT GLM's ledger numbers.
# =============================================================================

with patch("orchestrator.backend_pref.get_secondary", return_value="qwen"):
    with patch("orchestrator.usage.plan_usage", return_value={
        "available": True,
        "limits": [{"utilization": 0.3, "label": "monthly"}],
    }):
        resp = _CLIENT.get("/usage")
        html = resp.get_data(as_text=True) if resp.data else ""

chk(
    "(1a) secondary card shows configured backend display name (Qwen)",
    "Qwen" in html,
    "'Qwen' expected in usage page HTML (secondary card name)",
)
# EU-760 review: the old OR-based check passed vacuously. Hard conjunction instead —
# the not-connected copy MUST be present and BOTH stale placeholder strings MUST be absent.
chk(
    "(1b) configured-but-untracked secondary → 'usage tracking not connected', no stale copy",
    ("usage tracking not connected" in html
     and "isn't set up yet" not in html
     and "unconfigured" not in html.lower()),
    "expected not-connected card; no 'isn't set up yet' / 'unconfigured' anywhere",
)

# The Qwen card must carry NO ledger numbers: /usage always has a glm_budget_status() dict,
# and displaying GLM's burn under the resolved Qwen label is exactly the mislabeling EU-760 fixes.
# Bound the slice to the card's own markup (its closing </div></div>) so a later page section's
# bar (e.g. the daily-budget gauge) can never leak into the assertion.
_q_anchor = html.find("pname>Qwen<")
_q_end = html.find("</div></div>", _q_anchor) if _q_anchor != -1 else -1
_q_card = html[_q_anchor:_q_end + len("</div></div>")] if _q_end != -1 else ""
chk(
    "(1c) Qwen card shows no ledger gauge (GLM burn never mislabeled as Qwen)",
    _q_anchor != -1 and "% used" not in _q_card and "width:" not in _q_card,
    _q_card[:240],
)

# =============================================================================
# (2) /budget redirects to /usage (no standalone budget_page anymore)
# =============================================================================

r_budget = _CLIENT.get("/budget")
chk(
    "(2a) GET /budget returns 3xx redirect",
    300 <= r_budget.status_code < 400,
    f"status={r_budget.status_code}, expected 3xx",
)
loc = (r_budget.headers.get("Location") or "").lower()
chk(
    "(2b) /budget redirect target is /usage",
    "/usage" in loc,
    f"Location header: {r_budget.headers.get('Location', '')!r}",
)

endpoints = [rule.endpoint for rule in _APP.url_map.iter_rules()]
chk(
    "(2c) budget_page endpoint removed from URL map",
    "budget_page" not in endpoints,
    f"budget-like endpoints: {sorted(e for e in endpoints if 'budget' in e)}",
)
chk(
    "(2d) the /budget redirect route itself is still registered",
    "budget_redirect" in endpoints,
    f"budget-like endpoints: {sorted(e for e in endpoints if 'budget' in e)}",
)


# =============================================================================
# (3) /usage renders a SINGLE gauge: plan_usage computed once, one provider card
# =============================================================================

call_count = 0


def _counting_plan_usage(*a, **kw):
    global call_count
    call_count += 1
    return {
        "available": True,
        "limits": [{"utilization": 0.25, "label": "monthly"}],
    }


with patch("orchestrator.usage.plan_usage", side_effect=_counting_plan_usage):
    call_count = 0
    resp = _CLIENT.get("/usage")
    body3 = resp.get_data(as_text=True) if resp.data else ""
    assert resp.status_code == 200, f"/usage returned {resp.status_code}"

chk(
    "(3a) plan_usage(cfg) called exactly ONCE per /usage request",
    call_count == 1,
    f"called {call_count} times (expected 1; duplicated before EU-760)",
)
chk(
    "(3b) /usage still renders successfully after merge",
    resp.status_code == 200,
    f"status={resp.status_code}",
)
chk(
    "(3c) exactly ONE provider gauge card on the merged page (no duplicate meters)",
    body3.count("class=provcard") == 1,
    f"provcard count={body3.count('class=provcard')}",
)

# =============================================================================
# (4) No 'Dual-provider' / 'low-watermark' jargon in merged page
# =============================================================================

with patch("orchestrator.usage.plan_usage", return_value={
    "available": True,
    "limits": [{"utilization": 0.1, "label": "monthly"}],
}):
    resp = _CLIENT.get("/usage")
    html = resp.get_data(as_text=True) if resp.data else ""
    chk(
        "(4a) 'Dual-provider' not anywhere in response HTML",
        "Dual-provider" not in html,
        "'Dual-provider' found in HTML",
    )
    chk(
        "(4b) 'low-watermark' not anywhere in response HTML",
        "low-watermark" not in html.lower(),
        "'low-watermark' found in HTML",
    )


# =============================================================================
# (5) Unknown-state honesty when plan_usage has no usable limits
#          (never fabricate 100% / ok — builds on the EU-759 work)
# =============================================================================

fake_unknown = {"available": True, "limits": []}
with patch("orchestrator.usage.plan_usage", return_value=fake_unknown):
    with patch("orchestrator.backend_pref.get_secondary", return_value=None):
        resp = _CLIENT.get("/usage")
        html = resp.get_data(as_text=True) if resp.data else ""

chk(
    "(5a) unknown state shows \"can't read live limits\"",
    "can't read live limits" in html,
    "Expected honest unknown-state text in the merged panel",
)
chk(
    "(5b) unknown state never fabricates a healthy gauge",
    "100% remaining" not in html,
    "fabricated 100% remaining found (the EU-759 bug)",
)

print("\n============ EU-760 USAGE+BUDGET MERGE QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
