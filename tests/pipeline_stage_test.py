"""Pipeline-stage derivation from audit events (EU-312).

derive_pipeline_stage() turns one ticket's raw audit events into its CURRENT stage — derived from
the deciding event, picked by (timestamp, list-position) among the relevant kinds, never by a
forward-only assumption. This is the EU-136 fix: a `gate` event followed by a later `build` retry
must report "Building", not get stuck showing "Gate". Because timestamps only resolve to the
second, two relevant events can share one ts — the one appearing LATER in the input list decides.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from orchestrator.pipeline_state import STALE_THRESHOLD_SECONDS, derive_pipeline_stage

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


def ev(tid, kind, ts):
    return {"ticket_id": tid, "event": kind, "ts": ts}


NOW = datetime(2026, 7, 14, 12, 0, 0)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


# --- (1) forward progression + one backward retry (gate -> later build) = EU-136 -------------------
t0 = NOW - timedelta(hours=3)
t1 = NOW - timedelta(hours=2)   # gate
t2 = NOW - timedelta(hours=1)   # build, LATER than gate -> a retry
events = [
    ev("EU-1", "ticket_start", _fmt(t0)),
    ev("EU-1", "gate", _fmt(t1)),
    ev("EU-1", "build", _fmt(t2)),
]
out = derive_pipeline_stage(events, now=NOW)
chk("EU-136: gate then later build -> stage is Building, not Gate",
    out.get("EU-1", {}).get("stage") == "Building", out.get("EU-1"))
chk("EU-136: since_ts is the later build event's ts",
    out.get("EU-1", {}).get("since_ts") == t2, out.get("EU-1"))

# plain forward progression (no retry): ticket_start -> build -> gate -> review
t0b, t1b, t2b, t3b = (NOW - timedelta(hours=4), NOW - timedelta(hours=3),
                       NOW - timedelta(hours=2), NOW - timedelta(hours=1))
fwd = [
    ev("EU-2", "ticket_start", _fmt(t0b)),
    ev("EU-2", "build", _fmt(t1b)),
    ev("EU-2", "gate", _fmt(t2b)),
    ev("EU-2", "review", _fmt(t3b)),
]
out_fwd = derive_pipeline_stage(fwd, now=NOW)
chk("forward progression: last event review -> stage Review",
    out_fwd.get("EU-2", {}).get("stage") == "Review", out_fwd.get("EU-2"))

# --- (1b) IDENTICAL-timestamp tie-break: the LATER event in list order decides (EU-312 review fix) --
# Audit ts resolves only to the second, so gate and build can carry the exact same ts string. The
# deciding event must be whichever appears LATER in the input list (it was appended later), NOT
# whichever max() happens to encounter first. This is the exact case the old max()-over-ts call got
# wrong (it returned the first-seen maximum -> stuck on Gate).
same_ts = _fmt(NOW - timedelta(hours=1))
tie_gate_then_build = [
    ev("EU-10", "ticket_start", _fmt(NOW - timedelta(hours=2))),
    ev("EU-10", "gate", same_ts),
    ev("EU-10", "build", same_ts),   # same second as gate, but later in list -> a retry
]
out_tie1 = derive_pipeline_stage(tie_gate_then_build, now=NOW)
chk("tie-break: gate then build at identical ts -> Building (later in list wins)",
    out_tie1.get("EU-10", {}).get("stage") == "Building", out_tie1.get("EU-10"))

# Reverse order at the same ts proves the tie-break is list-position, not a build>gate preference.
tie_build_then_gate = [
    ev("EU-11", "ticket_start", _fmt(NOW - timedelta(hours=2))),
    ev("EU-11", "build", same_ts),
    ev("EU-11", "gate", same_ts),    # same second as build, but later in list
]
out_tie2 = derive_pipeline_stage(tie_build_then_gate, now=NOW)
chk("tie-break: build then gate at identical ts -> Gate (later in list wins)",
    out_tie2.get("EU-11", {}).get("stage") == "Gate", out_tie2.get("EU-11"))

# --- (2) blocked / needs_human -> since_ts is THAT event's ts, not ticket_start's --------------------
tb0 = NOW - timedelta(hours=5)
tb1 = NOW - timedelta(hours=1)   # blocked, much later than start
blocked_events = [
    ev("EU-3", "ticket_start", _fmt(tb0)),
    ev("EU-3", "build", _fmt(tb0 + timedelta(minutes=5))),
    ev("EU-3", "blocked", _fmt(tb1)),
]
out_b = derive_pipeline_stage(blocked_events, now=NOW)
chk("blocked: stage == Blocked", out_b.get("EU-3", {}).get("stage") == "Blocked", out_b.get("EU-3"))
chk("blocked: since_ts == blocked event's ts (not ticket_start)",
    out_b.get("EU-3", {}).get("since_ts") == tb1, out_b.get("EU-3"))

tn0 = NOW - timedelta(hours=5)
tn1 = NOW - timedelta(hours=2)   # needs_human, later than start
needs_events = [
    ev("EU-4", "ticket_start", _fmt(tn0)),
    ev("EU-4", "review", _fmt(tn0 + timedelta(minutes=5))),
    ev("EU-4", "needs_human", _fmt(tn1)),
]
out_n = derive_pipeline_stage(needs_events, now=NOW)
chk("needs_human: stage == Needs-you", out_n.get("EU-4", {}).get("stage") == "Needs-you", out_n.get("EU-4"))
chk("needs_human: since_ts == needs_human event's ts (not ticket_start)",
    out_n.get("EU-4", {}).get("since_ts") == tn1, out_n.get("EU-4"))

# --- (3) only ticket_start (no build/gate/review/terminal) -> Queued ---------------------------------
queued_events = [ev("EU-5", "ticket_start", _fmt(NOW - timedelta(minutes=10)))]
out_q = derive_pipeline_stage(queued_events, now=NOW)
chk("only ticket_start -> stage Queued", out_q.get("EU-5", {}).get("stage") == "Queued", out_q.get("EU-5"))

# no events at all -> empty mapping (nothing to report as current)
chk("empty input -> empty mapping", derive_pipeline_stage([], now=NOW) == {})

# --- (4) age_seconds / is_stale threshold -------------------------------------------------------------
just_under = NOW - timedelta(seconds=STALE_THRESHOLD_SECONDS - 60)
just_over = NOW - timedelta(seconds=STALE_THRESHOLD_SECONDS + 60)
stale_events = [
    ev("EU-6", "ticket_start", _fmt(just_under - timedelta(minutes=1))),
    ev("EU-6", "blocked", _fmt(just_under)),
    ev("EU-7", "ticket_start", _fmt(just_over - timedelta(minutes=1))),
    ev("EU-7", "blocked", _fmt(just_over)),
]
out_s = derive_pipeline_stage(stale_events, now=NOW)
chk("age_seconds computed from since_ts to now",
    abs(out_s["EU-6"]["age_seconds"] - (STALE_THRESHOLD_SECONDS - 60)) < 2, out_s["EU-6"]["age_seconds"])
chk("just under threshold -> is_stale False", out_s["EU-6"]["is_stale"] is False, out_s["EU-6"])
chk("just over threshold -> is_stale True", out_s["EU-7"]["is_stale"] is True, out_s["EU-7"])

# --- (5) mapping keyed by ticket_id, correctly separates interleaved tickets --------------------------
interleaved = [
    ev("EU-8", "ticket_start", _fmt(NOW - timedelta(hours=3))),
    ev("EU-9", "ticket_start", _fmt(NOW - timedelta(hours=3))),
    ev("EU-8", "build", _fmt(NOW - timedelta(hours=2))),
    ev("EU-9", "build", _fmt(NOW - timedelta(hours=2, minutes=30))),
    ev("EU-8", "merged", _fmt(NOW - timedelta(hours=1))),
    ev("EU-9", "gate", _fmt(NOW - timedelta(minutes=90))),
    ev("EU-9", "build", _fmt(NOW - timedelta(minutes=30))),   # EU-9 retries after gate
]
out_i = derive_pipeline_stage(interleaved, now=NOW)
chk("interleaved: EU-8 -> Merged", out_i.get("EU-8", {}).get("stage") == "Merged", out_i.get("EU-8"))
chk("interleaved: EU-9 -> Building (retry after gate)",
    out_i.get("EU-9", {}).get("stage") == "Building", out_i.get("EU-9"))
chk("interleaved: both tickets present and distinct", set(out_i.keys()) == {"EU-8", "EU-9"})

# --- record shape: every record carries the documented keys ------------------------------------------
rec = out_i["EU-8"]
chk("record has all documented keys",
    set(rec.keys()) >= {"ticket_id", "stage", "since_ts", "age_seconds", "is_stale"}, rec.keys())

print("\n============= PIPELINE STAGE DERIVATION QA =============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
