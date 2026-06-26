"""EU-63 [Ordnance BE] — ship paths are per-tab and the retired '*' casing is collapsed.

The ticket lists *ship* among the contexts that must scope to the one concrete project of the active
tab, and calls out the old ``*`` all-projects special-casing in ship (server.py:636-637) for removal.
``eu63_tab_routes_test`` proves patrol/run/board resolve per-tab; this pins the SHIP slice that test
leaves uncovered:

  - ``/api/ship-main`` ships the ACTIVE tab's one concrete project — even when app='*' is posted, it
    promotes that concrete app and never calls ``cfg.app('*')``;
  - focusing a different tab re-scopes the next ship (no global all-projects ship);
  - ``/api/ship-review`` with app='*' resolves to a concrete product (via _first_shippable / active
    tab), never the retired '*' sentinel.
"""
import sys, types, tempfile, time, threading
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

import orchestrator.server as srv
from orchestrator import sync, council
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

d = Path(tempfile.mkdtemp()); (d / "audit.jsonl").write_text("")
# automatixy is a real product repo (its path != the unit repo root); Elite-Unit IS the unit repo.
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(d / "auto"), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none"),
                   AppConfig(name="Elite-Unit", repo_path=str(d / "eu"), base_branch="dev",
                             protected_branch="main", backlog_backend="none")],
             audit_path=str(d / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"
srv.health.summary = lambda c: {"healthy": True, "checks": []}

# cfg.app('*') must never be reached — the all-projects-removal crash invariant.
_real_app = cfg.app
star_calls = []
def _guard_app(name):
    if name == "*":
        star_calls.append(name)
    return _real_app(name)
cfg.app = _guard_app

# Shipping must be enabled for the route to do anything; capture which concrete app gets promoted.
sync.can_promote = lambda: True
promoted = []
def fake_promote_app(app_cfg):
    promoted.append(getattr(app_cfg, "name", app_cfg))
    return {"ok": True, "base": "DEV", "prot": "MAIN", "ahead_before": 1}
sync.promote_app = fake_promote_app

client = srv.create_app(cfg).test_client()

def _wait(pred, n=40):
    for _ in range(n):
        if pred():
            return True
        time.sleep(0.05)
    return False

# --- 1) app-less ship targets the ACTIVE tab; '*' resolves to that concrete app, not cfg.app('*') ---
client.get("/tickets?app=automatixy")          # focus the automatixy tab for this session
srv._state["active"] = False; srv._state["shipping"] = False
client.post("/api/ship-main", data={"app": "*"})   # the retired '*' must collapse to the active tab
_wait(lambda: promoted and not srv._state.get("shipping"))
chk("ship-main with app='*' ships the active tab's concrete project", promoted == ["automatixy"], str(promoted))
chk("cfg.app('*') never called from ship", not star_calls, str(star_calls))

# --- 2) focusing another tab re-scopes the next ship (no global all-projects ship) ---
promoted.clear()
client.get("/tickets?app=Elite-Unit")
srv._state["active"] = False; srv._state["shipping"] = False
client.post("/api/ship-main")                  # NO app field -> the now-active Elite-Unit tab
_wait(lambda: promoted and not srv._state.get("shipping"))
chk("focusing another tab re-scopes the next ship (Elite-Unit)", promoted == ["Elite-Unit"], str(promoted))

# --- 3) ship-review with app='*' resolves to a concrete product, never the '*' sentinel ---
reviewed, rdone = [], threading.Event()
async def fake_ship_review(rcfg, app_name, audit=None):
    reviewed.append(app_name); rdone.set()
council.ship_review = fake_ship_review
srv._state["shipreview"] = False
client.post("/api/ship-review", data={"app": "*"})
rdone.wait(3)
chk("ship-review resolves '*' to a concrete product", reviewed and reviewed[0] in {"automatixy", "Elite-Unit"}, str(reviewed))
chk("ship-review never passes the '*' sentinel through", "*" not in reviewed)
_wait(lambda: not srv._state.get("shipreview"))

print("\n============ EU-63 SHIP-SCOPE QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
