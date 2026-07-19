"""Jira → Needs-you reconciliation (2026-07-19, Commander order): "If I answered in Jira it
needs to synchronize the needs-you" — parked entries, pending decisions and errored rows must
follow the ticket's LIVE Jira status instead of outliving it.

Pins:
  (1) DONE-like status (Done/QA/Closed…) → unparked + pending decision dropped + cleared
      with reason 'done in Jira';
  (2) ACTIVE-like status (To Do/In Progress) → unparked + decision dropped ('re-queued in
      Jira') — a lingering park would make the drain SKIP a ticket the Commander re-queued;
  (3) BLOCKED-like status → everything kept;
  (4) unreachable Jira / unknown status → kept (never clears on doubt);
  (5) a ticket with no matching Jira app → skipped, untouched;
  (6) the TTL throttle: a second call inside the window returns {"throttled": True}; force=True
      bypasses;
  (7) reconcile NEVER starts a build (no run/loop import in the module);
  (8) the wiring: /needs fires the throttled background pass and renders the ↻ Sync button;
      POST /api/needs-sync runs force=True and reports via the banner; the autopilot cycle
      carries the throttled call (source pins)."""
import sys
import tempfile
import types
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

from orchestrator import autopilot, decisions, needs_sync
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev",
                             protected_branch="main", backlog_backend="jira",
                             backlog={"project_key": "AUTO", "base_url": "https://x.atlassian.net"})],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)

STATUS = {"AUTO-1": "Done", "AUTO-2": "To Do", "AUTO-3": "Blocked", "AUTO-4": None,
          "AUTO-5": "QA"}


class _FakeBL:
    def _current_status(self, key):
        return STATUS.get(key)


import orchestrator.backlog.base as _bb
_orig_make = _bb.make_backlog
_bb.make_backlog = lambda app: _FakeBL()

# errored/awaiting cards for every ticket — reconcile must dismiss them for done AND active
import orchestrator.needs as _needs_mod
_orig_summary = _needs_mod.summary
_needs_mod.summary = lambda cfg, app_name=None: {
    "tasks": [{"ticket_id": t} for t in ("AUTO-1", "AUTO-2", "AUTO-3", "AUTO-4", "AUTO-5")]}

# seed: all five parked; decisions for 1, 2, 3
autopilot.save_blocked(cfg, {"AUTO-1", "AUTO-2", "AUTO-3", "AUTO-4", "AUTO-5", "ZZZ-9"})
for tid in ("AUTO-1", "AUTO-2", "AUTO-3"):
    decisions.add(cfg, Ticket(id=tid, key=tid, summary=f"S {tid}",
                              description="d", app="automatixy"),
                  "automatixy", f"What should we do about {tid}? It needs a proper decision.",
                  block=False)

try:
    r = needs_sync.reconcile(cfg, None, force=True)
finally:
    _bb.make_backlog = _orig_make
    _needs_mod.summary = _orig_summary

parked_after = autopilot.load_blocked(cfg)
pending_after = {p["id"] for p in decisions.load(cfg)}
cleared = dict(r.get("cleared", []))

chk("(1a) Done → unparked", "AUTO-1" not in parked_after)
chk("(1b) Done → decision dropped", "AUTO-1" not in pending_after)
chk("(1c) reason is 'done in Jira'", cleared.get("AUTO-1") == "done in Jira")
chk("(1d) QA counts as done-like", "AUTO-5" not in parked_after and cleared.get("AUTO-5") == "done in Jira")
chk("(2a) To Do → unparked (drain must not skip it)", "AUTO-2" not in parked_after)
chk("(2b) To Do → decision dropped", "AUTO-2" not in pending_after)
chk("(2c) reason is 're-queued in Jira'", cleared.get("AUTO-2") == "re-queued in Jira")

import json as _json
_dismissed = _json.loads((tmp / "dismissed.json").read_text(encoding="utf-8"))
chk("(2d) dead cards dismissed for done AND re-queued tickets",
    {"AUTO-1", "AUTO-2", "AUTO-5"} <= set(_dismissed))
chk("(2e) blocked/unknown cards NOT dismissed",
    "AUTO-3" not in _dismissed and "AUTO-4" not in _dismissed)
chk("(3) Blocked → kept parked + decision kept",
    "AUTO-3" in parked_after and "AUTO-3" in pending_after)
chk("(4) unreachable status → kept (never clears on doubt)", "AUTO-4" in parked_after)
chk("(5) no matching Jira app → skipped, untouched",
    "ZZZ-9" in parked_after and r.get("skipped_no_jira", 0) >= 1)

# (6) throttle
r2 = needs_sync.reconcile(cfg, None)          # inside the window
chk("(6a) second call inside the TTL is throttled", r2.get("throttled") is True)
_bb.make_backlog = lambda app: _FakeBL()
try:
    r3 = needs_sync.reconcile(cfg, None, force=True)
finally:
    _bb.make_backlog = _orig_make
chk("(6b) force=True bypasses the throttle", "checked" in r3)

# (7) never starts a build
src = Path("orchestrator/needs_sync.py").read_text(encoding="utf-8")
chk("(7) reconcile never imports the run/loop machinery",
    "from . import loop" not in src and "_run_bg" not in src and "run_agent" not in src)

# (8) wiring pins
ssrc = Path("orchestrator/server.py").read_text(encoding="utf-8")
chk("(8a) /needs fires the throttled background pass", "needs_sync" in ssrc
    and 'kwargs={"ttl_s": 300.0}' in ssrc)
chk("(8b) the ↻ Sync with Jira button renders on /needs", "Sync with Jira" in ssrc
    and "action=/api/needs-sync" in ssrc)
chk("(8c) POST /api/needs-sync runs force=True", "reconcile(cfg, audit, force=True)" in ssrc)
asrc = Path("orchestrator/autopilot.py").read_text(encoding="utf-8")
chk("(8d) the autopilot cycle carries the throttled reconcile",
    "_nsync.reconcile(cfg, audit, ttl_s=600.0)" in asrc)

print("\n========== NEEDS↔JIRA SYNC QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
