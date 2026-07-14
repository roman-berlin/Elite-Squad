"""EU-315 QA: pipeline-board visual states + Blocked-row freshness hardening.

Two acceptance criteria live in this harness:

  A. Blocked / Needs-you / Errored rows each render with a visually DISTINCT CSS class in the
     board's HTML (not just visually different — asserted by class-name string), reusing
     dashboard.pipeline_stage_tone(task, is_blocked=…) and cockpit_views._pipeline_board's per-tone
     colour.
  B. A PARKED (blocked) row whose latest event is OLDER than warroom.STALE_BLOCK_CUTOFF_S (24h)
     must NEVER render as a plain active Blocked row — it must carry a stale/greyed class instead —
     and a parked row WITHIN the cutoff renders normally as an active Blocked row.

REAL-PATH DRIVE (iteration-2 fix): the Blocked state is NOT a run outcome — no audit event ever
reconstructs to a ``blocked`` outcome. A ticket is blocked/parked when it is a MEMBER of
blocked_tickets.json (warroom._load_blocked). So this harness plants REAL audit events, writes a
REAL blocked_tickets.json, and drives BOTH through the production path — dashboard.load_tasks()
for the run rows and cockpit_views._pipeline_board()'s own self-load of the parked set from cfg —
exactly mirroring how pipeline_board_retry_test.py drives real audit events through load_tasks(),
rather than fabricating a task dict with a phantom ``outcome="blocked"``.
"""
import sys
import json
import types
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

# --- stub the Agent SDK so orchestrator modules import cleanly (mirrors pipeline_board_test.py) ---
_sdk = types.ModuleType("claude_agent_sdk")
class _Stub:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
_sdk.__getattr__ = lambda _n: _Stub
sys.modules["claude_agent_sdk"] = _sdk
_req = types.ModuleType("requests")
_req.Session = lambda: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules.setdefault("requests", _req)

sys.path.insert(0, ".")

from orchestrator import cockpit_views, dashboard
from orchestrator.warroom import STALE_BLOCK_CUTOFF_S

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail) if not cond else ""))


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _write(audit: Path, event: str, ts: str, **extra) -> None:
    with audit.open("a") as f:
        f.write(json.dumps({"event": event, "ts": ts, **extra}) + "\n")


def _row(tasks: list[dict], ticket_id: str) -> dict:
    for t in tasks:
        if str(t.get("ticket_id")) == ticket_id:
            return t
    raise AssertionError(f"{ticket_id} not found in loaded tasks")


class _Scene:
    """A temp workspace: a real audit.jsonl + a real blocked_tickets.json, driven through the
    production loaders (dashboard.load_tasks + the board's own warroom._load_blocked self-load)."""

    def __init__(self, events_writer, blocked_ids):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.audit = d / "audit.jsonl"
        self.audit.touch()
        events_writer(self.audit)
        (d / "blocked_tickets.json").write_text(json.dumps(list(blocked_ids)), encoding="utf-8")
        self.cfg = types.SimpleNamespace(audit_path=str(self.audit))
        self.blocked_ids = {str(b) for b in blocked_ids}
        self.tasks = dashboard.load_tasks(str(self.audit))

    def board(self, app_name: str) -> str:
        # Note: NO explicit blocked_set — the board self-loads it from cfg (blocked_tickets.json),
        # proving the real production wiring, not a fixture handed straight in.
        return cockpit_views._pipeline_board(self.cfg, self.tasks, app_name)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# A. Blocked / Needs-you / Errored each get a visually distinct CSS class
# ---------------------------------------------------------------------------

def test_three_stage_categories_render_distinct_classes() -> None:
    now = datetime.now()

    def _events(audit: Path) -> None:
        # Errored ticket — a builder/infra failure (ticket_exception -> outcome "errored").
        _write(audit, "ticket_start", _ts(now - timedelta(minutes=30)), ticket_id="AUTO-10",
               app="automatixy")
        _write(audit, "ticket_exception", _ts(now - timedelta(minutes=28)), ticket_id="AUTO-10",
               error="boom")
        # Needs-you ticket — escalated for a decision (needs_human -> "awaiting decision").
        _write(audit, "ticket_start", _ts(now - timedelta(minutes=30)), ticket_id="AUTO-11",
               app="automatixy")
        _write(audit, "needs_human", _ts(now - timedelta(minutes=28)), ticket_id="AUTO-11",
               question="which way?")
        # Blocked/parked ticket — same escalated outcome, but ALSO a member of blocked_tickets.json.
        _write(audit, "ticket_start", _ts(now - timedelta(minutes=30)), ticket_id="AUTO-12",
               app="automatixy")
        _write(audit, "needs_human", _ts(now - timedelta(minutes=28)), ticket_id="AUTO-12",
               question="parked")

    with _Scene(_events, blocked_ids={"AUTO-12"}) as sc:
        errored, needs_you, blocked = (_row(sc.tasks, "AUTO-10"),
                                       _row(sc.tasks, "AUTO-11"),
                                       _row(sc.tasks, "AUTO-12"))

        tone_err = dashboard.pipeline_stage_tone(errored, is_blocked=False)
        tone_ny = dashboard.pipeline_stage_tone(needs_you, is_blocked=False)
        tone_bl = dashboard.pipeline_stage_tone(blocked, is_blocked=True)

        chk("Aa. Errored gets its own tone", tone_err, tone_err)
        chk("Ab. Needs-you gets its own tone", tone_ny, tone_ny)
        chk("Ac. Blocked gets its own tone", tone_bl, tone_bl)
        chk("Ad. all three tones are pairwise DISTINCT",
            len({tone_err, tone_ny, tone_bl}) == 3, f"{tone_err}/{tone_ny}/{tone_bl}")

        board = sc.board("automatixy")
        cls_err, cls_ny, cls_bl = f"pbstage-{tone_err}", f"pbstage-{tone_ny}", f"pbstage-{tone_bl}"
        chk("Ae. Errored row's CSS class is present in the board HTML", cls_err in board, board)
        chk("Af. Needs-you row's CSS class is present in the board HTML", cls_ny in board, board)
        chk("Ag. Blocked row's CSS class (from the REAL parked set) is present", cls_bl in board,
            board)
        chk("Ah. the three class strings are pairwise DISTINCT",
            len({cls_err, cls_ny, cls_bl}) == 3, f"{cls_err}/{cls_ny}/{cls_bl}")
        # And the parked ticket reads "Blocked" even though its run outcome is "awaiting decision".
        chk("Ai. the parked ticket is labelled Blocked, not its escalated outcome",
            "Blocked" in board, board)


# ---------------------------------------------------------------------------
# B. Blocked-row freshness: within cutoff = active; past cutoff = never plain-active
# ---------------------------------------------------------------------------

def test_fresh_blocked_row_renders_as_active_blocked() -> None:
    now = datetime.now()
    ended = now - timedelta(hours=2)   # well within the 24h cutoff

    def _events(audit: Path) -> None:
        _write(audit, "ticket_start", _ts(ended - timedelta(minutes=5)), ticket_id="AUTO-20",
               app="automatixy")
        _write(audit, "needs_human", _ts(ended), ticket_id="AUTO-20", question="parked")

    with _Scene(_events, blocked_ids={"AUTO-20"}) as sc:
        t = _row(sc.tasks, "AUTO-20")
        chk("Ba. is_blocked_stale is False within the cutoff for a parked ticket",
            dashboard.is_blocked_stale(t, is_blocked=True) is False)

        board = sc.board("automatixy")
        chk("Bb. fresh blocked row shows the active 'pbstage-blocked' class",
            "pbstage-blocked" in board, board)
        chk("Bc. fresh blocked row is NOT marked stale", "pbstale" not in board, board)
        chk("Bd. fresh blocked row still reads plain 'Blocked'",
            "Blocked" in board and "(stale)" not in board, board)


def test_stale_blocked_row_never_renders_as_plain_active_blocked() -> None:
    now = datetime.now()
    ended = now - timedelta(seconds=STALE_BLOCK_CUTOFF_S + 3600)   # 1h past cutoff

    def _events(audit: Path) -> None:
        _write(audit, "ticket_start", _ts(ended - timedelta(minutes=5)), ticket_id="AUTO-21",
               app="automatixy")
        _write(audit, "needs_human", _ts(ended), ticket_id="AUTO-21", question="parked long ago")

    with _Scene(_events, blocked_ids={"AUTO-21"}) as sc:
        t = _row(sc.tasks, "AUTO-21")
        chk("Be. is_blocked_stale is True past the cutoff for a parked ticket",
            dashboard.is_blocked_stale(t, is_blocked=True) is True)

        board = sc.board("automatixy")
        chk("Bf. stale blocked row carries the stale/greyed class", "pbstale" in board, board)
        chk("Bg. stale blocked row is NEVER rendered with the plain active Blocked class",
            "pbstage-blocked" not in board, board)
        chk("Bh. the ticket itself is still visible (greyed, not silently dropped)",
            "AUTO-21" in board, board)


def test_is_blocked_stale_only_applies_to_parked_tickets() -> None:
    """A very old row that is NOT in the parked set is never flagged stale by this Blocked-specific
    check — each outcome has its own lifecycle. Drives the real path: the ticket has audit history
    but is absent from blocked_tickets.json, so the board self-loads an empty membership for it."""
    now = datetime.now()
    ancient = now - timedelta(days=30)

    def _events(audit: Path) -> None:
        _write(audit, "ticket_start", _ts(ancient - timedelta(minutes=5)), ticket_id="AUTO-22",
               app="automatixy")
        _write(audit, "ticket_exception", _ts(ancient), ticket_id="AUTO-22", error="old boom")

    with _Scene(_events, blocked_ids=set()) as sc:
        t = _row(sc.tasks, "AUTO-22")
        chk("Bi. a non-parked ticket is never flagged by is_blocked_stale",
            dashboard.is_blocked_stale(t, is_blocked=False) is False)
        board = sc.board("automatixy")
        chk("Bj. an ancient non-parked row is NOT greyed as stale on the board",
            "pbstale" not in board, board)


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

test_three_stage_categories_render_distinct_classes()
test_fresh_blocked_row_renders_as_active_blocked()
test_stale_blocked_row_never_renders_as_plain_active_blocked()
test_is_blocked_stale_only_applies_to_parked_tickets()

print("\n============= EU-315 PIPELINE BOARD — VISUAL STATES + FRESHNESS =============")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
