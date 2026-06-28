"""EU-104: ghost 'Working' card suppressed when the Jira ticket is Done/Closed.

Proves _ticket_done returns True for Done/Closed statuses and False for all others,
and that render_board forces live=False when _ticket_done indicates the ticket is done.
"""
import json
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path

sys.path.insert(0, ".")

# Stub claude_agent_sdk so warroom can import without the real SDK installed.
_sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
_sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", _sdk)

from orchestrator import warroom


# ---------------------------------------------------------------------------
# _ticket_done unit tests
# ---------------------------------------------------------------------------

def _make_cfg(backlog_backend="jira", status=None):
    """Return a minimal cfg stub whose app() returns an AppConfig with make_backlog wired up."""
    class FakeBacklog:
        def _current_status(self, key):
            return status

    class FakeApp:
        backlog_backend = "jira"
        name = "testapp"
        backlog = {}

    class FakeCfg:
        def app(self, name):
            a = FakeApp()
            a.backlog_backend = backlog_backend
            return a

    return FakeCfg()


def _make_cfg_with_bl(status):
    """cfg stub where make_backlog returns a FakeBacklog reporting ``status``."""
    import orchestrator.backlog.base as _base

    class FakeBacklog(_base.BacklogAdapter):
        def get_ready_tasks(self, limit): return []
        def get_task(self, key): raise RuntimeError("not impl")
        def set_status(self, ticket, status): pass
        def add_comment(self, ticket, body): pass
        def _current_status(self, key):
            return status

    class FakeApp:
        backlog_backend = "jira"
        name = "testapp"
        backlog = {}

    class FakeCfg:
        def app(self, name):
            return FakeApp()

    # Monkeypatch make_backlog for the duration of this test.
    orig = _base.make_backlog
    _base.make_backlog = lambda app: FakeBacklog()
    try:
        yield FakeCfg()
    finally:
        _base.make_backlog = orig


def test_ticket_done_true_for_done():
    """_ticket_done returns True when Jira reports status 'Done'."""
    # Clear the cache so the stub status is fetched fresh.
    warroom._TICKET_STATUS_CACHE.clear()
    import orchestrator.backlog.base as _base
    class FakeBL(_base.BacklogAdapter):
        def get_ready_tasks(self, l): return []
        def get_task(self, k): raise RuntimeError
        def set_status(self, t, s): pass
        def add_comment(self, t, b): pass
        def _current_status(self, key): return "Done"
    orig = _base.make_backlog
    _base.make_backlog = lambda a: FakeBL()
    try:
        cfg = _make_cfg()
        result = warroom._ticket_done(cfg, "testapp", "AUTO-99")
        assert result is True, f"expected True for 'Done', got {result!r}"
    finally:
        _base.make_backlog = orig
        warroom._TICKET_STATUS_CACHE.clear()


def test_ticket_done_true_for_closed():
    """_ticket_done returns True when Jira reports status 'Closed'."""
    warroom._TICKET_STATUS_CACHE.clear()
    import orchestrator.backlog.base as _base
    class FakeBL(_base.BacklogAdapter):
        def get_ready_tasks(self, l): return []
        def get_task(self, k): raise RuntimeError
        def set_status(self, t, s): pass
        def add_comment(self, t, b): pass
        def _current_status(self, key): return "Closed"
    orig = _base.make_backlog
    _base.make_backlog = lambda a: FakeBL()
    try:
        result = warroom._ticket_done(_make_cfg(), "testapp", "AUTO-99")
        assert result is True, f"expected True for 'Closed', got {result!r}"
    finally:
        _base.make_backlog = orig
        warroom._TICKET_STATUS_CACHE.clear()


def test_ticket_done_false_for_in_progress():
    """_ticket_done returns False when Jira reports status 'In Progress'."""
    warroom._TICKET_STATUS_CACHE.clear()
    import orchestrator.backlog.base as _base
    class FakeBL(_base.BacklogAdapter):
        def get_ready_tasks(self, l): return []
        def get_task(self, k): raise RuntimeError
        def set_status(self, t, s): pass
        def add_comment(self, t, b): pass
        def _current_status(self, key): return "In Progress"
    orig = _base.make_backlog
    _base.make_backlog = lambda a: FakeBL()
    try:
        result = warroom._ticket_done(_make_cfg(), "testapp", "AUTO-99")
        assert result is False, f"expected False for 'In Progress', got {result!r}"
    finally:
        _base.make_backlog = orig
        warroom._TICKET_STATUS_CACHE.clear()


def test_ticket_done_false_for_blank_ticket():
    """_ticket_done returns False immediately for empty / dash ticket keys."""
    warroom._TICKET_STATUS_CACHE.clear()
    cfg = _make_cfg()
    assert warroom._ticket_done(cfg, "testapp", "") is False
    assert warroom._ticket_done(cfg, "testapp", "—") is False
    assert warroom._ticket_done(cfg, "testapp", None) is False


def test_ticket_done_false_for_blank_app():
    """_ticket_done returns False immediately when no app is provided."""
    warroom._TICKET_STATUS_CACHE.clear()
    cfg = _make_cfg()
    assert warroom._ticket_done(cfg, None, "AUTO-99") is False
    assert warroom._ticket_done(cfg, "", "AUTO-99") is False


def test_ticket_done_false_on_jira_error():
    """_ticket_done returns False (never raises) when the Jira call throws."""
    warroom._TICKET_STATUS_CACHE.clear()
    import orchestrator.backlog.base as _base
    class FakeBL(_base.BacklogAdapter):
        def get_ready_tasks(self, l): return []
        def get_task(self, k): raise RuntimeError
        def set_status(self, t, s): pass
        def add_comment(self, t, b): pass
        def _current_status(self, key): raise ConnectionError("jira unreachable")
    orig = _base.make_backlog
    _base.make_backlog = lambda a: FakeBL()
    try:
        result = warroom._ticket_done(_make_cfg(), "testapp", "AUTO-99")
        assert result is False, "must return False, not raise, on Jira error"
    finally:
        _base.make_backlog = orig
        warroom._TICKET_STATUS_CACHE.clear()


def test_ticket_done_false_for_non_jira_backend():
    """_ticket_done returns False for apps with no Jira backend (none/notion)."""
    warroom._TICKET_STATUS_CACHE.clear()
    cfg = _make_cfg(backlog_backend="none")
    result = warroom._ticket_done(cfg, "testapp", "AUTO-99")
    assert result is False, "non-jira backend must return False without hitting any adapter"
    warroom._TICKET_STATUS_CACHE.clear()


# ---------------------------------------------------------------------------
# render_board guard: live card suppressed when ticket is Done
# ---------------------------------------------------------------------------

def test_render_board_suppresses_live_card_for_done_ticket():
    """When _ticket_done returns True render_board must render the idle 'last run' card, not
    the pulsing 'Working · Build' hero (EU-104 ghost-run guard)."""
    # Build a minimal audit with one in-flight ticket (no terminal outcome).
    d = Path(tempfile.mkdtemp())
    audit = d / "audit.jsonl"
    now = datetime.now().astimezone()
    ts = now.strftime("%Y-%m-%dT%H:%M:%S%z")
    rows = [
        dict(event="ticket_start", ticket_id="AUTO-77", app="myapp", branch="auto/AUTO-77", ts=ts),
        dict(event="build", ticket_id="AUTO-77", app="myapp", iteration=1, turns=3,
             cost_usd=0.0, effort="low", tools=["Read"], summary="wip", ts=ts),
    ]
    audit.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    ns = types.SimpleNamespace
    cfg = ns(
        audit_path=str(audit),
        apps=[ns(name="myapp", backlog_backend="jira", backlog={})],
    )
    # Give cfg an app() method that returns the first matching app.
    def _app(name):
        for a in cfg.apps:
            if a.name == name:
                return a
        raise KeyError(name)
    cfg.app = _app

    # Patch _ticket_done to return True (ticket is Done) so we don't need a real Jira.
    orig_td = warroom._ticket_done
    warroom._ticket_done = lambda *a, **k: True
    try:
        # active=True would normally trigger the live / Working rendering.
        board = warroom.render_board(cfg, "myapp", {"active": True})
        # The live hero header says "Working" — it must NOT appear.
        assert "Working" not in board, (
            "render_board must suppress 'Working' when the ticket is Done/Closed in Jira (EU-104)"
        )
        # 'last run' badge must appear instead.
        assert "last run" in board, (
            "render_board must show 'last run' idle card when ticket is Done/Closed in Jira (EU-104)"
        )
    finally:
        warroom._ticket_done = orig_td


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all eu104_ghost_run_guard tests passed")
