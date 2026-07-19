"""Audit-log rotation QA (EU-363).

state/audit.jsonl was append-only FOREVER (7.86MB / 13,262 lines at verification) and the hot
paths re-parse whatever the live file holds (consolidate.py:66 rejection dedup, load_tasks every
drain cycle). EU-345 fixed the reader COST; this pins the writer-side rotation: when the live
file crosses the size threshold, events older than the live window move to
``<state>/audit/archive-YYYY-MM.jsonl`` (month-keyed by each event's own ts) and the live file
keeps only the trailing window — shrunk IN PLACE under the same write lock + flock as appends, so
a concurrent writer can never lose a row and dashboard's EU-345 incremental reader falls back to
a clean full re-read (dashboard.py:122 names this exact rotation case).

Proves: (1) rotation splits old events into the month archive and keeps the window + new event,
losing nothing and preserving order; (2) a below-threshold file never rotates; (3) an over-size
file of purely in-window events is left intact (no-op pass, appends keep landing); (4) an
undatable/malformed line is kept in the live file, never guessed into an archive; (5) N separate
*processes* appending while rotation fires lose no rows and duplicate none; (6) dashboard's
incremental reader warmed BEFORE rotation returns exactly the post-rotation live lines after —
no stale splice.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")

from orchestrator.audit import AuditLog

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


tmp = Path(tempfile.mkdtemp())


def _ts(days_ago: float) -> str:
    dt = datetime.now().astimezone() - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S%z")


def _row(marker: str, days_ago: float, pad: int = 0) -> str:
    r = {"ts": _ts(days_ago), "event": "stress", "marker": marker}
    if pad:
        r["diff"] = "x" * pad
    return json.dumps(r)


def _seed(path: Path, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(r + "\n" for r in rows), encoding="utf-8")


def _mk(path: Path, **kw) -> AuditLog:
    try:
        return AuditLog(path, **kw)
    except TypeError:
        # Pre-EU-363 signature (no rotation knobs) — fall through so the assertions
        # below go RED behaviourally (no archive is ever created) instead of crashing.
        return AuditLog(path)


def _lines(p: Path) -> list[str]:
    if not p.exists():
        return []
    return [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _markers(lines: list[str]) -> list[str]:
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln).get("marker"))
        except json.JSONDecodeError:
            out.append(None)
    return out


# --- (1) basic rotation: old months archived, window + new event kept, order preserved ------------
d1 = tmp / "s1"
p1 = d1 / "audit.jsonl"
OLD_DAYS = 30.0
old_month = (datetime.now().astimezone() - timedelta(days=OLD_DAYS)).strftime("%Y-%m")
old_rows = [_row(f"old:{i}", OLD_DAYS) for i in range(40)]
recent_rows = [_row(f"recent:{i}", 1.0) for i in range(15)]
_seed(p1, old_rows + recent_rows)
log1 = _mk(p1, rotate_max_bytes=1, live_window_days=14)
log1.record("fresh", marker="new:0")

arch1 = d1 / "audit" / f"archive-{old_month}.jsonl"
live1 = _lines(p1)
archived1 = _lines(arch1)
chk("rotation: month archive file created", arch1.exists(), str(arch1))
chk("rotation: all old events archived, in original order",
    _markers(archived1) == [f"old:{i}" for i in range(40)],
    f"{len(archived1)} archived")
chk("rotation: live file keeps the window + the new event, in order",
    _markers(live1) == [f"recent:{i}" for i in range(15)] + ["new:0"],
    f"{len(live1)} live lines")
chk("rotation: nothing lost across live+archive",
    len(live1) + len(archived1) == 40 + 15 + 1,
    f"{len(live1)}+{len(archived1)} != 56")
chk("rotation: every surviving line parses as JSON",
    all(None is not json.loads(x) for x in live1 + archived1))

# --- (2) below-threshold: never rotates ------------------------------------------------------------
d2 = tmp / "s2"
p2 = d2 / "audit.jsonl"
log2 = _mk(p2)          # default thresholds (MBs) — a 3-row file is far below them
for i in range(3):
    log2.record("stress", marker=f"solo:{i}")
chk("no-op: below-threshold file never rotates", not (d2 / "audit").exists())
chk("no-op: appends unchanged", _markers(_lines(p2)) == ["solo:0", "solo:1", "solo:2"])

# --- (3) over-size but all in-window: no-op pass, appends keep landing -----------------------------
d3 = tmp / "s3"
p3 = d3 / "audit.jsonl"
_seed(p3, [_row(f"fresh:{i}", 1.0) for i in range(20)])
log3 = _mk(p3, rotate_max_bytes=1, live_window_days=14)
log3.record("stress", marker="fresh:20")
log3.record("stress", marker="fresh:21")   # second append rides the no-op backoff path
chk("in-window: no archive created when nothing is old enough", not (d3 / "audit").exists())
chk("in-window: no rows lost through the no-op passes",
    _markers(_lines(p3)) == [f"fresh:{i}" for i in range(22)],
    f"{len(_lines(p3))} lines")

# --- (4) an undatable line is kept in the live file, never guessed into an archive -----------------
d4 = tmp / "s4"
p4 = d4 / "audit.jsonl"
BAD = 'this is not json {{{'
_seed(p4, [BAD] + [_row(f"old:{i}", OLD_DAYS) for i in range(10)]
          + [_row(f"recent:{i}", 1.0) for i in range(5)])
log4 = _mk(p4, rotate_max_bytes=1, live_window_days=14)
log4.record("fresh", marker="new:0")
live4 = _lines(p4)
arch4 = _lines(d4 / "audit" / f"archive-{old_month}.jsonl")
chk("undatable: malformed line survives in the live file", BAD in live4)
chk("undatable: malformed line never lands in an archive", BAD not in arch4)
chk("undatable: the old events around it still archive",
    _markers(arch4) == [f"old:{i}" for i in range(10)], f"{len(arch4)} archived")
chk("undatable: live = malformed + window + new",
    len(live4) == 1 + 5 + 1, f"{len(live4)} lines")

# --- (5) inter-process: concurrent appends while rotation fires lose nothing, duplicate nothing ----
d5 = tmp / "s5"
p5 = d5 / "audit.jsonl"
N_OLD = 300
_seed(p5, [_row(f"old:{i}", OLD_DAYS, pad=2000) for i in range(N_OLD)]
          + [_row(f"recent:{i}", 1.0) for i in range(5)])
PROCS, PROC_ROWS = 5, 10
child = (
    "import sys; sys.path.insert(0, '.');"
    "from orchestrator.audit import AuditLog;"
    "p, w, n = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]);"
    "log = AuditLog(p, rotate_max_bytes=1, live_window_days=14);"
    "[log.record('stress', worker=w, seq=i, marker=f'{w}:{i}') for i in range(n)]"
)
procs = [
    subprocess.Popen([sys.executable, "-c", child, str(p5), str(w), str(PROC_ROWS)],
                     cwd=os.getcwd())
    for w in range(PROCS)
]
rcs = [p.wait() for p in procs]
chk("procs: all child writers exited cleanly", all(rc == 0 for rc in rcs), str(rcs))

all5 = _lines(p5)
for af in sorted((d5 / "audit").glob("archive-*.jsonl")) if (d5 / "audit").is_dir() else []:
    all5 += _lines(af)
m5 = _markers(all5)
expected5 = ([f"old:{i}" for i in range(N_OLD)] + [f"recent:{i}" for i in range(5)]
             + [f"{w}:{i}" for w in range(PROCS) for i in range(PROC_ROWS)])
chk("procs: no rows lost across rotation + concurrent appends",
    set(m5) >= set(expected5),
    f"missing: {sorted(set(expected5) - set(m5))[:5]}")
chk("procs: no row duplicated by rotation",
    len(m5) == len(expected5), f"{len(m5)} != {len(expected5)}")
chk("procs: every line parses as JSON",
    all(None is not json.loads(x) for x in all5))

# --- (6) dashboard's EU-345 incremental reader survives a rotation (no stale splice) ---------------
from orchestrator import dashboard as D

d6 = tmp / "s6"
p6 = d6 / "audit.jsonl"
_seed(p6, [_row(f"old:{i}", OLD_DAYS) for i in range(30)]
          + [_row(f"recent:{i}", 1.0) for i in range(8)])
pre = D._read_file_lines(p6)          # warms the incremental cache at the pre-rotation offset
chk("reader: pre-rotation read sees the full file", len(pre) == 38, f"{len(pre)}")
log6 = _mk(p6, rotate_max_bytes=1, live_window_days=14)
log6.record("fresh", marker="new:0")
post = D._read_file_lines(p6)
chk("reader: post-rotation read equals the live tail exactly (full re-read, no splice)",
    post == _lines(p6), f"{len(post)} vs {len(_lines(p6))}")
chk("reader: no archived event leaks into the post-rotation read",
    not any(m and m.startswith("old:") for m in _markers(post)))


print("\n============= AUDIT ROTATION QA (EU-363) =============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
