"""EU-29: the cockpit must NOT re-parse the whole merged audit on every SSE frame / open tab.

render_board fans out to the audit three times per frame (load_tasks + warroom._run_in_flight +
warroom._scan). With a 50k-line audit and the SSE stream pushing ≥ every 2s, per tab, that used to be
a full re-read + JSON-split of the entire history several times a second. This proves the (size,
mtime_ns)-keyed TTL cache collapses a burst of frames/tabs to ≤1 real disk read per interval, that the
cache invalidates the instant the audit changes, and that _sync_html is cached too."""
import json
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path

sys.path.insert(0, ".")
_sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
_sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = _sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req

from orchestrator import warroom, dashboard as D
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
audit = tmp / "audit.jsonl"
from datetime import timedelta
def stamp(secs_ago=0):
    return (datetime.now().astimezone() - timedelta(seconds=secs_ago)).strftime("%Y-%m-%dT%H:%M:%S%z")
ts = stamp(0)

# A finished run + a NEWEST in-flight run (recent ticket_start, no terminal) so render_board takes the
# full 3× fan-out: load_tasks + warroom._run_in_flight (reads the audit) + warroom._scan (KPIs). Then
# ~50k filler audit lines so this is genuinely the "50k-line audit" the ticket calls out.
rows = [
    dict(event="ticket_start", ticket_id="AUTO-1", app="automatixy", branch="auto/AUTO-1", ts=stamp(600)),
    dict(event="build", ticket_id="AUTO-1", app="automatixy", iteration=1, turns=9,
         tools=["Read", "Edit"], summary="did it", ts=stamp(590)),
    dict(event="review", ticket_id="AUTO-1", iteration=1, verdict="PASS", summary="lgtm", ts=stamp(585)),
    dict(event="merged", ticket_id="AUTO-1", app="automatixy", ts=stamp(580)),
    dict(event="ticket_start", ticket_id="AUTO-7", app="automatixy", branch="auto/AUTO-7", ts=stamp(5)),
]
rows += [dict(event="heartbeat", seq=i, ts=ts) for i in range(50_000)]
audit.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
N_LINES = len(rows)

cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(audit), use_worktree=False)

# Pin the TTL high so the proof rests on (size, mtime_ns) invalidation, not wall-clock — a tight burst
# of frames must reuse one parse whether it takes 50ms or 5s.
D._AUDIT_TTL = 10_000.0

def _reset(clear_cache=False):
    if clear_cache:
        D._audit_cache.clear()
        D._tasks_cache.clear()
    D.audit_lines_calls = 0
    D.audit_lines_reads = 0

state = {}   # empty state = the cockpit didn't start the run (background/inflight path)

# --- 1. One COLD render reads the audit exactly once, despite fanning out to it 3× -----------------
_reset(clear_cache=True)
board = warroom.render_board(cfg, "automatixy", state, log_lines=[])
chk("cold render fans out to audit_lines ≥3× (load_tasks + _run_in_flight + _scan)",
    D.audit_lines_calls >= 3, f"calls={D.audit_lines_calls}")
chk("…but reads the 50k-line audit from disk only ONCE per frame",
    D.audit_lines_reads == 1, f"reads={D.audit_lines_reads}")
chk("render still correct (the in-flight run shows)", "AUTO-7" in board)

# --- 2. A burst of frames / K tabs (unchanged audit) shares ONE parse --------------------------------
_reset()                         # keep the warm cache; only reset the counters
FRAMES = 60                      # ~2 min of SSE frames, or many duplicate tabs hammering /api/stream
for _ in range(FRAMES):
    warroom.render_board(cfg, "automatixy", state, log_lines=[])
chk(f"{FRAMES} more frames called audit_lines many times (frame rate × tabs)",
    D.audit_lines_calls >= FRAMES, f"calls={D.audit_lines_calls}")
chk("≤1 parse per interval regardless of frame rate / tab count (0 re-reads while unchanged)",
    D.audit_lines_reads == 0, f"reads={D.audit_lines_reads}")

# also at the raw API level: load_tasks across the burst never re-parsed
_reset()
for _ in range(FRAMES):
    D.load_tasks(audit)
    D.audit_lines(audit)
chk("load_tasks/audit_lines served from cache across the burst (0 disk reads)",
    D.audit_lines_reads == 0, f"reads={D.audit_lines_reads}")

# --- 3. The cache INVALIDATES the instant the audit changes (correctness, not just speed) -----------
_reset()
prev_reads = D.audit_lines_reads
audit.write_text(audit.read_text(encoding="utf-8")
                 + "\n" + json.dumps(dict(event="ticket_start", ticket_id="AUTO-2",
                                          app="automatixy", branch="auto/AUTO-2", ts=ts)),
                 encoding="utf-8")
lines = D.audit_lines(audit)
chk("appending to the audit forces a fresh read (size/mtime changed)",
    D.audit_lines_reads == prev_reads + 1, f"reads delta={D.audit_lines_reads - prev_reads}")
chk("the freshly-appended event is present (no stale cache)",
    any("AUTO-2" in ln for ln in lines))
tasks = D.load_tasks(audit)
chk("load_tasks reflects the new run after the change",
    any(t.get("ticket_id") == "AUTO-2" for t in tasks))

# --- 4. _sync_html is cached (minute-precision badge, no per-frame glob+stat) ------------------------
warroom._SYNC_CACHE.clear()
s1 = warroom._sync_html(cfg)
chk("_sync_html cached after first call", str(cfg.audit_path) in warroom._SYNC_CACHE)
s2 = warroom._sync_html(cfg)
chk("_sync_html returns a stable value from cache", s1 == s2)

print("\n============== COCKPIT CACHE QA (EU-29) ==============")
print(f"  audit lines = {N_LINES}")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
