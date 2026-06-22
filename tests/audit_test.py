"""Audit-log concurrency QA (EU-7 / F8).

AuditLog.record appends from the cockpit run thread, the autopilot, and the decisions poller — sometimes
in separate processes. Rows carry build/review summaries + diffs that exceed an atomic write, so without a
lock concurrent appends interleave and corrupt the JSONL that all forensics/consolidate/dashboard read.

This harness proves: (1) N threads writing large rows yields a file where every line parses as JSON;
(2) N separate *processes* doing the same is equally clean; (3) single-writer behaviour is unchanged.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, ".")

from orchestrator.audit import AuditLog

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


tmp = Path(tempfile.mkdtemp())

# Large payload — far bigger than a pipe/FS atomic-write boundary, so an unlocked append would split mid-row.
BIG = "x" * 200_000


def _writer(path: str, worker: int, rows: int) -> None:
    log = AuditLog(path)
    for i in range(rows):
        log.record("stress", worker=worker, seq=i, diff=BIG, marker=f"{worker}:{i}")


# --- (1) intra-process: N threads hammering one AuditLog -------------------------------------------
THREADS, ROWS = 8, 25
thr_path = tmp / "threads.jsonl"
threads = [threading.Thread(target=_writer, args=(str(thr_path), w, ROWS)) for w in range(THREADS)]
for t in threads:
    t.start()
for t in threads:
    t.join()

lines = thr_path.read_text(encoding="utf-8").splitlines()
parsed, bad = [], []
for ln in lines:
    try:
        parsed.append(json.loads(ln))
    except json.JSONDecodeError:
        bad.append(ln)
chk("threads: every line parses as JSON", not bad, f"{len(bad)} corrupt of {len(lines)}")
chk("threads: no rows lost", len(lines) == THREADS * ROWS, f"{len(lines)} != {THREADS * ROWS}")
chk("threads: every (worker,seq) marker present",
    {r.get("marker") for r in parsed} == {f"{w}:{i}" for w in range(THREADS) for i in range(ROWS)})
chk("threads: large payload survived intact", all(len(r.get("diff", "")) == len(BIG) for r in parsed))

# --- (2) inter-process: N separate processes appending to one file --------------------------------
# Each child runs _writer in its own interpreter, so only fcntl.flock (not the threading.Lock) serialises them.
PROCS, PROC_ROWS = 6, 15
proc_path = tmp / "procs.jsonl"
child = (
    "import sys; sys.path.insert(0, '.');"
    "from orchestrator.audit import AuditLog;"
    "p, w, n = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]);"
    "log = AuditLog(p);"
    "big = 'x' * 200000;"
    "[log.record('stress', worker=w, seq=i, diff=big, marker=f'{w}:{i}') for i in range(n)]"
)
procs = [
    subprocess.Popen([sys.executable, "-c", child, str(proc_path), str(w), str(PROC_ROWS)],
                     cwd=os.getcwd())
    for w in range(PROCS)
]
rcs = [p.wait() for p in procs]
chk("procs: all child writers exited cleanly", all(rc == 0 for rc in rcs), str(rcs))

plines = proc_path.read_text(encoding="utf-8").splitlines() if proc_path.exists() else []
pparsed, pbad = [], []
for ln in plines:
    try:
        pparsed.append(json.loads(ln))
    except json.JSONDecodeError:
        pbad.append(ln)
chk("procs: every line parses as JSON", not pbad, f"{len(pbad)} corrupt of {len(plines)}")
chk("procs: no rows lost across processes", len(plines) == PROCS * PROC_ROWS, f"{len(plines)} != {PROCS * PROC_ROWS}")
chk("procs: every (worker,seq) marker present",
    {r.get("marker") for r in pparsed} == {f"{w}:{i}" for w in range(PROCS) for i in range(PROC_ROWS)})

# --- (3) single-writer behaviour unchanged --------------------------------------------------------
solo_path = tmp / "solo.jsonl"
solo = AuditLog(solo_path)
solo.record("alpha", n=1)
solo.record("beta", n=2, diff="hello")
srows = [json.loads(x) for x in solo_path.read_text(encoding="utf-8").splitlines()]
chk("single: writes exactly the rows recorded", len(srows) == 2, str(len(srows)))
chk("single: fields preserved in order", srows[0]["event"] == "alpha" and srows[1]["diff"] == "hello")
chk("single: each row carries a ts + event", all("ts" in r and "event" in r for r in srows))
chk("single: diff_hash unchanged", AuditLog.diff_hash("abc") == AuditLog.diff_hash("abc") and len(AuditLog.diff_hash("abc")) == 12)

print("\n============= AUDIT CONCURRENCY QA =============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
