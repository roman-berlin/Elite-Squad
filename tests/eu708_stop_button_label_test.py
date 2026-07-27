"""EU-708: the toolbar autopilot Stop button's TRUE effect must be visible.

The toolbar "Stop" button only marks autopilot off — any in-flight build keeps
running. Before EU-708 that consequence was admitted only in a `title=` tooltip
(most users never read it) while the bare "Stop" label implied a hard stop.

AC1  The button's visible label names its real effect ('Stop autopilot') — it
     no longer implies it stops in-flight work — in BOTH renders that carry it
     (autopilot ON cluster and the mid-drain 'Stopping…' cluster).
AC2  The 'in-flight builds continue running' consequence is VISIBLE on-page
     text next to the control (element content, not solely a title= tooltip).
AC3  Presentation-only: the button's wiring is unchanged — it still posts
     action=stop to /api/autopilot (behavior is exercised by eu103/eu356;
     here we pin the rendered form contract), and the EU-709 hard-stop
     control still renders alongside during a drain.
"""
import sys, types, threading, tempfile
from unittest.mock import patch

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

sys.path.insert(0, ".")

from orchestrator import cockpit_views as V, cockpit_state
from orchestrator.config import AppConfig, Config

d = tempfile.mkdtemp()
cfg = Config(
    apps=[AppConfig(name="automatixy", repo_path=d + "/automatixy", base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")],
    audit_path=d + "/audit.jsonl", use_worktree=False)

checks = 0
passed = 0

def chk(name, cond):
    global checks, passed
    checks += 1
    if cond:
        passed += 1
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name}")

NOTE = ">In-flight builds continue running</span>"   # element CONTENT, not a title= value

# ── Autopilot ON render ───────────────────────────────────────────────────────
cockpit_state.reset_run_state()
st = cockpit_state.get_state("automatixy")
st["active"] = True
st["autopilot_on"] = True
st["stop_event"] = threading.Event()   # issued: NO — not set → ON, not draining
# Hermetic: a real launchd daemon on this box must not flip the render (eu103 pattern).
with patch("orchestrator.autopilot.daemon_running", return_value=False):
    bar_on = V._control_bar(cfg, "automatixy", True)

# AC1: label names the real effect; the bare 'Stop' label is gone.
chk("AC1 on: button labeled 'Stop autopilot'",
    "Stop&nbsp;autopilot</button>" in bar_on)
chk("AC1 on: no bare 'Stop' button label remains",
    ">Stop</button>" not in bar_on)
# AC2: consequence is visible page text (span content), not tooltip-only.
chk("AC2 on: 'In-flight builds continue running' rendered as visible text",
    NOTE in bar_on)
# AC3: wiring unchanged — still action=stop to /api/autopilot, app-scoped.
chk("AC3 on: stop form still posts action=stop to /api/autopilot",
    "action=/api/autopilot" in bar_on and "action value=stop" in bar_on)
chk("AC3 on: stop form still scoped to the current app",
    'name=app value="automatixy"' in bar_on)

# ── Mid-drain ('Stopping…') render — the same button, second surface ─────────
cockpit_state.reset_run_state()
st = cockpit_state.get_state("automatixy")
st["active"] = True
st["autopilot_on"] = True
ev = threading.Event()
ev.set()                               # stop issued → draining
st["stop_event"] = ev
with patch("orchestrator.autopilot.daemon_running", return_value=False):
    bar_dr = V._control_bar(cfg, "automatixy", True)

chk("AC1 drain: button labeled 'Stop autopilot'",
    "Stop&nbsp;autopilot</button>" in bar_dr)
chk("AC1 drain: no bare 'Stop' button label remains",
    ">Stop</button>" not in bar_dr)
chk("AC2 drain: 'In-flight builds continue running' rendered as visible text",
    NOTE in bar_dr)
chk("AC3 drain: stop form still posts action=stop to /api/autopilot",
    "action=/api/autopilot" in bar_dr and "action value=stop" in bar_dr)
# EU-709 regression guard: the HARD-stop control still sits alongside.
chk("AC3 drain: EU-709 hard-stop control still rendered alongside",
    "action=/api/stop-run" in bar_dr)

# ── Autopilot OFF render: no Stop button → no note ───────────────────────────
cockpit_state.reset_run_state()
with patch("orchestrator.autopilot.daemon_running", return_value=False):
    bar_off = V._control_bar(cfg, "automatixy", True)
chk("scope: note absent when there is no Stop button (autopilot off)",
    "In-flight builds continue running" not in bar_off)

cockpit_state.reset_run_state()

print(f"\n{passed}/{checks} passed")
sys.exit(0 if passed == checks else 1)
