"""/api/run defaults to LIVE (build + merge to DEV); dry-run is opt-in; the dry flag clears after a run.

EU-289 removed the "+ New task" toolbar panel that used to drive this route (intake is Jira-only);
the route itself stays for scripted use, which is what this harness exercises directly.
"""
import sys, types, tempfile, threading, time
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.server as srv
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

d = Path(tempfile.mkdtemp()); (d / "audit.jsonl").write_text("")
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(d / "audit.jsonl"), use_worktree=False)

# --- stubs: healthy, trivial worklist, capture the dry_run the run was launched with ---
captured, done = {}, threading.Event()
async def fake_run_loop(rcfg, worklist, audit, stop_event=None):
    captured["dry_run"] = rcfg.dry_run
    done.set()
srv.health.summary = lambda c: {"healthy": True, "checks": []}
srv.intake.from_text = lambda *a, **k: ["wl"]
srv.run_loop = fake_run_loop

app = srv.create_app(cfg)
c = app.test_client()

# EU-64: the run is now per-project — its active/dry_run/run_started live on the app's OWN state
# (``get_state("automatixy")``), not the unit-wide ``_state``. Wait on (and assert) that key.
ST = srv.get_state("automatixy")

def wait_idle():
    for _ in range(40):
        if not ST["active"]:
            return
        time.sleep(0.05)

# 1) the control bar carries no ad-hoc run form at all.
# CONTRACT CHANGE (EU-289, 2026-07-17): this used to assert `name=dryrun` IS in the bar — the
# opt-in checkbox on the "+ New task" panel's POST /api/run form. Roman had that panel removed
# ("remove New task — we use only Jira"), so the whole form is gone and the old assertion pinned
# deleted markup. Inverted into a positive pin of the new contract; the dry-run DEFAULT behaviour
# it was really protecting is unaffected and still covered by the /api/run checks below (default
# submit → LIVE, dryrun=on → DRY), which drive the endpoint directly rather than via the markup.
bar = srv._control_bar(cfg, "automatixy", True)
chk("control bar no longer ships the ad-hoc run form (EU-289: intake is Jira-only)",
    "name=dryrun" not in bar and "/api/run" not in bar, bar[:160])
chk("control bar dropped the 'live' checkbox", "name=live" not in bar)

# 2) default submit (no dryrun) -> LIVE
done.clear(); captured.clear()
c.post("/api/run", data={"kind": "task", "text": "do x"})
done.wait(3)
chk("default run is LIVE (dry_run is False)", captured.get("dry_run") is False, str(captured))

# 3) after the run finishes, the dry/live flag is cleared (no stale tag)
wait_idle(); time.sleep(0.1)
chk("dry_run reset to None after the run ends", ST["dry_run"] is None, str(ST.get("dry_run")))
chk("run_started reset to None after the run ends", ST["run_started"] is None, str(ST.get("run_started")))

# 4) opt-in dry run -> DRY
done.clear(); captured.clear()
c.post("/api/run", data={"kind": "task", "text": "do y", "dryrun": "on"})
done.wait(3)
chk("dryrun=on is a DRY run (dry_run is True)", captured.get("dry_run") is True, str(captured))
wait_idle()

# 5) a second run requested while one is active is rejected WITH feedback (not silent)
srv._state["active"] = True
try:
    c.post("/api/run", data={"kind": "task", "text": "do z"})
    chk("busy run gives feedback", "already in progress" in (srv._state.get("last_msg") or ""), srv._state.get("last_msg"))
finally:
    srv._state["active"] = False

print("\n================ RUN DEFAULT = LIVE QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
# EU-302 class (failure-blind harness): this file printed "N FAIL" and still exited 0, so a real
# regression only surfaced via run_all's soft-tally and was invisible to a direct run — which is
# exactly how it read GREEN in isolation while failing in the suite (2026-07-17). Exit honestly.
sys.exit(0 if passed == len(results) else 1)
