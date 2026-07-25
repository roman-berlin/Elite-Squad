"""EU-159 — GET /merge-stats: the frontend merge-statistics page (today's view).

Covers the ticket's four testable acceptance criteria:
  1. GET /merge-stats -> HTTP 200, body has no 'error' key / traceback (renders cleanly)
  2. Seeded audit events -> the page shows today's aggregated numbers with labels (total merges,
     PRs opened, success rate), matching compute_merge_stats(audit_path, 'today')
  3. warroom.kpis(...) 'Merged -> DEV today' card href == '/merge-stats' (was '/tasks?filter=merged')
  4. The page has a title ('Merge statistics') and the _wrap '<- cockpit' breadcrumb back to home
"""
from __future__ import annotations

import json
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

from orchestrator import server, warroom
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
# (1) GET /merge-stats -> 200, no error key / traceback
# =============================================================================

def _test_page_renders_cleanly() -> None:
    _reset_audit()
    resp = _CLIENT.get("/merge-stats")
    chk("GET /merge-stats: HTTP 200", resp.status_code == 200, f"status={resp.status_code}")
    body = resp.get_data(as_text=True)
    chk("GET /merge-stats: no 'error' key in body", "'error'" not in body and '"error"' not in body, body[:300])
    chk("GET /merge-stats: no Python traceback", "Traceback (most recent call last)" not in body)


_test_page_renders_cleanly()

# =============================================================================
# (2) seeded events -> page shows today's aggregated numbers with labels
# =============================================================================

def _test_page_shows_todays_aggregated_numbers() -> None:
    _reset_audit()
    now = time.time()
    _write_event("merged", now)
    _write_event("merged", now)
    _write_event("pr_opened", now)
    _write_event("merged", now - 10 * 86400)   # 10 days ago — must NOT count toward "today"

    expected = server.compute_merge_stats(str(_AUDIT), "today", now=now)
    resp = _CLIENT.get("/merge-stats")
    body = resp.get_data(as_text=True)

    chk("today numbers: total_merges is 2", expected["total_merges"] == 2, str(expected))
    chk("today numbers: page shows total merges count", str(expected["total_merges"]) in body, body)
    chk("today numbers: page shows PRs opened count", str(expected["pr_opened"]) in body, body)
    chk("today numbers: page has a 'Total merges' label", "Total merges" in body)
    chk("today numbers: page has a 'PRs opened' label", "PRs opened" in body)
    chk("today numbers: page has a 'Success rate' label", "Success rate" in body)
    # success_rate = 2/3 -> 67%
    chk("today numbers: page shows the success rate percentage", "67%" in body, body)


_test_page_shows_todays_aggregated_numbers()


def _test_page_shows_em_dash_when_no_attempts() -> None:
    _reset_audit()
    resp = _CLIENT.get("/merge-stats")
    body = resp.get_data(as_text=True)
    chk("no attempts: success rate renders as em dash, not a crash", "—" in body, body[:400])


_test_page_shows_em_dash_when_no_attempts()

# =============================================================================
# (3) the KPI card links to /merge-stats
# =============================================================================

def _test_kpi_card_links_to_merge_stats_page() -> None:
    cards = {c["label"]: c.get("href") for c in warroom.kpis(_CFG, warroom.D.load_tasks(str(_AUDIT)), None)}
    chk("KPI 'Merged -> DEV today' href == /merge-stats", cards.get("Merged → DEV today") == "/merge-stats",
        str(cards.get("Merged → DEV today")))


_test_kpi_card_links_to_merge_stats_page()

# =============================================================================
# (4) page title + back-to-cockpit breadcrumb
# =============================================================================

def _test_page_title_and_breadcrumb() -> None:
    resp = _CLIENT.get("/merge-stats")
    body = resp.get_data(as_text=True)
    chk("page has 'Merge statistics' title", "Merge statistics" in body, body[:300])
    chk("page has a back-to-cockpit breadcrumb", "backbtn" in body and "aria-label='Back to cockpit'" in body, body[:600])


_test_page_title_and_breadcrumb()

# =============================================================================
# Summary
# =============================================================================
passed_n = sum(1 for _, ok, _ in results if ok)
print("\n========= EU-159 merge-stats page tests =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("---------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed_n == len(results) else f"{len(results) - passed_n} FAIL ❌")
sys.exit(0 if passed_n == len(results) else 1)
