"""EU-689: hard-stop control visible during drain."""
import sys, types, threading, tempfile
from pathlib import Path

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
app_path = d + "/automatixy"
audit_file = d + "/audit.jsonl"
cfg = Config(
    apps=[AppConfig(name="automatixy", repo_path=app_path, base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")],
    audit_path=audit_file, use_worktree=False)
cockpit_state.reset_run_state()

st = cockpit_state.get_state("automatixy")
st["active"] = True
st["autopilot_on"] = True
ev = threading.Event()
ev.set()
st["stop_event"] = ev

bar = V._control_bar(cfg, "automatixy", True)

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

# AC1: Stop button present in drain-state render
chk("Stop button (action value=stop) in drain-state HTML",
    "action value=stop" in bar and "Stop</button>" in bar)

# AC2: Posts to correct endpoint with app scope
chk("Form action is /api/autopilot (same as running-state stop)",
    "action=/api/autopilot" in bar)
chk("Hidden app field matches current app",
    'value="automatixy"' in bar)

# AC3: Stopping label still shows alongside the button
chk("'Stopping...' label present alongside button",
    "&#9203;" in bar and "Stopping" in bar)
chk("tbap stopping class on wrapper div",
    'class="tbap stopping"' in bar)

cockpit_state.reset_run_state()

print(f"\n{passed}/{checks} passed")
sys.exit(0 if passed == checks else 1)
