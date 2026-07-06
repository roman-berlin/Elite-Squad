"""Shared file-lock helper QA (EU-48 / generalised F8 lock).

``orchestrator.locking`` lifts the F8 audit-log pattern into two reusable primitives so the files that
still race — blocked / pending_decisions / usage_ledger — can share one correct lock instead of each
rolling its own. This harness proves both primitives hold under concurrency:

* ``locked_append``: N threads and N separate *processes* appending big rows to one JSONL yields a file
  where every line parses and no row is lost (the F8 guarantee, now generic).
* ``locked_rmw``: N threads and N processes each incrementing a counter in one JSON file lose no update
  (the lost-write bug the ticket is about), and a concurrent reader never sees a half-written file.
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

from orchestrator.locking import locked_append, locked_rmw

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


tmp = Path(tempfile.mkdtemp())

# Far bigger than a pipe/FS atomic-write boundary, so an unlocked append would split mid-row.
BIG = "x" * 200_000


# --- (1) locked_append: N threads hammering one JSONL ---------------------------------------------
def _appender(path: str, worker: int, rows: int) -> None:
    for i in range(rows):
        locked_append(path, json.dumps({"worker": worker, "seq": i, "diff": BIG, "marker": f"{worker}:{i}"}))


THREADS, ROWS = 8, 25
thr_path = tmp / "append_threads.jsonl"
threads = [threading.Thread(target=_appender, args=(str(thr_path), w, ROWS)) for w in range(THREADS)]
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
chk("append/threads: every line parses as JSON", not bad, f"{len(bad)} corrupt of {len(lines)}")
chk("append/threads: no rows lost", len(lines) == THREADS * ROWS, f"{len(lines)} != {THREADS * ROWS}")
chk("append/threads: every (worker,seq) marker present",
    {r.get("marker") for r in parsed} == {f"{w}:{i}" for w in range(THREADS) for i in range(ROWS)})
chk("append/threads: large payload survived intact", all(len(r.get("diff", "")) == len(BIG) for r in parsed))

# --- (2) locked_append: N separate processes -----------------------------------------------------
PROCS, PROC_ROWS = 6, 15
proc_path = tmp / "append_procs.jsonl"
append_child = (
    "import sys, json; sys.path.insert(0, '.');"
    "from orchestrator.locking import locked_append;"
    "p, w, n = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]);"
    "big = 'x' * 200000;"
    "[locked_append(p, json.dumps({'worker': w, 'seq': i, 'diff': big, 'marker': f'{w}:{i}'})) for i in range(n)]"
)
procs = [
    subprocess.Popen([sys.executable, "-c", append_child, str(proc_path), str(w), str(PROC_ROWS)], cwd=os.getcwd())
    for w in range(PROCS)
]
rcs = [p.wait() for p in procs]
chk("append/procs: all child writers exited cleanly", all(rc == 0 for rc in rcs), str(rcs))
plines = proc_path.read_text(encoding="utf-8").splitlines() if proc_path.exists() else []
pparsed, pbad = [], []
for ln in plines:
    try:
        pparsed.append(json.loads(ln))
    except json.JSONDecodeError:
        pbad.append(ln)
chk("append/procs: every line parses as JSON", not pbad, f"{len(pbad)} corrupt of {len(plines)}")
chk("append/procs: no rows lost across processes", len(plines) == PROCS * PROC_ROWS, f"{len(plines)} != {PROCS * PROC_ROWS}")

# --- (3) locked_rmw: N threads incrementing one counter ------------------------------------------
def _incr(value):
    value = value or {"count": 0}
    value["count"] += 1
    return value


def _bumper(path: str, times: int) -> None:
    for _ in range(times):
        locked_rmw(path, _incr, default={"count": 0})


RMW_THREADS, BUMPS = 8, 30
rmw_path = tmp / "rmw_threads.json"
bumpers = [threading.Thread(target=_bumper, args=(str(rmw_path), BUMPS)) for _ in range(RMW_THREADS)]
for t in bumpers:
    t.start()
for t in bumpers:
    t.join()
final = json.loads(rmw_path.read_text(encoding="utf-8"))
chk("rmw/threads: no lost updates", final.get("count") == RMW_THREADS * BUMPS, f"{final.get('count')} != {RMW_THREADS * BUMPS}")

# --- (4) locked_rmw: N separate processes incrementing one counter -------------------------------
RMW_PROCS, PROC_BUMPS = 6, 20
rmw_proc_path = tmp / "rmw_procs.json"
rmw_child = (
    "import sys; sys.path.insert(0, '.');"
    "from orchestrator.locking import locked_rmw;"
    "p, n = sys.argv[1], int(sys.argv[2]);"
    "f = lambda v: ({**(v or {'count': 0}), 'count': (v or {'count': 0})['count'] + 1});"
    "[locked_rmw(p, f, default={'count': 0}) for _ in range(n)]"
)
rprocs = [
    subprocess.Popen([sys.executable, "-c", rmw_child, str(rmw_proc_path), str(PROC_BUMPS)], cwd=os.getcwd())
    for _ in range(RMW_PROCS)
]
rrcs = [p.wait() for p in rprocs]
chk("rmw/procs: all child writers exited cleanly", all(rc == 0 for rc in rrcs), str(rrcs))
pfinal = json.loads(rmw_proc_path.read_text(encoding="utf-8")) if rmw_proc_path.exists() else {}
chk("rmw/procs: no lost updates across processes", pfinal.get("count") == RMW_PROCS * PROC_BUMPS,
    f"{pfinal.get('count')} != {RMW_PROCS * PROC_BUMPS}")

# --- (5) locked_rmw: reader never sees a torn write ----------------------------------------------
# A reader polling the file while writers churn must always parse it (os.replace is atomic), even
# if it occasionally races ahead of a not-yet-created file.
torn_path = tmp / "rmw_reader.json"
locked_rmw(torn_path, lambda v: {"count": 0}, default={"count": 0})  # seed
torn = {"bad": 0}
stop = threading.Event()


def _reader() -> None:
    while not stop.is_set():
        try:
            json.loads(torn_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            torn["bad"] += 1


reader = threading.Thread(target=_reader)
reader.start()
for _ in range(200):
    locked_rmw(torn_path, _incr, default={"count": 0})
stop.set()
reader.join()
chk("rmw/reader: never observed a torn/half-written file", torn["bad"] == 0, f"{torn['bad']} torn reads")

# --- (6) locked_rmw: default applied for a missing file ------------------------------------------
fresh_path = tmp / "rmw_fresh.json"
seen = {}
def _capture(v):
    seen["v"] = v
    return {"ok": True}
locked_rmw(fresh_path, _capture, default={"seeded": True})
chk("rmw/default: missing file yields the default value", seen.get("v") == {"seeded": True}, str(seen.get("v")))
chk("rmw/default: new value written", json.loads(fresh_path.read_text(encoding="utf-8")) == {"ok": True})

# --- (7) locked_rewrite: appenders race concurrent rewriters — nothing lost, nothing torn --------
# 2026-07-05 audit §7.4: governor/usage pruned their JSONL files with an unlocked write_text while
# locked_append writers were live — a row landing between read and rewrite was truncated away.
# locked_rewrite flocks the DATA file's own inode (the one appenders lock), so the interleave gap
# cannot exist. Seed rows are marked keep:false and must vanish; every appended keep:true row from
# every thread must survive all rewrites intact.
from orchestrator.locking import locked_rewrite

rw_path = tmp / "rewrite.jsonl"
for i in range(300):
    locked_append(rw_path, json.dumps({"keep": False, "seed": i}))
RW_THREADS, RW_ROWS, RW_PASSES = 8, 40, 5


def _rw_appender(worker: int) -> None:
    for i in range(RW_ROWS):
        locked_append(rw_path, json.dumps({"keep": True, "w": worker, "s": i}))


def _rw_rewriter() -> None:
    for _ in range(RW_PASSES):
        locked_rewrite(rw_path, lambda lines: [ln for ln in lines if '"keep": true' in ln])


rw_threads = [threading.Thread(target=_rw_appender, args=(w,)) for w in range(RW_THREADS)] \
    + [threading.Thread(target=_rw_rewriter) for _ in range(3)]
for t in rw_threads:
    t.start()
for t in rw_threads:
    t.join()
# One deterministic final pass: rewriters may all have finished before the last appends landed,
# but appended rows are keep:true so only the seed rows' fate depends on it.
locked_rewrite(rw_path, lambda lines: [ln for ln in lines if '"keep": true' in ln])

rw_lines = rw_path.read_text(encoding="utf-8").splitlines()
rw_parsed, rw_bad = [], []
for ln in rw_lines:
    try:
        rw_parsed.append(json.loads(ln))
    except json.JSONDecodeError:
        rw_bad.append(ln)
chk("rewrite/threads: every surviving line parses (no torn rows)", not rw_bad,
    f"{len(rw_bad)} corrupt of {len(rw_lines)}")
chk("rewrite/threads: every appended row survived the concurrent rewrites",
    {(r.get("w"), r.get("s")) for r in rw_parsed if r.get("keep")}
    == {(w, s) for w in range(RW_THREADS) for s in range(RW_ROWS)},
    f"{len(rw_parsed)} rows survived, want {RW_THREADS * RW_ROWS}")
chk("rewrite/threads: seed rows filtered out", not any(not r.get("keep") for r in rw_parsed),
    f"{sum(1 for r in rw_parsed if not r.get('keep'))} seed rows left")

print("\n============= SHARED FILE-LOCK QA =============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
