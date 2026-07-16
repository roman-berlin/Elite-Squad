"""EU-310 — a just-merged ticket must not be re-picked and rebuilt.

Live 2026-07-14 (EU-307): a ticket that modifies the unit's OWN code merged (17:34:39) but its
post-merge Jira transition was lost — it stayed In Progress, so the drain re-selected it 19s later
and rebuilt already-merged code. self_update_pending_restart fired the same instant; the daemon
didn't actually restart, and the Done transition was skipped/lost on that path.

The durable prevention is an in-memory recently-merged exclusion in the picker (independent of
whether the board transition landed). Plus a lost transition is now an audit event, not a silent
print, so the miss is diagnosable.

Pins:
  (1) _assemble_worklist excludes a merged key passed in the exclusion set (the picker honours it);
  (2) source: the loop records the merged ids into recently_merged and folds them into the pick
      exclusion for a cooldown window;
  (3) source: a failed post-merge transition records a merge_transition_failed audit event;
  (4) the cooldown is bounded (not infinite — a genuinely re-opened ticket isn't stuck forever).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
sdk.__getattr__ = lambda n: (lambda *a, **k: None)
sys.modules.setdefault("claude_agent_sdk", sdk)
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers={}, update=lambda *a, **k: None)
sys.modules.setdefault("requests", req)

sys.path.insert(0, ".")

from orchestrator import autopilot  # noqa: E402

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


APP = types.SimpleNamespace(name="EU")


def T(tid):
    return types.SimpleNamespace(id=tid, status="To Do")


# (1) the picker excludes a merged key when it's in the exclusion set
window = [(APP, T("EU-307")), (APP, T("EU-308"))]
wl = autopilot._assemble_worklist(window, blocked={"EU-307"}, resumed={}, cap=10)
ok("(1) a just-merged key in the exclusion set is not re-picked",
   [t.id for _, t in wl] == ["EU-308"], f"got {[t.id for _, t in wl]}")

# (2) source: recently_merged is recorded from MERGED reports and folded into the pick exclusion
src = Path("orchestrator/autopilot.py").read_text()
ok("(2) merged ids recorded into recently_merged",
   "recently_merged[r.ticket_id] = time.time()" in src)
ok("(2b) recently_merged folded into the pick exclusion set",
   "_pick_exclude = blocked | set(recently_merged)" in src
   and "_assemble_worklist(raw, _pick_exclude" in src)

# (3) source: a lost post-merge transition is an audit event, not a silent print
lsrc = Path("orchestrator/loop.py").read_text()
ok("(3) lost post-merge transition records merge_transition_failed",
   'audit.record("merge_transition_failed"' in lsrc)

# (4) the cooldown is bounded and applied as a time window
ok("(4) _MERGED_COOLDOWN_S is a bounded window",
   "_MERGED_COOLDOWN_S = 600.0" in src and "_now_m - ts < _MERGED_COOLDOWN_S" in src)

print(f"\n{checks}/{checks} passed")
