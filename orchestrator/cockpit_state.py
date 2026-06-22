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

_state = {"active": False, "last_msg": "", "drilling": False, "dry_run": None,
          "last_activity": None, "run_started": None, "stop_event": None, "log_seq": 0,
          "approving": None}

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
