"""EU-204: /api/ship-main endpoint removed (hazardous auto-promote).

This test now verifies that POST /api/ship-main returns 404 (endpoint no longer exists).
The ship-review (/api/ship-review) endpoint still exists for the Release Manager workflow.
"""
import sys, types, tempfile, threading
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
from orchestrator import council
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

d = Path(tempfile.mkdtemp()); (d / "audit.jsonl").write_text("")
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(d / "auto"), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none"),
                   AppConfig(name="Elite-Unit", repo_path=str(d / "eu"), base_branch="dev",
                             protected_branch="main", backlog_backend="none")],
             audit_path=str(d / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"
srv.health.summary = lambda c: {"healthy": True, "checks": []}

client = srv.create_app(cfg).test_client()

# --- EU-204: /api/ship-main removed (hazardous auto-promote) ---
r1 = client.post("/api/ship-main")
chk("POST /api/ship-main returns 404 (endpoint removed)", r1.status_code == 404, f"got {r1.status_code}")

r2 = client.post("/api/ship-main", data={"app": "*"})
chk("POST /api/ship-main with app='*' returns 404", r2.status_code == 404, f"got {r2.status_code}")

# --- the ship-review flow still works through the merged /api/qa (2026-07-19) ---
reviewed, rdone = [], threading.Event()
async def fake_ship_review(rcfg, app_name, audit=None):
    reviewed.append(app_name); rdone.set()
council.ship_review = fake_ship_review
from orchestrator import patrol as _patrol_mod
async def _fake_patrol(c, app_name, do_file=True, audit=None):
    return "ok"
_patrol_mod.patrol = _fake_patrol
srv._state["qa"] = False
client.post("/api/qa", data={"app": "*"})
rdone.wait(3)
chk("QA still resolves '*' to a concrete product", reviewed and reviewed[0] in {"automatixy", "Elite-Unit"}, str(reviewed))
chk("QA never passes the '*' sentinel through", "*" not in reviewed)

print("\n============ EU-204 SHIP-ENDPOINT-REMOVAL QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
