"""Cockpit shared run-state + the stdout→Live-feed plumbing.

Split out of ``server.py`` (F16: decompose templates/routes/state) so the global
run-state and the log ring buffer live in one small module that both the routes
(``server.py``) and the views (``cockpit_views.py``) import. Behaviour is unchanged:
``server`` re-exports these names, so ``server._state`` / ``server._Tee`` / ``server.recent_log``
stay valid for callers and tests, and every importer shares the SAME mutable objects.
"""
from __future__ import annotations

import collections as _collections
import threading
import time
from dataclasses import asdict, dataclass, field

_state = {"active": False, "last_msg": "", "last_result": "", "drilling": False, "dry_run": None,
          "last_activity": None, "run_started": None, "stop_event": None, "log_seq": 0,
          "approving": None}
# ``last_msg``  : sticky control-bar note (run/standup/drill state); cleared on /memory & /needs.
# ``last_result``: one-shot read-and-clear result banner for the side-effectful / actions
#                  (ship / promote / patrol) — set by their _bg, shown once on /, then cleared.

# Guards the active check-then-set so two near-simultaneous run POSTs can't both pass the
# `_state["active"]` guard and start two runs (TOCTOU race). Acquire it whenever you claim a run.
_run_lock = threading.Lock()

# Ring buffer of the unit's stdout — fed to the War Room's "Live feed" panel so you can watch
# the implementation steps in the dashboard, not just the terminal.
_LOG: "_collections.deque[str]" = _collections.deque(maxlen=600)


def recent_log(n: int = 60) -> list[str]:
    return list(_LOG)[-n:]


def _sse(event: str, data: str) -> str:
    """Format one Server-Sent Event. Multi-line `data` is split into the required `data:` lines."""
    body = "".join("data: " + ln + "\n" for ln in data.replace("\r", "").split("\n"))
    return f"event: {event}\n{body}\n"


class _Tee:
    """Mirror stdout to the real terminal AND the ring buffer (skips the noisy poll line)."""
    def __init__(self, real):
        self._real = real

    def write(self, s: str):
        self._real.write(s)
        for line in s.splitlines():
            t = line.rstrip()
            if t and "/api/board" not in t and "GET /api/" not in t:
                _LOG.append(t)
                _state["last_activity"] = time.time()   # heartbeat — proves the unit is alive
                _state["log_seq"] = _state.get("log_seq", 0) + 1   # wake SSE streamers (real-time push)

    def flush(self):
        self._real.flush()

    def isatty(self):
        return getattr(self._real, "isatty", lambda: False)()


# --------------------------------------------------------------------------------------------------
# EU-63 — per-tab cockpit workspace (one-project-per-tab; "All projects"/* is gone).
#
# The cockpit used to carry a SINGLE selected project (``?app=…``) with a literal ``"*"`` sentinel
# meaning "All projects". That single-context assumption is replaced here by an explicit *workspace*:
# an ordered set of TABS, each pinned to exactly ONE concrete project, each holding its own view
# state (board / ticket-picker selection / runs / needs-you). The routes (server.py) and the views
# (cockpit_views.py) build on this model. The "*"/all-projects view no longer exists, so ``"*"`` is
# rejected as a project name here rather than special-cased downstream.
#
# Persistence: workspaces live in a server-side, per-session store (``_workspaces``, guarded by
# ``_workspace_lock``) so the open tabs survive a page reload within a session. ``to_dict`` /
# ``from_dict`` give the routes a JSON-able shape they can ALSO hand to the browser for a
# localStorage round-trip (rehydrate-on-load) — see ``rehydrate_workspace``.
# --------------------------------------------------------------------------------------------------

ALL_PROJECTS_SENTINEL = "*"   # the retired "All projects" selector — never a valid tab project.


@dataclass
class Tab:
    """One cockpit tab, pinned to a single concrete project, carrying that tab's view state.

    A tab is the unit of the tabbed workspace: it never represents "all projects". ``project`` is a
    real app name (``config.yaml``); the remaining fields are the per-tab view state the cockpit
    panels render — kept here (not in module globals) so each open project keeps its own selection,
    board layout, run list and needs-you queue independently of the others.
    """
    project: str
    ticket: str | None = None          # ticket-picker selection (the chosen ticket key, or None)
    board: dict = field(default_factory=dict)       # per-tab board view state (columns/filters)
    runs: list = field(default_factory=list)        # run ids/state surfaced in this tab
    needs_you: list = field(default_factory=list)   # pending "needs-you" decision ids for this tab

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Tab":
        """Build a Tab from an untrusted dict (localStorage / session store), dropping unknown keys
        and coercing the containers so a malformed payload can't crash the cockpit."""
        project = str(data.get("project", "")).strip()
        if not project or project == ALL_PROJECTS_SENTINEL:
            raise ValueError(f"a tab must be pinned to a concrete project, not {project!r}")
        ticket = data.get("ticket")
        return cls(
            project=project,
            ticket=str(ticket) if ticket else None,
            board=dict(data.get("board") or {}),
            runs=list(data.get("runs") or []),
            needs_you=list(data.get("needs_you") or []),
        )


@dataclass
class Workspace:
    """An ordered set of tabs plus which one is active — one cockpit session's open projects.

    Mutual exclusion is the core invariant: a project may be open in AT MOST one tab. ``add_tab``
    focuses the existing tab instead of opening a duplicate, so the tab list is always a set of
    distinct projects in open order.

    Thread-safety: ``_lock`` (one per ``Workspace`` instance) guards concurrent tab mutations
    (``add_tab``, ``remove_tab``, ``set_active``). The module-level ``_workspace_lock`` guards only
    the ``_workspaces`` dict (the lazy-create step in ``workspace_for``); these two locks are
    independent and never held at the same time.
    """
    tabs: list[Tab] = field(default_factory=list)
    active: str | None = None          # the active tab's project (None when no tabs are open)
    _lock: threading.Lock = field(default_factory=threading.Lock, compare=False, repr=False)

    # -- queries -----------------------------------------------------------------------------------
    def projects(self) -> list[str]:
        """The open projects, in tab order."""
        return [t.project for t in self.tabs]

    def get_tab(self, project: str) -> Tab | None:
        """The tab pinned to ``project``, or None if that project isn't open."""
        for t in self.tabs:
            if t.project == project:
                return t
        return None

    def active_tab(self) -> Tab | None:
        """The currently active tab, or None when the workspace is empty."""
        return self.get_tab(self.active) if self.active else None

    # -- mutations ---------------------------------------------------------------------------------
    def add_tab(self, project: str, *, activate: bool = True) -> Tab:
        """Open ``project`` in a tab (or focus its existing tab — mutual exclusion).

        Rejects the retired "All projects" sentinel and empty names. Returns the tab either way, so
        callers get the same object whether the project was already open or freshly added.
        """
        with self._lock:
            project = (project or "").strip()
            if not project or project == ALL_PROJECTS_SENTINEL:
                raise ValueError(f"cannot open a tab for {project!r}: tabs pin to one concrete project")
            existing = self.get_tab(project)
            if existing is not None:
                if activate:
                    self.active = project
                return existing
            tab = Tab(project=project)
            self.tabs.append(tab)
            if activate or self.active is None:
                self.active = project
            return tab

    def remove_tab(self, project: str) -> bool:
        """Close ``project``'s tab. Returns True if a tab was removed.

        If the closed tab was active, the active tab falls back to the neighbour to its left (or the
        new first tab), matching the usual editor "close tab" behaviour; the workspace goes empty
        (``active is None``) only when the last tab is closed.
        """
        with self._lock:
            idx = next((i for i, t in enumerate(self.tabs) if t.project == project), None)
            if idx is None:
                return False
            was_active = self.tabs[idx].project == self.active
            self.tabs.pop(idx)
            if was_active:
                if not self.tabs:
                    self.active = None
                else:
                    self.active = self.tabs[max(0, idx - 1)].project
            return True

    def set_active(self, project: str) -> bool:
        """Focus an already-open tab. Returns False (no change) if the project isn't open."""
        with self._lock:
            if self.get_tab(project) is None:
                return False
            self.active = project
            return True

    # -- persistence -------------------------------------------------------------------------------
    def to_dict(self) -> dict:
        """JSON-able snapshot for the session store AND the browser localStorage round-trip."""
        return {"tabs": [t.to_dict() for t in self.tabs], "active": self.active}

    @classmethod
    def from_dict(cls, data: dict | None) -> "Workspace":
        """Rebuild a workspace from an untrusted snapshot, enforcing mutual exclusion.

        Malformed tabs are skipped rather than fatal; duplicate projects collapse to the first
        occurrence; ``active`` is clamped to a project that actually survived (else the first tab).
        """
        ws = cls()
        seen: set[str] = set()
        for raw in (data or {}).get("tabs") or []:
            try:
                tab = Tab.from_dict(raw)
            except (ValueError, TypeError, AttributeError):
                continue
            if tab.project in seen:
                continue
            seen.add(tab.project)
            ws.tabs.append(tab)
        active = (data or {}).get("active")
        ws.active = active if active in seen else (ws.tabs[0].project if ws.tabs else None)
        return ws


# Server-side per-session workspace store. Keyed by an opaque session id supplied by the routes
# (cookie / header). Guarded so concurrent requests for the same session can't corrupt the tab list.
_workspaces: "dict[str, Workspace]" = {}
_workspace_lock = threading.Lock()


def workspace_for(session_id: str) -> Workspace:
    """The (lazily created) server-side workspace for ``session_id``. Same object on every call, so
    routes mutate the live tab set in place; persists for the process lifetime of the session.

    Thread-safety: ``_workspace_lock`` guards only this dict (the lazy-create step below); each
    ``Workspace`` carries its own ``_lock`` for concurrent tab mutations (``add_tab``,
    ``remove_tab``, ``set_active``). The two locks are independent and never nested."""
    with _workspace_lock:
        ws = _workspaces.get(session_id)
        if ws is None:
            ws = Workspace()
            _workspaces[session_id] = ws
        return ws


def rehydrate_workspace(session_id: str, data: dict | None) -> Workspace:
    """Replace ``session_id``'s server-side workspace from a client snapshot (localStorage rehydrate
    on load) and return it. Lets the browser restore the open tabs after a reload that started a
    fresh server session; the rebuilt workspace re-enforces mutual exclusion via ``from_dict``."""
    ws = Workspace.from_dict(data)
    with _workspace_lock:
        _workspaces[session_id] = ws
    return ws


def reset_workspaces() -> None:
    """Drop every stored workspace — test seam / session-clear hook."""
    with _workspace_lock:
        _workspaces.clear()
