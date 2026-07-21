"""EU-406 — needs_sync hardening: snapshot race, decision-deletion scope, PR/budget park
coverage, custom-status safety (production audit 2026-07-21, P1s).

Pins the four acceptance criteria, each watchable RED on the pre-fix code:

  AC1 — the final write is a LOCKED READ-MODIFY-WRITE against the CURRENT store (remove
        exactly the ids the pass decided on), never a snapshot overwrite. A park a concurrent
        drain makes DURING the minutes-long Jira scan must survive. Pre-fix: reconcile snap-
        shotted parked at the top, subtracted its decisions, and save_blocked()'d the stale
        snapshot back — blowing away the concurrent park (the EU-218 lost-park class).

  AC2 — decisions are dropped ONLY on explicit DONE/ACTIVE classifications; an unrecognized
        Jira status (a renamed/custom column) keeps everything and is SURFACED (audit event +
        return dict + one-line note) instead of guessed 'active' and silently cleared.

  AC3 — PR_OPENED and run-budget ESCALATED parks are Jira-INVISIBLE (they never transition
        Jira to Blocked, so the ticket sits 'In Progress' the whole time it is parked). Such a
        park must NOT be cleared just because Jira says 'In Progress' — that is its resting
        state, not a Commander re-queue — only an explicit DONE ends it. Pre-fix: 'In Progress'
        -> 'active' -> unparked -> the drain rebuilt the already-reviewed ticket (oscillation).

  AC4 — this harness pins all four (AC1 + AC2 + AC3 + the existence of this harness).
"""
import sys
import tempfile
import types
from pathlib import Path

# Stub the Agent SDK + requests the same way the sibling needs_sync_test.py does, so importing
# the orchestrator never reaches for a real network or model.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import autopilot, decisions, needs_sync          # noqa: E402
from orchestrator.config import Config, AppConfig                  # noqa: E402
from orchestrator.contracts import Ticket                          # noqa: E402

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


def _make_cfg():
    tmp = Path(tempfile.mkdtemp())
    cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev",
                                 protected_branch="main", backlog_backend="jira",
                                 backlog={"project_key": "AUTO",
                                          "base_url": "https://x.atlassian.net"})],
                 audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
    return cfg, tmp


# Per-section state read by the fakes below. Each AC clears + repopulates these before its run.
_STATUS: dict[str, str | None] = {}
_OUTCOME: dict[str, str] = {}
_SIDEEFFECT: dict[str, object] = {}   # tid -> callable(tid) fired inside _current_status


class _FakeBL:
    def _current_status(self, key):
        se = _SIDEEFFECT.get(key)
        if se:
            se(key)
        return _STATUS.get(key)


import orchestrator.backlog.base as _bb
_orig_make = _bb.make_backlog
_bb.make_backlog = lambda app: _FakeBL()

import orchestrator.needs as _needs_mod
_orig_summary = _needs_mod.summary


def _fake_summary(cfg, app_name=None):
    tasks = [{"ticket_id": t, "outcome": o} for t, o in _OUTCOME.items()]
    return {"tasks": tasks, "rows": [], "decisions": [], "proposals": [], "total": 0}


_needs_mod.summary = _fake_summary


class _FakeAudit:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def record(self, event, **kw):
        self.events.append((event, kw))


def _seed_decision(cfg, tid):
    decisions.add(cfg, Ticket(id=tid, key=tid, summary=f"S {tid}",
                              description="d", app="automatixy"),
                  "automatixy", f"Decision needed for {tid}?", block=False)


def _pending_ids(cfg):
    return {p["id"] for p in decisions.load(cfg)}


# ====================================================================== AC1 ===
# The final write is a locked read-modify-write against the CURRENT store; a park a concurrent
# drain makes DURING the Jira scan must survive.
_STATUS.clear(); _OUTCOME.clear(); _SIDEEFFECT.clear()
cfg, _tmp = _make_cfg()
autopilot.save_blocked(cfg, {"AUTO-10"})
_STATUS["AUTO-10"] = "Done"


def _inject_concurrent_park(_key):
    # Simulate a concurrent drain parking AUTO-11 mid-scan (the EU-218 lost-park class):
    # the snapshot taken at reconcile start was {AUTO-10}; by the time the scan ends, the live
    # store is {AUTO-10, AUTO-11}. A snapshot overwrite would erase AUTO-11.
    autopilot.save_blocked(cfg, autopilot.load_blocked(cfg) | {"AUTO-11"})


_SIDEEFFECT["AUTO-10"] = _inject_concurrent_park
_r1 = needs_sync.reconcile(cfg, None, force=True)
_parked_after_1 = autopilot.load_blocked(cfg)
chk("AC1: a park made DURING the Jira scan survives (no snapshot overwrite)",
    "AUTO-11" in _parked_after_1)
chk("AC1: the id the pass decided to clear (Done) is removed", "AUTO-10" not in _parked_after_1)

_ns_src = Path("orchestrator/needs_sync.py").read_text(encoding="utf-8")
chk("AC1: reconcile removes decided ids via autopilot.remove_blocked (RMW against current store)",
    "autopilot.remove_blocked(cfg" in _ns_src)
chk("AC1: reconcile no longer writes a pre-scan snapshot back (save_blocked(still_parked) gone)",
    "save_blocked(cfg, still_parked)" not in _ns_src)
_ap_src = Path("orchestrator/autopilot.py").read_text(encoding="utf-8")
chk("AC1: autopilot.remove_blocked primitive exists (locked_rmw set-difference, default [])",
    "def remove_blocked(cfg" in _ap_src and "locked_rmw" in _ap_src)


# ====================================================================== AC2 ===
# Decisions are dropped ONLY on explicit DONE/ACTIVE; unknown statuses keep everything and are
# surfaced (audit + return dict + note) instead of guessed.
chk("AC2-unit: Done -> done", needs_sync._classify("Done") == "done")
chk("AC2-unit: QA -> done", needs_sync._classify("QA") == "done")
chk("AC2-unit: To Do -> active", needs_sync._classify("To Do") == "active")
chk("AC2-unit: In Progress -> active", needs_sync._classify("In Progress") == "active")
chk("AC2-unit: Blocked -> blocked", needs_sync._classify("Blocked") == "blocked")
chk("AC2-unit: custom 'On Hold - Client' -> blocked (substring match)",
    needs_sync._classify("On Hold - Client") == "blocked")
chk("AC2-unit: 'Needs Triage' -> unknown (NOT guessed active)",
    needs_sync._classify("Needs Triage") == "unknown")
chk("AC2-unit: 'Greebeldyflux' -> unknown", needs_sync._classify("Greebeldyflux") == "unknown")
chk("AC2-unit: None -> unknown", needs_sync._classify(None) == "unknown")

_STATUS.clear(); _OUTCOME.clear(); _SIDEEFFECT.clear()
cfg, _tmp = _make_cfg()
for tid in ("AUTO-20", "AUTO-21", "AUTO-22", "AUTO-23", "AUTO-24"):
    autopilot.save_blocked(cfg, autopilot.load_blocked(cfg) | {tid})
    _seed_decision(cfg, tid)
_STATUS.update({"AUTO-20": "Done", "AUTO-21": "In Progress",
                "AUTO-22": "Needs Triage", "AUTO-23": "On Hold - Client",
                "AUTO-24": "Greebeldyflux"})
_OUTCOME.update({t: "errored" for t in
                 ("AUTO-20", "AUTO-21", "AUTO-22", "AUTO-23", "AUTO-24")})
_audit2 = _FakeAudit()
_r2 = needs_sync.reconcile(cfg, _audit2, force=True)
_pending2 = _pending_ids(cfg)
chk("AC2: explicit Done drops the decision", "AUTO-20" not in _pending2)
chk("AC2: explicit Active drops the decision", "AUTO-21" not in _pending2)
chk("AC2: unknown 'Needs Triage' KEEPS the decision", "AUTO-22" in _pending2)
chk("AC2: unknown 'Greebeldyflux' KEEPS the decision", "AUTO-24" in _pending2)
chk("AC2: custom blocked 'On Hold - Client' KEEPS the decision", "AUTO-23" in _pending2)
_unk_ids = {t for t, _ in _r2.get("unknown", [])}
chk("AC2: unknown statuses surfaced in the return dict (r['unknown'])",
    "AUTO-22" in _unk_ids and "AUTO-24" in _unk_ids)
chk("AC2: a blocked-ish custom status is NOT surfaced as unknown", "AUTO-23" not in _unk_ids)
_unk_ev = [kw for e, kw in _audit2.events if e == "needs_sync_unknown_status"]
chk("AC2: a needs_sync_unknown_status audit event is recorded", len(_unk_ev) == 1)
chk("AC2: the audit event names the unknown tickets + their raw statuses",
    bool(_unk_ev) and {"AUTO-22", "AUTO-24"} <= set(_unk_ev[0].get("tickets", []))
    and _unk_ev[0].get("statuses", {}).get("AUTO-22") == "Needs Triage")
chk("AC2: an unrecognized-status ticket is kept parked (not cleared)",
    "AUTO-22" in autopilot.load_blocked(cfg))
chk("AC2: reconcile returns the 'unknown' key", "unknown" in _r2)
# the one-line /needs note: the /api/needs-sync banner must mention unknown statuses
_srv_src = Path("orchestrator/server.py").read_text(encoding="utf-8")
chk("AC2: the /needs sync banner surfaces unknown statuses (one-line note)",
    'r.get("unknown"' in _srv_src and "unrecognized" in _srv_src)


# ====================================================================== AC3 ===
# PR_OPENED and run-budget ESCALATED parks are Jira-invisible (Jira stays 'In Progress'); they
# must be KEPT on active and only cleared on DONE. Decision-backed / error parks (Jira-visible)
# keep the existing clear-on-active semantics.
_STATUS.clear(); _OUTCOME.clear(); _SIDEEFFECT.clear()
cfg, _tmp = _make_cfg()
autopilot.save_blocked(cfg, {"AUTO-30", "AUTO-31", "AUTO-32", "AUTO-33"})
# every one of these sits 'In Progress' in Jira (the Jira-invisible resting state)
_STATUS.update({t: "In Progress" for t in ("AUTO-30", "AUTO-31", "AUTO-32", "AUTO-33")})
_OUTCOME.update({"AUTO-30": "PR / needs you",   # PR_OPENED park, no decision -> invisible
                 "AUTO-31": "escalated",         # run-budget ESCALATED, no decision -> invisible
                 "AUTO-32": "escalated",         # decision-backed ESCALATED -> visible
                 "AUTO-33": "errored"})          # error park, no decision -> visible
_seed_decision(cfg, "AUTO-32")                   # only AUTO-32 carries a pending decision
_r3 = needs_sync.reconcile(cfg, None, force=True)
_parked_after_3 = autopilot.load_blocked(cfg)
_pending3 = _pending_ids(cfg)
chk("AC3: PR_OPENED park KEPT on active (Jira-invisible — 'In Progress' != re-queue)",
    "AUTO-30" in _parked_after_3)
chk("AC3: run-budget ESCALATED park KEPT on active (Jira-invisible)",
    "AUTO-31" in _parked_after_3)
chk("AC3: decision-backed ESCALATED cleared on active + decision dropped (Jira-visible)",
    "AUTO-32" not in _parked_after_3 and "AUTO-32" not in _pending3)
chk("AC3: error park cleared on active (Jira-visible Blocked -> re-queue)",
    "AUTO-33" not in _parked_after_3)
_cleared3 = {t for t, _ in _r3.get("cleared", [])}
chk("AC3: PR/budget parks NOT in the cleared list",
    "AUTO-30" not in _cleared3 and "AUTO-31" not in _cleared3)

# DONE still ends even a Jira-invisible park (the work is truly over).
_STATUS.clear(); _OUTCOME.clear(); _SIDEEFFECT.clear()
cfg2, _tmp2 = _make_cfg()
autopilot.save_blocked(cfg2, {"AUTO-40"})
_STATUS["AUTO-40"] = "Done"
_OUTCOME["AUTO-40"] = "PR / needs you"
needs_sync.reconcile(cfg2, None, force=True)
chk("AC3: explicit Done clears even a Jira-invisible (PR) park",
    "AUTO-40" not in autopilot.load_blocked(cfg2))


# ====================================================================== AC4 ===
# This harness pins all four: prove each AC block asserted something non-trivial.
_ac1 = any(n.startswith("AC1:") for n, ok, _ in results if ok)
_ac2 = any(n.startswith("AC2:") for n, ok, _ in results if ok)
_ac3 = any(n.startswith("AC3:") for n, ok, _ in results if ok)
chk("AC4: harness exercises AC1 (snapshot-race RMW)", _ac1)
chk("AC4: harness exercises AC2 (decision scope + unknown surfacing)", _ac2)
chk("AC4: harness exercises AC3 (PR/budget Jira-invisible parks)", _ac3)


# ====================================================================== ======= =
print("\n========== EU-406 NEEDS_SYNC HARDENING QA ==========")
_passed = sum(1 for _, ok, _ in results if ok)
for _n, _ok, _d in results:
    print(f"  [{'PASS' if _ok else 'FAIL'}] {_n}" + (f"  ({_d})" if _d and not _ok else ""))
print("----------------------------------------------------")
print(f"  {_passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if _passed == len(results) else f"{len(results) - _passed} FAIL")

# restore so later harnesses in the same process see the real backlog/summary
_bb.make_backlog = _orig_make
_needs_mod.summary = _orig_summary

sys.exit(0 if _passed == len(results) else 1)
