"""EU-74: /api/autopilot redirects must preserve ?app= so the project selector stays in sync
with the autopilot badge after Start, Stop, and drain.

Root cause: all redirect("/") calls in autopilot_api dropped the ?app= query param, so the
home route read appq=None and rendered "All projects" while the badge correctly showed the
real scope from _state["autopilot"]["app"]. Fix: redirect to /?app=<project> instead."""
import sys, types, threading, tempfile, unittest.mock
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import server, sync
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
cfg = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(tmp / "audit.jsonl"),
    use_worktree=False,
)
cfg.detected_auth = lambda: "test"
sync.can_promote = lambda: False
client = server.create_app(cfg).test_client()


# ------------------------------------------------------------------
# STOP — form carries no `app`; redirect app comes from state
# ------------------------------------------------------------------
ev1 = threading.Event()
server._state["autopilot"] = {"on": True, "stop": ev1, "app": "automatixy", "stopping": False}
r_stop = client.post("/api/autopilot", data={"action": "stop"})
loc_stop = r_stop.headers.get("Location", "")
chk("Stop redirects (302)", r_stop.status_code == 302)
chk("Stop redirect preserves ?app= from state (no app in form)", "/?app=automatixy" in loc_stop,
    f"Location={loc_stop!r}")

# ------------------------------------------------------------------
# DRAIN — form carries no `app`; redirect app comes from state
# ------------------------------------------------------------------
ev2 = threading.Event()
server._state["autopilot"] = {"on": True, "stop": ev2, "app": "automatixy", "stopping": False}
r_drain = client.post("/api/autopilot", data={"action": "drain"})
loc_drain = r_drain.headers.get("Location", "")
chk("Drain redirects (302)", r_drain.status_code == 302)
chk("Drain redirect preserves ?app= from state (no app in form)", "/?app=automatixy" in loc_drain,
    f"Location={loc_drain!r}")

# ------------------------------------------------------------------
# START while already running — form carries `app`; early return in the lock
# ------------------------------------------------------------------
ev3 = threading.Event()
server._state["autopilot"] = {"on": True, "stop": ev3, "app": "automatixy", "stopping": False}
r_dup = client.post("/api/autopilot", data={"action": "start", "app": "automatixy"})
loc_dup = r_dup.headers.get("Location", "")
chk("Duplicate-start redirects (302)", r_dup.status_code == 302)
chk("Duplicate-start redirect preserves ?app= from form", "/?app=automatixy" in loc_dup,
    f"Location={loc_dup!r}")

# ------------------------------------------------------------------
# NO project in form AND no project in state → plain / (no dangling ?app=)
# ------------------------------------------------------------------
server._state["autopilot"] = None
r_noapp = client.post("/api/autopilot", data={"action": "drain"})
loc_noapp = r_noapp.headers.get("Location", "")
# Should redirect to "/" (possibly with a trailing host prefix) but NOT end with ?app=
chk("No app → no dangling ?app= in redirect", "?app=" not in loc_noapp,
    f"Location={loc_noapp!r}")

# ------------------------------------------------------------------
# placeholder apps are not leaked into the URL
# ------------------------------------------------------------------
ev4 = threading.Event()
for placeholder in ["(no project)", "(external daemon)"]:
    server._state["autopilot"] = {"on": True, "stop": ev4, "app": placeholder, "stopping": False}
    r_ph = client.post("/api/autopilot", data={"action": "stop"})
    loc_ph = r_ph.headers.get("Location", "")
    chk(f"Placeholder '{placeholder}' not leaked to redirect URL",
        "?app=" not in loc_ph, f"Location={loc_ph!r}")

# ------------------------------------------------------------------
# SUCCESSFUL START — primary bug scenario: autopilot OFF, health OK,
# form carries app=automatixy → must redirect to /?app=automatixy.
# Before EU-74 the redirect was "/" and the selector jumped to "All
# projects" even though the badge correctly showed "automatixy".
# ------------------------------------------------------------------
_healthy = {"healthy": True, "checks": {}}
_orig_health_summary = server.health.summary
server._state["autopilot"] = None
server._state["active"] = False

# Patch health and daemon_running so Start can proceed past both guards
# without actually launching the background autopilot loop.
with (
    unittest.mock.patch.object(server.health, "summary", return_value=_healthy),
    unittest.mock.patch("orchestrator.autopilot.daemon_running", return_value=False),
    unittest.mock.patch("orchestrator.autopilot.autopilot", return_value=None),
):
    r_start = client.post("/api/autopilot", data={"action": "start", "app": "automatixy"})
loc_start = r_start.headers.get("Location", "")
# Reset state — the background thread may have set _state["autopilot"] by now
ev_start = (server._state.get("autopilot") or {}).get("stop")
if ev_start is not None:
    ev_start.set()
server._state["active"] = False
server._state["autopilot"] = None

chk("Successful-start redirects (302)", r_start.status_code == 302)
chk("Successful-start redirect preserves ?app= from form (primary EU-74 bug path)",
    "/?app=automatixy" in loc_start, f"Location={loc_start!r}")

# ------------------------------------------------------------------
# HEALTH-BLOCKED START — health not OK, form carries app; must still
# redirect to /?app= not plain /
# ------------------------------------------------------------------
_unhealthy = {"healthy": False, "checks": {"jira": "auth failed"}}
server._state["autopilot"] = None
server._state["active"] = False

with unittest.mock.patch.object(server.health, "summary", return_value=_unhealthy):
    r_unhealthy = client.post("/api/autopilot", data={"action": "start", "app": "automatixy"})
loc_unhealthy = r_unhealthy.headers.get("Location", "")
chk("Health-blocked start redirects (302)", r_unhealthy.status_code == 302)
chk("Health-blocked start preserves ?app= in redirect",
    "/?app=automatixy" in loc_unhealthy, f"Location={loc_unhealthy!r}")

# ------------------------------------------------------------------
# ACTIVE-RUN-BLOCKED START — a manual run is live, form carries app=;
# must redirect to /?app=, not plain /
# ------------------------------------------------------------------
_healthy2 = {"healthy": True, "checks": {}}
server._state["autopilot"] = None
server._state["active"] = True   # simulate a running manual job

with (
    unittest.mock.patch.object(server.health, "summary", return_value=_healthy2),
    unittest.mock.patch("orchestrator.autopilot.daemon_running", return_value=False),
):
    r_active = client.post("/api/autopilot", data={"action": "start", "app": "automatixy"})
loc_active = r_active.headers.get("Location", "")
server._state["active"] = False
chk("Active-run-blocked start redirects (302)", r_active.status_code == 302)
chk("Active-run-blocked start preserves ?app= in redirect",
    "/?app=automatixy" in loc_active, f"Location={loc_active!r}")

# ------------------------------------------------------------------
# DETACHED-DAEMON-BLOCKED START — a keepalive daemon is live (EU-73),
# form carries app=; must redirect to /?app=, not plain /
# ------------------------------------------------------------------
server._state["autopilot"] = None
server._state["active"] = False

with (
    unittest.mock.patch.object(server.health, "summary", return_value=_healthy2),
    unittest.mock.patch("orchestrator.autopilot.daemon_running", return_value=True),
):
    r_daemon = client.post("/api/autopilot", data={"action": "start", "app": "automatixy"})
loc_daemon = r_daemon.headers.get("Location", "")
chk("Detached-daemon-blocked start redirects (302)", r_daemon.status_code == 302)
chk("Detached-daemon-blocked start preserves ?app= in redirect",
    "/?app=automatixy" in loc_daemon, f"Location={loc_daemon!r}")

# Restore state cleanly
server._state["autopilot"] = None
server._state["active"] = False

print("\n============ EU-74 AUTOPILOT REDIRECT QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
