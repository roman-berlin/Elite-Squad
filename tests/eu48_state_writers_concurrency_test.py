"""EU-48 / F6 — the shared-state writers are concurrency-safe through their PUBLIC APIs.

`locking_test.py` proves the two primitives (`locked_append`/`locked_rmw`) in isolation, and
`autopilot_unblock_reread_test.py` proves the park-block re-read. This harness closes the remaining
gap: that the three call sites the ticket names actually USE the lock and so survive the concurrent
contexts they're written from (in-process autopilot loop + Telegram poll thread + cockpit Flask
threads — all threads of one process). Each block here drives the module's real public function, not
the helper, so a regression that quietly drops the lock back to a bare write_text/open("a") fails it.

The asserted bugs (all from the ticket):
* decisions — lockless read-modify-write in add()/resolve() loses or resurrects a decision;
* usage    — torn ledger lines are dropped by _rows(), so today's burn UNDER-counts and the
             runaway-budget auto-pause (budget_status/over_budget) can fail to trip;
* governor — torn rows corrupt the rolling-hour call count.
"""
from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, ".")

from orchestrator import decisions, governor, usage
from orchestrator.config import Config
from orchestrator.contracts import Ticket

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


def _ticket(tid: str) -> Ticket:
    return Ticket(id=tid, key=tid, summary=f"sum {tid}", description=f"desc {tid}",
                  acceptance_criteria=[])


def _run(targets):
    threads = [threading.Thread(target=t) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


# ---------------------------------------------------------------------------------------------------
# (1) decisions.add — N threads each filing a DISTINCT pending decision lose nothing.
#     Old code: items = load(cfg); ...; _save(cfg, items)  — a classic lost-update read-modify-write.
# ---------------------------------------------------------------------------------------------------
tmp1 = Path(tempfile.mkdtemp())
cfg1 = Config(apps=[], audit_path=str(tmp1 / "audit.jsonl"))
N_ADD = 40
_run([(lambda i=i: decisions.add(cfg1, _ticket(f"EU-{i:03d}"), "app", f"q{i}")) for i in range(N_ADD)])
stored = decisions.load(cfg1)
ids = {d["id"] for d in stored}
chk("decisions/add: every concurrent add is persisted (no lost write)",
    len(stored) == N_ADD and ids == {f"EU-{i:03d}" for i in range(N_ADD)},
    f"{len(stored)} of {N_ADD}, missing={sorted({f'EU-{i:03d}' for i in range(N_ADD)} - ids)[:5]}")

# ---------------------------------------------------------------------------------------------------
# (2) decisions.add de-dup still holds under concurrency: many threads re-filing the SAME id collapse
#     to exactly one entry (concurrent read-modify-write must not duplicate the row).
# ---------------------------------------------------------------------------------------------------
tmp2 = Path(tempfile.mkdtemp())
cfg2 = Config(apps=[], audit_path=str(tmp2 / "audit.jsonl"))
_run([(lambda i=i: decisions.add(cfg2, _ticket("EU-DUP"), "app", f"q{i}")) for i in range(25)])
dup = decisions.load(cfg2)
chk("decisions/add: concurrent re-file of one id de-dupes to a single entry",
    len(dup) == 1 and dup[0]["id"] == "EU-DUP", f"{len(dup)} entries")

# ---------------------------------------------------------------------------------------------------
# (3) decisions.resolve — concurrent resolve() never hands the same decision to two callers and never
#     loses one. Seed N decisions, then resolve all of them concurrently; each must be popped exactly
#     once (no duplicate id, no None while items remain) and the store must end empty.
# ---------------------------------------------------------------------------------------------------
tmp3 = Path(tempfile.mkdtemp())
cfg3 = Config(apps=[], audit_path=str(tmp3 / "audit.jsonl"))
N_RES = 40
for i in range(N_RES):
    decisions.add(cfg3, _ticket(f"EU-{i:03d}"), "app", f"q{i}")

popped: list[dict] = []
plock = threading.Lock()
def _resolver(tid):
    got = decisions.resolve(cfg3, "answer", tid)
    if got is not None:
        with plock:
            popped.append(got)

_run([(lambda i=i: _resolver(f"EU-{i:03d}")) for i in range(N_RES)])
popped_ids = [d["id"] for d in popped]
chk("decisions/resolve: every targeted decision resolved exactly once",
    len(popped_ids) == N_RES and len(set(popped_ids)) == N_RES,
    f"{len(popped_ids)} popped, {len(set(popped_ids))} unique")
chk("decisions/resolve: store fully drained, none left behind or resurrected",
    decisions.load(cfg3) == [], str(decisions.load(cfg3)))

# ---------------------------------------------------------------------------------------------------
# (4) usage.record — concurrent records keep every row intact, so today_tokens does NOT under-count
#     and the runaway-budget auto-pause trips. This is the exact ticket impact: torn lines are dropped
#     by _rows(), so an unlocked append would lose burn and over_budget() could fail to fire.
# ---------------------------------------------------------------------------------------------------
tmp4 = Path(tempfile.mkdtemp())
audit4 = str(tmp4 / "audit.jsonl")
usage.configure(audit4)
IN_TOK, OUT_TOK, N_REC = 1000, 500, 50           # each record = 1500 tokens
expected_tokens = N_REC * (IN_TOK + OUT_TOK)
# cap one record below the true total: only an exact, loss-free count crosses it.
cfg4 = Config(apps=[], audit_path=audit4, daily_token_budget=expected_tokens - (IN_TOK + OUT_TOK))
_run([(lambda i=i: usage.record("claude-opus", IN_TOK, OUT_TOK, tag="builder", ticket_id=f"EU-{i}"))
      for i in range(N_REC)])
got_tokens = usage.today_tokens(cfg4)
chk("usage/record: no under-count under concurrency (every row survived)",
    got_tokens == expected_tokens, f"{got_tokens} != {expected_tokens}")
chk("usage/record: runaway-budget auto-pause trips on the honest total",
    usage.over_budget(cfg4) and usage.budget_status(cfg4)["over"],
    f"used={got_tokens} cap={cfg4.daily_token_budget}")
usage.configure  # leave module pointer as-is; later harnesses reconfigure their own.
usage._PATH = None   # reset the module global so we don't leak this tmp path into other harnesses.

# ---------------------------------------------------------------------------------------------------
# (5) governor.note_call — concurrent bursts (the n>1 batched-append path included) keep every row, so
#     calls_last_hour counts them all. A torn row would be silently skipped and under-count the hour.
# ---------------------------------------------------------------------------------------------------
tmp5 = Path(tempfile.mkdtemp())
cfg5 = Config(apps=[], audit_path=str(tmp5 / "audit.jsonl"), usage_cap_per_hour=10_000)
N_THREADS, BURST = 20, 5                          # 20 threads × a 5-call burst each = 100 calls
_run([(lambda: governor.note_call(cfg5, BURST)) for _ in range(N_THREADS)])
hour = governor.calls_last_hour(cfg5)
chk("governor/note_call: concurrent bursts all counted (no torn/lost rows)",
    hour == N_THREADS * BURST, f"{hour} != {N_THREADS * BURST}")

# ---------------------------------------------------------------------------------------------------
# (6) approvals — the 2026-07-05 audit §7.4 HIGH race, closed via locking.locked_rmw.
#     proposals.json: N distinct enqueues from N threads racing approves/denies on other batches
#     lose nothing, and no actioned batch reverts to pending. approvals.json: a concurrent
#     approve('drill') + disapprove('adjutant') keeps BOTH kinds' records.
# ---------------------------------------------------------------------------------------------------
from orchestrator import approvals, filing

tmp6 = Path(tempfile.mkdtemp())
cfg6 = Config(apps=[], audit_path=str(tmp6 / "audit.jsonl"))

N_BATCH = 24
_run([(lambda i=i: approvals.enqueue_proposals(
    cfg6, app_name="automatixy", officer_label="qa", source=f"council-{i:02d}",
    report=[{"title": f"Finding {i:02d}", "type": "Task", "severity": "P2", "body": "b"}]))
    for i in range(N_BATCH)])
queued = approvals.pending_proposals(cfg6)
chk("approvals/enqueue: every concurrent batch is persisted (no lost write)",
    len(queued) == N_BATCH, f"{len(queued)} of {N_BATCH}")

# Race approve (filing stubbed — no Jira) against deny on disjoint batches + more enqueues.
_orig_file_findings = filing.file_findings
filing.file_findings = lambda app, label, block: filing.FilingResult()
try:
    ids = [b["id"] for b in queued]
    approve_ids, deny_ids = ids[:8], ids[8:16]
    _run([(lambda b=b: approvals.approve_proposals(cfg6, b)) for b in approve_ids]
         + [(lambda b=b: approvals.deny_proposals(cfg6, b, "no")) for b in deny_ids]
         + [(lambda i=i: approvals.enqueue_proposals(
             cfg6, app_name="automatixy", officer_label="qa", source=f"late-{i}",
             report=[{"title": f"Late {i}", "type": "Task", "severity": "P2", "body": "b"}]))
            for i in range(4)])
finally:
    filing.file_findings = _orig_file_findings
final_items = approvals._load_proposals(cfg6)
by_id = {b["id"]: b for b in final_items}
chk("approvals/approve+deny+enqueue race: no actioned batch reverts, none lost",
    all(by_id.get(b, {}).get("status") == "approved" for b in approve_ids)
    and all(by_id.get(b, {}).get("status") == "denied" for b in deny_ids)
    and len(approvals.pending_proposals(cfg6)) == (N_BATCH - 16) + 4,
    f"statuses={[by_id.get(b, {}).get('status') for b in approve_ids + deny_ids]}, "
    f"pending={len(approvals.pending_proposals(cfg6))}")

# approvals.json: concurrent approve/disapprove of DIFFERENT kinds keeps both records.
# disapprove() is sync; drive the state write the same way approve() does, via its marker path.
(Path(cfg6.audit_path).with_name("drill-report.md")).write_text("drill body", encoding="utf-8")
(Path(cfg6.audit_path).with_name("adjutant-report.md")).write_text("adj body", encoding="utf-8")


def _approve_drill():
    # approve() awaits drillmaster.apply + does git I/O; pin the RMW itself instead, exactly
    # as approve() calls it, so the state-write interleaving is what's under test.
    approvals._mutate_state(
        cfg6, lambda st: {**st, "drill": {"hash": "h1", "action": "approved", "ts": 1.0}})


_run([_approve_drill, (lambda: approvals.disapprove(cfg6, "adjutant", "not now"))] * 3)
st6 = approvals._load(cfg6)
chk("approvals/state: concurrent drill-approve + adjutant-disapprove keeps BOTH kinds",
    st6.get("drill", {}).get("action") == "approved"
    and st6.get("adjutant", {}).get("action") == "disapproved", str(st6))

# Regression tripwire: the writers must stay on locking.locked_rmw (a quiet revert to bare
# write_text reintroduces the lost-update race even if the assertions above get lucky).
_src = Path(approvals.__file__).read_text(encoding="utf-8")
chk("approvals: writers routed through locking.locked_rmw (no bare write_text left)",
    "locked_rmw" in _src and "write_text(json.dumps" not in _src)

print("\n======= EU-48 STATE-WRITERS CONCURRENCY QA =======")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
