"""F16 — decompose server.py (templates/routes/state).

Regression guard for the split: ``server.py`` now re-exports the cockpit run-state from
``cockpit_state`` and the inline-HTML templates from ``cockpit_views``. The whole point of the
extraction is that NOTHING changed for callers/tests — so this asserts:

  1. ``server._state`` / ``_LOG`` are the SAME mutable objects as in ``cockpit_state`` (a copy
     would silently break the live cockpit: routes mutate state the views never see).
  2. The template helpers (``_control_bar`` / ``_wrap`` …) are the same objects re-exported.
  3. Behaviour is intact end-to-end: state mutated via ``server._state`` is reflected by the
     re-exported ``_control_bar``; the ``_Tee`` stdout mirror still feeds the log ring + log_seq.
"""
import sys
import types
import tempfile
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        pass

    def __call__(s, *a, **k):
        return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import cockpit_state, cockpit_views, server, sync
from orchestrator.config import Config, AppConfig

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
sync.can_promote = lambda: False   # keep the bar off git/network — we only exercise the split

# --- 1. shared mutable objects (NOT copies) across the three modules ---
chk("server._state IS cockpit_state._state", server._state is cockpit_state._state)
chk("views._state IS cockpit_state._state", cockpit_views._state is cockpit_state._state)
chk("server._LOG IS cockpit_state._LOG", server._LOG is cockpit_state._LOG)

# --- 2. template helpers are the re-exported objects ---
chk("server._control_bar is views._control_bar", server._control_bar is cockpit_views._control_bar)
chk("server._wrap is views._wrap", server._wrap is cockpit_views._wrap)
chk("server._Tee is state._Tee", server._Tee is cockpit_state._Tee)
chk("server.recent_log is state.recent_log", server.recent_log is cockpit_state.recent_log)

# --- 3a. state mutated via server is reflected by the re-exported control bar ---
server._state["promoting"] = False
server._state["shipping"] = False
chk("idle bar has no deploy strip", "<div class=deploybar>" not in server._control_bar(cfg, "automatixy", True))
server._state["promoting"] = True
chk("promoting -> strip appears (shared state drives the view)",
    "<div class=deploybar>" in server._control_bar(cfg, "automatixy", True))
server._state["promoting"] = False

# --- 3b. the _Tee stdout mirror still feeds the log ring buffer + bumps log_seq ---
seq0 = server._state.get("log_seq", 0)
n0 = len(server.recent_log(600))
tee = server._Tee(sys.stdout)
tee.write("DECOMPOSE-MARKER builder step\n")
chk("_Tee appended the line to recent_log", "DECOMPOSE-MARKER builder step" in server.recent_log(600))
chk("_Tee bumped log_seq (wakes SSE streamers)", server._state.get("log_seq", 0) > seq0)
chk("_Tee skips noisy poll lines", (lambda: (tee.write("GET /api/board\n"),
     len(server.recent_log(600)))[1])() == len(server.recent_log(600)))

# --- 3c. a pure template helper renders + escapes as before ---
w = server._wrap("Title & <x>", "<b>inner</b>")
chk("_wrap escapes the title", "Title &amp; &lt;x&gt;" in w and "<b>inner</b>" in w)

print("\n============ F16 DECOMPOSE SERVER QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
