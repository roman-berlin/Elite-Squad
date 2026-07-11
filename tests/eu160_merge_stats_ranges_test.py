"""EU-160 — GET /merge-stats: time range selector (today / this week / this month / all time).

Covers the ticket's five testable acceptance criteria:
  1. GET /merge-stats -> 200, body has a selector control for each of the four ranges, no
     'error' key / traceback.
  2. GET /merge-stats?time_range=week server-renders the week aggregation: the displayed numbers
     equal compute_merge_stats(audit_path, 'week') for seeded events that differ between ranges.
  3. GET /merge-stats with no query param defaults to 'today'; ?time_range=bogus falls back to
     'today' and still returns 200 (no crash).
  4. The control matching the active range is visually distinguished (active class / aria-current)
     while the other three are not.
  5. The page ships client JS that fetches '/api/merge-stats?time_range=' and updates the browser
     URL (history.pushState/replaceState) when a range is selected.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import time
import types
from pathlib import Path

# ── minimal SDK / requests stubs so the orchestrator can import without real deps ──
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        pass

    def __call__(s, *a, **k):
        return s


sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)

req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules.setdefault("requests", req)

sys.path.insert(0, ".")

from orchestrator import server
from orchestrator.config import AppConfig, Config

# ── shared config / audit path / Flask test client ─────────────────────────────
_TMP = Path(tempfile.mkdtemp())
_AUDIT = _TMP / "audit.jsonl"
_CFG = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(_TMP), base_branch="DEV",
                     protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(_AUDIT),
    use_worktree=False,
)
_CFG.detected_auth = lambda: "test"

_APP = server.create_app(_CFG)
_CLIENT = _APP.test_client()

# ── result accumulator ──────────────────────────────────────────────────────────
results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


def _reset_audit() -> None:
    _AUDIT.parent.mkdir(parents=True, exist_ok=True)
    _AUDIT.write_text("", encoding="utf-8")


_event_seq = 0


def _write_event(event: str, ts_epoch: float) -> None:
    global _event_seq
    _event_seq += 1
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts_epoch)), "event": event,
           "ticket_id": f"TEST-{_event_seq}"}
    with _AUDIT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


# =============================================================================
# (1) selector control for each of the four ranges, page renders cleanly
# =============================================================================

def _test_selector_has_all_four_ranges() -> None:
    _reset_audit()
    resp = _CLIENT.get("/merge-stats")
    chk("GET /merge-stats: HTTP 200", resp.status_code == 200, f"status={resp.status_code}")
    body = resp.get_data(as_text=True)
    chk("GET /merge-stats: no 'error' key in body", "'error'" not in body and '"error"' not in body, body[:300])
    chk("GET /merge-stats: no Python traceback", "Traceback (most recent call last)" not in body)
    for r in ("today", "week", "month", "all"):
        chk(f"selector has control for range '{r}'", f"data-range='{r}'" in body, body[:800])
    for label in ("Today", "This week", "This month", "All time"):
        chk(f"selector shows label '{label}'", label in body, body[:800])


_test_selector_has_all_four_ranges()

# =============================================================================
# (2) ?time_range=week server-renders the week aggregation
# =============================================================================

def _test_week_range_server_renders_week_numbers() -> None:
    _reset_audit()
    now = time.time()
    # 2 merges + 1 pr_opened inside "today"; 1 extra merge 3 days ago (in "week" but not "today")
    # so the week total (3 merges) differs from the today total (2 merges).
    _write_event("merged", now)
    _write_event("merged", now)
    _write_event("pr_opened", now)
    _write_event("merged", now - 3 * 86400)
    _write_event("merged", now - 20 * 86400)   # outside "week" (7d), inside "month" (30d)

    expected_week = server.compute_merge_stats(str(_AUDIT), "week")
    expected_today = server.compute_merge_stats(str(_AUDIT), "today")
    chk("fixture sanity: week total != today total", expected_week["total_merges"] != expected_today["total_merges"],
        f"week={expected_week} today={expected_today}")

    resp = _CLIENT.get("/merge-stats?time_range=week")
    chk("GET /merge-stats?time_range=week: HTTP 200", resp.status_code == 200, f"status={resp.status_code}")
    body = resp.get_data(as_text=True)
    chk("week numbers: page shows week total merges count", str(expected_week["total_merges"]) in body, body)
    chk("week numbers: page shows week PRs opened count", str(expected_week["pr_opened"]) in body, body)
    sr = expected_week["success_rate"]
    sr_str = "—" if sr is None else f"{round(sr * 100)}%"
    chk("week numbers: page shows week success rate", sr_str in body, body)


_test_week_range_server_renders_week_numbers()

# =============================================================================
# (3) default range is 'today'; bogus falls back to 'today' without crashing
# =============================================================================

def _test_default_and_bogus_range_fall_back_to_today() -> None:
    _reset_audit()
    now = time.time()
    _write_event("merged", now)
    _write_event("pr_opened", now)
    _write_event("merged", now - 20 * 86400)   # outside "today", inside "month"/"all"
    expected_today = server.compute_merge_stats(str(_AUDIT), "today")

    resp_default = _CLIENT.get("/merge-stats")
    chk("no query param: HTTP 200", resp_default.status_code == 200, f"status={resp_default.status_code}")
    body_default = resp_default.get_data(as_text=True)
    chk("no query param defaults to today's total merges", str(expected_today["total_merges"]) in body_default,
        body_default)
    chk("no query param: 'today' control marked active", "data-range='today' aria-current=page" in body_default
        or re.search(r"data-range='today'[^>]*class='msrange active'", body_default) is not None
        or "class='msrange active' data-range='today'" in body_default, body_default[:800])

    resp_bogus = _CLIENT.get("/merge-stats?time_range=bogus")
    chk("bogus time_range: HTTP 200 (no crash)", resp_bogus.status_code == 200, f"status={resp_bogus.status_code}")
    body_bogus = resp_bogus.get_data(as_text=True)
    chk("bogus time_range: no traceback", "Traceback (most recent call last)" not in body_bogus)
    chk("bogus time_range falls back to today's total merges", str(expected_today["total_merges"]) in body_bogus,
        body_bogus)


_test_default_and_bogus_range_fall_back_to_today()

# =============================================================================
# (4) active range is visually distinguished; the other three are not
# =============================================================================

def _test_active_control_is_visually_distinguished() -> None:
    _reset_audit()
    resp = _CLIENT.get("/merge-stats?time_range=month")
    body = resp.get_data(as_text=True)

    controls = set(re.findall(r"<button[^>]*data-range='(\w+)'[^>]*>", body))
    chk("found all four controls in markup", controls == {"today", "week", "month", "all"}, controls)

    for r in ("today", "week", "month", "all"):
        tag_match = re.search(rf"<button[^>]*data-range='{r}'[^>]*>", body)
        chk(f"control '{r}' present as a single <button> tag", tag_match is not None, body[:800])
        if not tag_match:
            continue
        tag = tag_match.group(0)
        is_active = "active" in tag and "aria-current" in tag
        is_plain = "active" not in tag and "aria-current" not in tag
        if r == "month":
            chk("active range 'month' control has active class + aria-current", is_active, tag)
        else:
            chk(f"non-active range '{r}' control has NO active class / aria-current", is_plain, tag)


_test_active_control_is_visually_distinguished()

# =============================================================================
# (5) client JS fetches the API and updates the URL via history.pushState/replaceState
# =============================================================================

def _test_client_js_fetches_api_and_updates_url() -> None:
    resp = _CLIENT.get("/merge-stats")
    body = resp.get_data(as_text=True)
    chk("page has a <script> block", "<script>" in body, body[-600:])
    chk("client JS fetches /api/merge-stats?time_range=", "fetch('/api/merge-stats?time_range='" in body,
        body[-600:])
    chk("client JS updates the URL via pushState or replaceState",
        "history.pushState" in body or "history.replaceState" in body, body[-600:])
    chk("client JS rewrites the displayed stat numbers (ms-total/ms-pr/ms-sr ids present)",
        "id=ms-total" in body and "id=ms-pr" in body and "id=ms-sr" in body, body[-600:])


_test_client_js_fetches_api_and_updates_url()

# =============================================================================
# Summary
# =============================================================================
passed_n = sum(1 for _, ok, _ in results if ok)
print("\n========= EU-160 merge-stats time range tests =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("---------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed_n == len(results) else f"{len(results) - passed_n} FAIL ❌")
sys.exit(0 if passed_n == len(results) else 1)
