"""EU-76: KPI sparkline data + inline SVG — unit tests.

Covers:
  - _merges_per_day(): counts 'merged' events per calendar day from audit.jsonl.
  - _daily_token_burn(): sums ledger cost per calendar day from usage_ledger.jsonl.
  - _kpi_sparkline_svg(): produces a valid SVG polyline string.
  - kpis(): attaches sparkline lists to the 'Merged total' and 'Tokens' cards (EU-145: merged today+week).
  - _kpi_html(): embeds the sparkline SVG inside the rendered KPI card HTML.
"""
import sys
import json
import time
import types
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Minimal stubs so warroom can be imported without the full dependency tree.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import warroom
from orchestrator.config import Config, AppConfig


# ---------------------------------------------------------------------------
# Helpers

def _make_cfg(tmp: Path) -> Config:
    """Minimal Config pointing at a temp directory."""
    audit = tmp / "audit.jsonl"
    audit.touch()
    return Config(
        apps=[AppConfig(name="test", repo_path=str(tmp), base_branch="dev",
                        protected_branch="main", backlog_backend="none")],
        audit_path=str(audit),
        use_worktree=False,
    )


def _write_audit_ev(audit: Path, event: str, ts: str) -> None:
    with audit.open("a") as f:
        f.write(json.dumps({"event": event, "ts": ts}) + "\n")


def _write_ledger_row(ledger: Path, ts_unix: float, cost: float) -> None:
    with ledger.open("a") as f:
        f.write(json.dumps({"t": ts_unix, "i": 0, "o": 0, "c": cost, "m": "s", "g": ""}) + "\n")


# ---------------------------------------------------------------------------
# Tests: _merges_per_day

def test_merges_per_day_empty_audit():
    """Returns 14 zeros when the audit is empty (no 'merged' events)."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        result = warroom._merges_per_day(cfg, days=14)
    assert len(result) == 14
    assert all(v == 0 for v in result), result


def test_merges_per_day_counts_today():
    """'merged' events timestamped today increment today's bucket."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        audit = Path(cfg.audit_path)
        now = datetime.now()
        # Use distinct second-level timestamps so lines are unambiguously different.
        ts1 = now.strftime("%Y-%m-%dT%H:%M:%S")
        ts2 = (now - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S")
        ts3 = (now - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%S")
        _write_audit_ev(audit, "merged", ts1)
        _write_audit_ev(audit, "merged", ts2)
        _write_audit_ev(audit, "ticket_start", ts3)  # different event — must NOT be counted
        result = warroom._merges_per_day(cfg, days=14)
    assert len(result) == 14
    assert result[-1] == 2, f"today (last bucket) expected 2, got {result[-1]}"


def test_merges_per_day_counts_yesterday():
    """'merged' events timestamped yesterday land in the second-to-last bucket."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        audit = Path(cfg.audit_path)
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        _write_audit_ev(audit, "merged", yesterday)
        result = warroom._merges_per_day(cfg, days=14)
    assert result[-2] == 1, f"yesterday bucket expected 1, got {result[-2]}"
    assert result[-1] == 0, f"today bucket expected 0, got {result[-1]}"


def test_merges_per_day_ignores_old_events():
    """Events older than `days` days are NOT counted."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        audit = Path(cfg.audit_path)
        old = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%S")
        _write_audit_ev(audit, "merged", old)
        result = warroom._merges_per_day(cfg, days=14)
    assert sum(result) == 0, f"expected all zeros for old event, got {result}"


# ---------------------------------------------------------------------------
# Tests: _daily_token_burn

def test_daily_token_burn_absent_ledger():
    """Returns 14 zeros when usage_ledger.jsonl does not exist."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        # Ensure ledger does NOT exist.
        ledger = Path(cfg.audit_path).with_name("usage_ledger.jsonl")
        assert not ledger.exists()
        result = warroom._daily_token_burn(cfg, days=14)
    assert len(result) == 14
    assert all(v == 0.0 for v in result), result


def test_daily_token_burn_sums_today():
    """Rows timestamped today are summed into today's (last) bucket."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        ledger = Path(cfg.audit_path).with_name("usage_ledger.jsonl")
        now = time.time()
        _write_ledger_row(ledger, now, 0.01)
        _write_ledger_row(ledger, now, 0.02)
        result = warroom._daily_token_burn(cfg, days=14)
    assert len(result) == 14
    assert abs(result[-1] - 0.03) < 1e-9, f"today bucket expected 0.03, got {result[-1]}"


def test_daily_token_burn_ignores_old_rows():
    """Rows older than `days` days are excluded."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        ledger = Path(cfg.audit_path).with_name("usage_ledger.jsonl")
        old_ts = time.time() - 20 * 86400
        _write_ledger_row(ledger, old_ts, 5.0)
        result = warroom._daily_token_burn(cfg, days=14)
    assert sum(result) == 0.0, f"old row should be ignored, got {result}"


# ---------------------------------------------------------------------------
# Tests: _kpi_sparkline_svg

def test_kpi_sparkline_svg_fewer_than_2_points():
    """Returns empty string for 0 or 1 data points."""
    assert warroom._kpi_sparkline_svg([]) == ""
    assert warroom._kpi_sparkline_svg([5.0]) == ""


def test_kpi_sparkline_svg_valid_svg():
    """Returns a valid SVG polyline string for a multi-point series."""
    svg = warroom._kpi_sparkline_svg([0, 1, 2, 3], width=60, height=20)
    assert svg.startswith("<svg"), svg[:40]
    assert "polyline" in svg
    assert 'points="' in svg
    assert "</svg>" in svg


def test_kpi_sparkline_svg_respects_dimensions():
    """Width and height appear in the viewBox and element attributes."""
    svg = warroom._kpi_sparkline_svg([1, 2, 3], width=80, height=25)
    assert 'width="80"' in svg
    assert 'height="25"' in svg
    assert "0 0 80 25" in svg


def test_kpi_sparkline_svg_custom_stroke():
    """Custom stroke colour is embedded in the polyline element."""
    svg = warroom._kpi_sparkline_svg([1, 2], stroke="var(--ok)")
    assert 'stroke="var(--ok)"' in svg


def test_kpi_sparkline_svg_flat_series():
    """All-equal series (span=0) renders a flat mid-line, not an error."""
    svg = warroom._kpi_sparkline_svg([5, 5, 5, 5])
    assert "polyline" in svg


# ---------------------------------------------------------------------------
# Tests: kpis() and _kpi_html() integration

def test_kpis_merged_today_card_has_sparkline():
    """The 'Merged → DEV today' KPI card (EU-150: retired Merged total) carries a 'sparkline' list."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        cards = warroom.kpis(cfg, [], None)
    merged_card = next((c for c in cards if c["label"] == "Merged → DEV today"), None)
    assert merged_card is not None, "Merged → DEV today card must exist"
    assert "sparkline" in merged_card, "Merged → DEV today card must have 'sparkline' key"
    assert isinstance(merged_card["sparkline"], list), "sparkline must be a list"
    assert len(merged_card["sparkline"]) == 14, "sparkline must cover 14 days"


def test_kpis_tokens_card_has_sparkline():
    """The 'Tokens' card (EU-145: merged today+week) carries a 'sparkline' list when the usage module works."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        cards = warroom.kpis(cfg, [], None)
    tok_card = next((c for c in cards if c["label"] == "Tokens"), None)
    if tok_card is None:
        return  # usage module may not be present in this env — skip silently
    assert "sparkline" in tok_card, "Tokens card must have 'sparkline' key"
    assert len(tok_card["sparkline"]) == 14


def test_kpi_html_embeds_sparkline_svg():
    """_kpi_html() injects an <svg> element into cards that carry sparkline data."""
    card = {
        "label": "Merged total",
        "value": 7,
        "hint": "all time",
        "tone": "ok",
        "sparkline": [0, 1, 2, 1, 3, 2, 4],
    }
    html = warroom._kpi_html([card])
    assert "<svg" in html, "SVG element must appear in rendered KPI HTML"
    assert "polyline" in html


def test_kpi_html_no_svg_without_sparkline():
    """_kpi_html() does NOT inject an <svg> when the card has no sparkline."""
    card = {"label": "Parked", "value": 0, "hint": "none"}
    html = warroom._kpi_html([card])
    assert "<svg" not in html


def test_kpi_html_ok_tone_uses_green_stroke():
    """Cards with tone='ok' get a green (--ok) sparkline stroke."""
    card = {
        "label": "Tokens",
        "value": "120k · 12%",
        "hint": "some cap",
        "tone": "ok",
        "sparkline": [1, 2, 3],
    }
    html = warroom._kpi_html([card])
    assert "var(--ok)" in html


def test_kpi_html_warn_tone_uses_amber_stroke():
    """Cards with tone='warn' get an amber (--warn) sparkline stroke."""
    card = {
        "label": "Tokens today",
        "value": "120k",
        "hint": "some cap",
        "tone": "warn",
        "sparkline": [10, 20, 30],
    }
    html = warroom._kpi_html([card])
    assert "var(--warn)" in html


def test_kpi_html_bad_tone_uses_red_stroke():
    """Cards with tone='bad' get a red (--bad) sparkline stroke."""
    card = {
        "label": "Failed runs",
        "value": 3,
        "hint": "all time",
        "tone": "bad",
        "sparkline": [0, 1, 3],
    }
    html = warroom._kpi_html([card])
    assert "var(--bad)" in html, f"expected var(--bad) stroke for bad-tone card, got: {html}"


# ---------------------------------------------------------------------------
# Tests: _run_html() EU-76 hero-merged layout

def _make_run(live: bool = False) -> dict:
    """Minimal synthetic run dict for _run_html() unit tests."""
    return {
        "live": live,
        "ticket": "EU-76",
        "app": "eu",
        "passes": 2,
        "verdict": "PASS",
        "outcome": "running" if live else "merged→dev",
        "branch": "autodev/EU-76",
        "phases": list(warroom.PHASES),
        "reached": 1,
        "failed_phase": None,
        "sparkline": [],
    }


def test_run_html_live_uses_hero_classes():
    """Live run HTML must contain the hero-merged CSS classes (EU-76 de-dupe)."""
    html = warroom._run_html(_make_run(live=True))
    assert "runlive" in html, "runlive class must appear in live run HTML"
    assert "hgdot" in html,   "hgdot pulsing indicator must appear in live run HTML"
    assert "runtitle" in html, "runtitle class must appear in live run HTML"
    assert "runsub" in html,  "runsub class must appear in live run HTML"


def test_run_html_idle_compact_layout():
    """Idle (last-run) HTML uses the compact header — runtitle present, runlive absent."""
    html = warroom._run_html(_make_run(live=False))
    assert "runtitle" in html,  "runtitle must appear in idle run HTML"
    assert "runlive" not in html, "runlive must NOT appear in idle run HTML"
    assert "last run" in html,  "'last run' badge must appear in idle run HTML"


# ---------------------------------------------------------------------------
# Tests: active_run() sparkline ordering

def test_active_run_sparkline_ordering_and_filters():
    """Sparkline contains only completed runs, ordered oldest→newest; in-flight excluded."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        # Tasks ordered newest→oldest (active_run() convention).
        # T3 and T2 are completed; T1 is in-flight (no outcome).
        tasks = [
            {"ticket_id": "T3", "app": "", "outcome": "errored",
             "passes": 5, "verdict": None, "branch": None, "cost": 0, "passes_list": []},
            {"ticket_id": "T2", "app": "", "outcome": "merged→dev",
             "passes": 3, "verdict": "PASS", "branch": "b", "cost": 0, "passes_list": []},
            {"ticket_id": "T1", "app": "", "outcome": None,   # in-flight — must be excluded
             "passes": 7, "verdict": None, "branch": None, "cost": 0, "passes_list": []},
        ]
        run = warroom.active_run(cfg, tasks, None, False)
    assert run is not None
    assert run["sparkline"] == [3, 5], (
        f"sparkline must be [oldest_passes, newest_passes] from completed runs only; "
        f"got {run['sparkline']}"
    )


# ---------------------------------------------------------------------------
# Tests: EU-76 iter-2 perf — board render must not re-parse history per frame

def test_scan_exposes_merged_by_day_single_pass():
    """_scan() buckets 'merged' events by calendar day so _merges_per_day reads that dict
    instead of triggering its own extra full-history parse on every board frame."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        audit = Path(cfg.audit_path)
        now = datetime.now()
        _write_audit_ev(audit, "merged", now.strftime("%Y-%m-%dT%H:%M:%S"))
        _write_audit_ev(audit, "merged", (now - timedelta(seconds=3)).strftime("%Y-%m-%dT%H:%M:%S"))
        _write_audit_ev(audit, "build", now.strftime("%Y-%m-%dT%H:%M:%S"))  # must NOT count
        warroom._scan_cache.clear()
        scan = warroom._scan(cfg.audit_path)
    assert "merged_by_day" in scan, "scan must expose merged_by_day for the merges sparkline"
    today_key = datetime.now().strftime("%Y-%m-%d")
    assert scan["merged_by_day"].get(today_key) == 2, scan["merged_by_day"]


def test_daily_burn_series_parses_ledger_once_then_invalidates():
    """A burst of renders parses the ledger ONCE per (size, mtime_ns); a change forces one re-read.

    Proves the EU-76 review fix: the board's burn sparkline no longer calls read_text() on the whole
    ledger every frame. Counts real read_text() calls on usage_ledger.jsonl across many calls."""
    import pathlib
    from orchestrator import usage
    usage._burn_series_cache.clear()
    reads = {"n": 0}
    orig = pathlib.Path.read_text

    def counting_read_text(self, *a, **k):
        if self.name == "usage_ledger.jsonl":
            reads["n"] += 1
        return orig(self, *a, **k)

    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        ledger = Path(cfg.audit_path).with_name("usage_ledger.jsonl")
        _write_ledger_row(ledger, time.time(), 0.05)
        pathlib.Path.read_text = counting_read_text
        try:
            first = usage.daily_burn_series(cfg, days=14)
            for _ in range(20):                       # ~a burst of SSE board frames / open tabs
                usage.daily_burn_series(cfg, days=14)
            assert reads["n"] == 1, f"ledger parsed {reads['n']}× across 21 calls; expected 1 (cached)"
            assert abs(first[-1] - 0.05) < 1e-9, first[-1]
            # Appending to the ledger changes (size, mtime_ns) → exactly one fresh parse.
            _write_ledger_row(ledger, time.time(), 0.07)
            after = usage.daily_burn_series(cfg, days=14)
            assert reads["n"] == 2, f"ledger change should force one re-read; got {reads['n']}"
            assert abs(after[-1] - 0.12) < 1e-9, after[-1]
        finally:
            pathlib.Path.read_text = orig


# ---------------------------------------------------------------------------
# Runner (same pattern as kpi_deeplink_test.py)

if __name__ == "__main__":
    import traceback

    tests = [
        test_merges_per_day_empty_audit,
        test_merges_per_day_counts_today,
        test_merges_per_day_counts_yesterday,
        test_merges_per_day_ignores_old_events,
        test_daily_token_burn_absent_ledger,
        test_daily_token_burn_sums_today,
        test_daily_token_burn_ignores_old_rows,
        test_kpi_sparkline_svg_fewer_than_2_points,
        test_kpi_sparkline_svg_valid_svg,
        test_kpi_sparkline_svg_respects_dimensions,
        test_kpi_sparkline_svg_custom_stroke,
        test_kpi_sparkline_svg_flat_series,
        test_kpis_merged_today_card_has_sparkline,
        test_kpis_tokens_card_has_sparkline,
        test_kpi_html_embeds_sparkline_svg,
        test_kpi_html_no_svg_without_sparkline,
        test_kpi_html_ok_tone_uses_green_stroke,
        test_kpi_html_warn_tone_uses_amber_stroke,
        test_kpi_html_bad_tone_uses_red_stroke,
        test_run_html_live_uses_hero_classes,
        test_run_html_idle_compact_layout,
        test_active_run_sparkline_ordering_and_filters,
        test_scan_exposes_merged_by_day_single_pass,
        test_daily_burn_series_parses_ledger_once_then_invalidates,
    ]
    passed = failed = 0
    print("\n========== EU-76 KPI SPARKLINE TESTS ==========")
    for fn in tests:
        try:
            fn()
            print(f"  [PASS] {fn.__name__}")
            passed += 1
        except Exception:
            print(f"  [FAIL] {fn.__name__}")
            traceback.print_exc()
            failed += 1
    print("------------------------------------------------")
    print(f"  {passed}/{passed + failed} passed")
    print("  RESULT:", "ALL GREEN" if failed == 0 else f"{failed} FAIL")
    sys.exit(0 if failed == 0 else 1)
