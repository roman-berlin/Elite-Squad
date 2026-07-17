"""EU-313: the "Security blocks" KPI card must not surface a stale block as if it were live.

`warroom.kpis()` used to count EVERY `security_block` audit event ever recorded (all-time) —
so a block from 12 days ago (e.g. EU-116) still showed up as an active count on the dashboard.
Covers:
  - `_active_security_blocks()`: drops blocks older than STALE_BLOCK_CUTOFF_S, drops blocks
    superseded by a later event for the same ticket_id, keeps fresh unsuperseded blocks.
  - `kpis()`: the "Security blocks" card's `value` reflects only active blocks, `tone` is "bad"
    only when the active count is non-zero, and the rendered card surfaces the age of the most
    recent active block (contains "ago").
  - The "Merged → DEV today" and "Tokens" cards are unaffected by this change.
"""
import sys
import json
import types
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Minimal stubs so warroom can be imported without the full dependency tree
# (same pattern as tests/eu76_kpi_sparklines_test.py).
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


def _write_audit_ev(audit: Path, event: str, ts: str, **extra) -> None:
    row = {"event": event, "ts": ts, **extra}
    with audit.open("a") as f:
        f.write(json.dumps(row) + "\n")


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Tests: _active_security_blocks()

def test_stale_block_excluded_by_age():
    """A security_block older than the freshness cutoff (default 24h), with no follow-up event
    for its ticket, is NOT active — even though it's still in the all-time list."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        audit = Path(cfg.audit_path)
        old_ts = _ts(datetime.now() - timedelta(days=12))   # EU-116-style 12-day-old block
        _write_audit_ev(audit, "security_block", old_ts, ticket_id="EU-116", reason="stale finding")
        blocks = warroom._load_security_blocks(cfg)
        active = warroom._active_security_blocks(cfg, blocks)
    assert len(blocks) == 1, "the block must still appear in the all-time list"
    assert active == [], f"a 12-day-old block with no follow-up must NOT be active, got {active}"


def test_fresh_block_included():
    """A security_block within the 24h cutoff, with no superseding event, IS active."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        audit = Path(cfg.audit_path)
        fresh_ts = _ts(datetime.now() - timedelta(hours=3))
        _write_audit_ev(audit, "security_block", fresh_ts, ticket_id="EU-313", reason="fresh finding")
        blocks = warroom._load_security_blocks(cfg)
        active = warroom._active_security_blocks(cfg, blocks)
    assert len(active) == 1, f"a fresh (3h-old) block must be active, got {active}"
    assert active[0]["ticket_id"] == "EU-313"


def test_block_superseded_by_later_event_excluded():
    """A security_block within the freshness window but followed by a LATER event for the same
    ticket_id (e.g. a fresh build re-attempt) is superseded — no longer counted as active."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        audit = Path(cfg.audit_path)
        block_ts = _ts(datetime.now() - timedelta(hours=5))
        later_ts = _ts(datetime.now() - timedelta(hours=1))
        _write_audit_ev(audit, "security_block", block_ts, ticket_id="EU-313", reason="old finding")
        _write_audit_ev(audit, "build", later_ts, ticket_id="EU-313")   # supersedes the block above
        blocks = warroom._load_security_blocks(cfg)
        active = warroom._active_security_blocks(cfg, blocks)
    assert active == [], f"a block superseded by a later same-ticket event must be excluded, got {active}"


def test_block_for_different_ticket_not_superseded():
    """A later event for a DIFFERENT ticket_id must not affect this block's active status."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        audit = Path(cfg.audit_path)
        block_ts = _ts(datetime.now() - timedelta(hours=5))
        later_ts = _ts(datetime.now() - timedelta(hours=1))
        _write_audit_ev(audit, "security_block", block_ts, ticket_id="EU-313", reason="finding")
        _write_audit_ev(audit, "build", later_ts, ticket_id="EU-999")   # unrelated ticket
        blocks = warroom._load_security_blocks(cfg)
        active = warroom._active_security_blocks(cfg, blocks)
    assert len(active) == 1, f"an unrelated ticket's later event must not supersede this block, got {active}"


# ---------------------------------------------------------------------------
# Tests: kpis() integration — the "Security blocks" card itself

def test_kpis_stale_block_renders_zero():
    """AC: a 12-day-old security_block with no follow-up -> card value 0, tone not 'bad'."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        audit = Path(cfg.audit_path)
        old_ts = _ts(datetime.now() - timedelta(days=12))
        _write_audit_ev(audit, "security_block", old_ts, ticket_id="EU-116", reason="stale finding")
        cards = warroom.kpis(cfg, [], None)
    card = next(c for c in cards if c["label"] == "Security blocks")
    assert card["value"] == 0, f"stale block must not count, got value={card['value']}"
    assert card.get("tone") != "bad", f"a zero active-block card must not be toned 'bad', got {card.get('tone')}"


def test_kpis_fresh_block_renders_nonzero_with_age():
    """AC: a fresh block within 24h -> card value >= 1 and the rendered card surfaces its age
    (contains 'ago', via _rel)."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        audit = Path(cfg.audit_path)
        fresh_ts = _ts(datetime.now() - timedelta(hours=3))
        _write_audit_ev(audit, "security_block", fresh_ts, ticket_id="EU-313", reason="fresh finding")
        cards = warroom.kpis(cfg, [], None)
    card = next(c for c in cards if c["label"] == "Security blocks")
    assert card["value"] >= 1, f"a fresh block must count, got value={card['value']}"
    assert card.get("tone") == "bad", f"a non-zero active-block card must be toned 'bad', got {card.get('tone')}"
    html = warroom._kpi_html([card])
    assert "ago" in html, f"rendered card must surface the active block's age, got: {html}"


def test_dashboard_stale_block_test_zero_and_nonzero_cases():
    """The ticket's named regression test: plants a 12-day-old block with no follow-up (must
    render zero/no-active-blocks) AND a fresh block for a different ticket (must render non-zero),
    in the SAME kpis() call — proving the two cases don't interfere."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        audit = Path(cfg.audit_path)
        old_ts = _ts(datetime.now() - timedelta(days=12))
        fresh_ts = _ts(datetime.now() - timedelta(hours=1))
        _write_audit_ev(audit, "security_block", old_ts, ticket_id="EU-116", reason="stale finding")
        _write_audit_ev(audit, "security_block", fresh_ts, ticket_id="EU-313", reason="fresh finding")
        cards = warroom.kpis(cfg, [], None)
    card = next(c for c in cards if c["label"] == "Security blocks")
    assert card["value"] == 1, f"only the fresh block should count, got value={card['value']}"
    html = warroom._kpi_html([card])
    assert "ago" in html


# ---------------------------------------------------------------------------
# Tests: EU-290 — the interactive findings list must match the headline count.
#
# EU-313 filtered only the COUNT: kpis() still passed the all-time `sec_blocks` as
# "security_block_findings", so the card read "0" while rendering the 17-day-old EU-116 block
# expanded-by-default with a live "Record response" box posting to /api/security-reply. The
# producing gate was DELETED in the Phase-2 collapse (provost.py: security_gate is gone, commit
# c24f459), so that block can never be actionable — it was a permanent false "needs-you" signal.

def test_stale_block_yields_empty_findings_list():
    """EU-290: a stale block must drop out of the findings list too, not just the count —
    otherwise the card renders a retired subsystem's history as a current, actionable item."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        audit = Path(cfg.audit_path)
        old_ts = _ts(datetime.now() - timedelta(days=17))   # the real EU-116 block's age
        _write_audit_ev(audit, "security_block", old_ts, ticket_id="EU-116",
                        reason="Claude Code returned an error result: success; failing closed")
        cards = warroom.kpis(cfg, [], None)
    card = next(c for c in cards if c["label"] == "Security blocks")
    assert card["value"] == 0, f"stale block must not count, got value={card['value']}"
    assert card.get("security_block_findings") == [], (
        "a stale block must not be passed to the interactive findings list — the headline count "
        f"and the list must agree; got {card.get('security_block_findings')}")


def test_stale_block_card_renders_no_actionable_response_form():
    """EU-290 AC: no dashboard KPI presents an all-time historical event from a retired
    subsystem as current/actionable. With only a stale block on file the card must render the
    empty branch — no EU-116 row, no /api/security-reply box that cannot unblock anything."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        audit = Path(cfg.audit_path)
        old_ts = _ts(datetime.now() - timedelta(days=17))
        _write_audit_ev(audit, "security_block", old_ts, ticket_id="EU-116", reason="stale finding")
        cards = warroom.kpis(cfg, [], None)
    card = next(c for c in cards if c["label"] == "Security blocks")
    html_out = warroom._kpi_html([card])
    assert "EU-116" not in html_out, f"the stale block is still rendered on the card: {html_out}"
    assert "/api/security-reply" not in html_out, (
        "a response form is still offered for a block that cannot be unblocked (the gate that "
        f"produced it is deleted): {html_out}")
    assert "secempty" in html_out, f"expected the empty branch to render, got: {html_out}"


def test_zero_count_card_is_not_expanded_by_default():
    """EU-290: an empty Security-blocks card must not sit permanently expanded — an always-open
    <details> around 'No security blocks recorded yet.' is exactly the fixed false "needs-you"
    signal the ticket is about. It stays open when there IS something to act on."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        audit = Path(cfg.audit_path)
        _write_audit_ev(audit, "security_block", _ts(datetime.now() - timedelta(days=17)),
                        ticket_id="EU-116", reason="stale finding")
        empty_html = warroom._kpi_html(
            [next(c for c in warroom.kpis(cfg, [], None) if c["label"] == "Security blocks")])
    assert "<details class=\"kpi \" open>" not in empty_html and " open>" not in empty_html, (
        f"the zero-count card is still expanded by default: {empty_html}")

    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        audit = Path(cfg.audit_path)
        _write_audit_ev(audit, "security_block", _ts(datetime.now() - timedelta(hours=2)),
                        ticket_id="EU-313", reason="fresh finding")
        live_html = warroom._kpi_html(
            [next(c for c in warroom.kpis(cfg, [], None) if c["label"] == "Security blocks")])
    assert " open>" in live_html, (
        f"a card with a LIVE block must still be expanded so the finding is visible: {live_html}")
    assert "/api/security-reply" in live_html, (
        f"a live block must keep its actionable response form: {live_html}")


# ---------------------------------------------------------------------------
# Tests: unrelated KPI cards are untouched

def test_merged_and_tokens_cards_unchanged():
    """AC: 'Merged → DEV today' and 'Tokens' cards are unaffected by the security-blocks fix."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _make_cfg(Path(d))
        cards_before_labels = {c["label"]: {"value": c["value"], "hint": c.get("hint"), "href": c.get("href")}
                                for c in warroom.kpis(cfg, [], None)}
        audit = Path(cfg.audit_path)
        old_ts = _ts(datetime.now() - timedelta(days=12))
        _write_audit_ev(audit, "security_block", old_ts, ticket_id="EU-116", reason="stale finding")
        cards_after_labels = {c["label"]: {"value": c["value"], "hint": c.get("hint"), "href": c.get("href")}
                               for c in warroom.kpis(cfg, [], None)}
    for label in ("Merged → DEV today", "Tokens"):
        assert cards_before_labels[label] == cards_after_labels[label], (
            f"{label} card must be byte-for-byte unchanged; "
            f"before={cards_before_labels[label]} after={cards_after_labels[label]}")


# ---------------------------------------------------------------------------
# Runner (same pattern as eu76_kpi_sparklines_test.py)

if __name__ == "__main__":
    import traceback

    tests = [
        test_stale_block_excluded_by_age,
        test_fresh_block_included,
        test_block_superseded_by_later_event_excluded,
        test_block_for_different_ticket_not_superseded,
        test_kpis_stale_block_renders_zero,
        test_kpis_fresh_block_renders_nonzero_with_age,
        test_dashboard_stale_block_test_zero_and_nonzero_cases,
        test_stale_block_yields_empty_findings_list,
        test_stale_block_card_renders_no_actionable_response_form,
        test_zero_count_card_is_not_expanded_by_default,
        test_merged_and_tokens_cards_unchanged,
    ]
    passed = failed = 0
    print("\n========== EU-313 STALE SECURITY-BLOCK KPI TESTS ==========")
    for fn in tests:
        try:
            fn()
            print(f"  [PASS] {fn.__name__}")
            passed += 1
        except Exception:
            print(f"  [FAIL] {fn.__name__}")
            traceback.print_exc()
            failed += 1
    print("-------------------------------------------------------------")
    print(f"  {passed}/{passed + failed} passed")
    print("  RESULT:", "ALL GREEN" if failed == 0 else f"{failed} FAIL")
    sys.exit(0 if failed == 0 else 1)
