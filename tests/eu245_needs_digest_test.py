"""EU-245: the daily brief's 'Needs you' digest was an archaeology dump — a raw scan of the WHOLE
audit log's outcome field, with no dedup and no "is this actually resolved now" check. On 2026-07-11
it listed 99 items: massive per-ticket duplicates (AUTO-14 x5, AUTO-57 x4, EU-17 x3), tickets resolved
WEEKS earlier, and no bound. This harness pins the fix: dashboard.standup()'s 'Needs you' line now
sources from needs.summary(cfg) — the SAME deduped, live-state inbox the cockpit's Needs-you panel
already uses — dedupes by ticket, drops resolved/merged tickets, sorts oldest-first, and bounds to the
top 10 with an '…and N more' tail.

Written fail-first against the pre-fix raw scan (``needs = [t for t in tasks if t["outcome"] in
_NEEDS_YOU]``), which has none of those properties.
"""
import sys, types, json, tempfile
from datetime import datetime, timedelta
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

from orchestrator import dashboard, needs
from orchestrator.config import Config, AppConfig

NOW = datetime.now()


def _mk_cfg() -> Config:
    d = Path(tempfile.mkdtemp())
    (d / "audit.jsonl").write_text("")
    app = AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV", protected_branch="MAIN",
                    backlog_backend="none")
    cfg = Config(apps=[app], audit_path=str(d / "audit.jsonl"), use_worktree=False)
    needs.clear_cache()
    return cfg


def _task(tid, outcome, days_ago, app="automatixy", note=""):
    ts = (NOW - timedelta(days=days_ago)).replace(hour=12, minute=0, second=0, microsecond=0)
    return {"ticket_id": tid, "outcome": outcome, "app": app, "note": note,
            "started": ts, "ended": ts, "branch": "", "passes": 1, "turns": 1, "cost": 0.0,
            "verdict": None, "pr_url": None, "dry_run": None, "detail": {}}


# ============================================================================================
# 1) AUTO-14 x5 dedup + resolved-drop: 5 historical errored runs + a LATER merged→dev run.
#    'Needs you' must list AUTO-14 at most once, and must NOT list it at all (latest is resolved).
# ============================================================================================
cfg1 = _mk_cfg()
tasks1 = [_task("AUTO-14", "errored", days_ago=40 - i) for i in range(5)]
tasks1.append(_task("OTHER-1", "merged→dev", days_ago=45))          # unrelated noise, ignore
tasks1.append(_task("AUTO-14", "merged→dev", days_ago=10))          # LATEST run — resolved
dashboard.load_tasks = lambda p: tasks1
dashboard.load_dismissed = lambda p: {}
needs.clear_cache()
su1 = dashboard.standup(cfg1)
auto14_count = su1.count("AUTO-14")
chk("AUTO-14 (5 historical errors + later merged→dev) appears AT MOST ONCE in the digest",
    auto14_count <= 1, f"appeared {auto14_count}x: {su1}")
chk("AUTO-14's latest run is merged->dev, so it is EXCLUDED entirely (zero resolved entries)",
    "AUTO-14" not in su1.split("Needs you")[1].split("\n")[0] if "Needs you" in su1 else False,
    su1)

# ============================================================================================
# 2) The 2026-07-11 case reproduced: ~99 raw entries (heavy per-ticket dupes + tickets resolved
#    weeks ago + a couple of ghost-blocked tickets with no audit history at all) -> ~13 unique
#    currently-actionable items, live-sourced (needs.summary / blocked_tickets.json), never a raw
#    historical-outcome scan.
# ============================================================================================
cfg2 = _mk_cfg()
tasks2: list[dict] = []

# -- Group A: RESOLVED weeks ago (must be excluded entirely, 0 actionable) — heavy duplicate
#    errored runs from ~June, superseded by a later merged->dev run each.
RESOLVED = {"AUTO-9": 10, "AUTO-11": 10, "AUTO-12": 10, "AUTO-13": 10, "AUTO-14": 9}
for tid, n in RESOLVED.items():
    for i in range(n):
        tasks2.append(_task(tid, "errored", days_ago=50 + i))
    tasks2.append(_task(tid, "merged→dev", days_ago=10))            # later -> resolved

# -- Group B: still-actionable tickets WITH duplicates in the raw log (AUTO-57 x4, EU-17 x3 per
#    the real 2026-07-11 case) — latest run is still a needs-you outcome, so exactly ONE each.
for i in range(4):
    tasks2.append(_task("AUTO-57", "errored", days_ago=4 - i))
for i in range(3):
    tasks2.append(_task("EU-17", "escalated", days_ago=3 - i, app="Elite-Unit"))

# -- Group C1: 6 more unique actionable tickets via the audit, each with a padded stack of OLDER
#    duplicate entries (realistic "archaeology") that must NOT change which run is "latest".
C1 = ["AUTO-70", "AUTO-71", "AUTO-72", "EU-80", "EU-81", "EU-82"]
C1_OUTCOMES = ["errored", "escalated", "awaiting decision", "PR / needs you", "errored", "escalated"]
PADDED = {"AUTO-70", "AUTO-71", "EU-80", "EU-81"}
for tid, outcome in zip(C1, C1_OUTCOMES):
    tasks2.append(_task(tid, outcome, days_ago=1))                  # the LATEST (actionable) run
    if tid in PADDED:
        for i in range(8):
            tasks2.append(_task(tid, "errored", days_ago=20 + i))   # older duplicate noise

# -- Group C2: 5 more actionable tickets that are PURELY blocked (ghost — no audit history at
#    all), sourced only from blocked_tickets.json — exercises the "live state" merge explicitly.
#    Ids deliberately far from every other group's numbers (AUTO-9/11-14, AUTO-57, AUTO-70-72,
#    EU-17, EU-80-82) so a substring check (e.g. "AUTO-9" in "AUTO-90") can never false-positive.
C2 = ["AUTO-500", "AUTO-501", "EU-500", "EU-501", "EU-502"]
(Path(cfg2.audit_path).with_name("blocked_tickets.json")).write_text(
    json.dumps({tid: "budget exhausted" for tid in C2}), encoding="utf-8")

dashboard.load_tasks = lambda p: tasks2
dashboard.load_dismissed = lambda p: {}
needs.clear_cache()

RAW_TOTAL = len(tasks2)
chk("fixture reproduces the 2026-07-11 scale: ~99 raw entries", 90 <= RAW_TOTAL <= 105, str(RAW_TOTAL))

rows2 = dashboard._needs_you_rows(cfg2)
tids2 = [r["ticket_id"] for r in rows2]
chk("~99-entry archaeology collapses to ~13 unique currently-actionable items",
    11 <= len(rows2) <= 15, f"{len(rows2)} rows: {tids2}")
chk("every ticket id is unique (no duplicates survive)", len(tids2) == len(set(tids2)), str(tids2))
for tid in RESOLVED:
    chk(f"resolved ticket {tid} (latest run merged->dev) does not appear",
        tid not in tids2, str(tids2))
for tid in ["AUTO-57", "EU-17", *C1, *C2]:
    chk(f"currently-actionable ticket {tid} appears exactly once", tids2.count(tid) == 1, str(tids2))

su2 = dashboard.standup(cfg2)
chk("standup() text also excludes every resolved ticket",
    all(tid not in su2 for tid in RESOLVED), su2)
chk("standup() text carries the ghost-blocked tickets from blocked_tickets.json (live-state merge)",
    all(tid in su2 for tid in C2), su2)

# ============================================================================================
# 3) Bound to the top 10 + '…and N more' tail; a <=10 case renders everything with no tail.
# ============================================================================================
needs_line2 = next(l for l in su2.splitlines() if l.startswith("🟡 Needs you"))
chk("more than 10 actionable items exist in the fixture -> tail must be present",
    len(rows2) > dashboard._NEEDS_YOU_MAX, str(len(rows2)))
chk("Needs-you line renders exactly the top 10 ticket ids", sum(needs_line2.count(t) for t in tids2[:10]) >= 10
    and all(t in needs_line2 for t in tids2[:dashboard._NEEDS_YOU_MAX]), needs_line2)
chk("Needs-you line carries an '…and N more' tail naming the overflow count",
    f"and {len(rows2) - dashboard._NEEDS_YOU_MAX} more" in needs_line2, needs_line2)
chk("the tail carries a cockpit link to the full inbox", "/needs" in needs_line2, needs_line2)
overflow_ids = tids2[dashboard._NEEDS_YOU_MAX:]
chk("tickets past the top 10 are NOT individually named on the line",
    not any(t in needs_line2.replace(f"and {len(rows2) - dashboard._NEEDS_YOU_MAX} more", "")
            for t in overflow_ids), needs_line2)

# -- <=10 case: no tail, everything listed.
cfg3 = _mk_cfg()
tasks3 = [_task(f"AUTO-{100+i}", "errored", days_ago=i) for i in range(5)]
dashboard.load_tasks = lambda p: tasks3
dashboard.load_dismissed = lambda p: {}
needs.clear_cache()
su3 = dashboard.standup(cfg3)
needs_line3 = next(l for l in su3.splitlines() if l.startswith("🟡 Needs you"))
chk("<=10 actionable items -> every one is listed", all(f"AUTO-{100+i}" in needs_line3 for i in range(5)),
    needs_line3)
chk("<=10 actionable items -> no '…and N more' tail", "more (" not in needs_line3, needs_line3)

# ============================================================================================
# 4) Corridor small-talk stays retired: no active ceremony code path convenes it, while
#    config.smalltalk_model is RETAINED as the live Haiku cheap-tier knob (roster/notify still
#    resolve it) — mirrors scheduler_single_source_test.py's de-cron guard.
# ============================================================================================
ROOT = Path(__file__).resolve().parent.parent
from orchestrator.config import Config as _Cfg
_cfg_fields = {f.name for f in __import__("dataclasses").fields(_Cfg)}
chk("config.Config still declares smalltalk_model (the Haiku cheap-tier knob, NOT dead)",
    "smalltalk_model" in _cfg_fields, str(sorted(_cfg_fields)))

roster_src = (ROOT / "orchestrator" / "roster.py").read_text(encoding="utf-8")
notify_src = (ROOT / "orchestrator" / "notify.py").read_text(encoding="utf-8")
chk("roster.py still resolves cfg.smalltalk_model", "cfg.smalltalk_model" in roster_src)
chk("notify.py still resolves cfg.smalltalk_model", "smalltalk_model" in notify_src)

main_src = (ROOT / "orchestrator" / "main.py").read_text(encoding="utf-8")
chk("no 'general smalltalk' CLI command survives", '"smalltalk"' not in main_src)

NEEDLES = ("small_talk(", "_corridor_insight(", "convene_smalltalk", "corridor_convene")
offenders = []
for p in (ROOT / "orchestrator").glob("*.py"):
    text = p.read_text(encoding="utf-8", errors="ignore")
    hits = [n for n in NEEDLES if n in text]
    if hits:
        offenders.append((p.name, hits))
chk("no orchestrator source calls a corridor-convene / small-talk function (all deleted)",
    not offenders, str(offenders))

governor_src = (ROOT / "orchestrator" / "governor.py").read_text(encoding="utf-8")
# The OLD, misleading wording claimed the governor still caps corridor small-talk as a live,
# discretionary ceremony ("chatter (corridor small-talk, spontaneous meetings)"). That ceremony is
# gone (council.py's Phase-2 §2 marker) — a mention of it framed as DELETED/retired is fine (that's
# what documents the retirement, same as council.py's own marker); the LIVE-ceremony framing must not.
chk("governor.py no longer frames corridor small-talk as a live ceremony it currently caps",
    "chatter (corridor small-talk" not in governor_src.lower()
    and "caps the *discretionary*\nchatter" not in governor_src, governor_src[:400])
chk("governor.py documents corridor small-talk's retirement (not silently scrubbed, just corrected)",
    "corridor small-talk" in governor_src.lower() and "delete" in governor_src.lower(), governor_src[:600])

print("\n============ EU-245 NEEDS-YOU DIGEST QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
