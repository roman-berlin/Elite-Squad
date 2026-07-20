"""Smoke test for the War Room data + render, against a synthetic audit log."""
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

d = Path(tempfile.mkdtemp())
audit = d / "audit.jsonl"
now = datetime.now().astimezone()
ts = now.strftime("%Y-%m-%dT%H:%M:%S%z")   # matches audit.py exactly
rows = [
    dict(event="ticket_start", ticket_id="AUTO-12", app="automatixy", branch="auto/AUTO-12", ts=ts),
    dict(event="build", ticket_id="AUTO-12", app="automatixy", iteration=1, turns=12,
         cost_usd=0.0, effort="high", tools=["Read", "Edit"], summary="did it", ts=ts),
    dict(event="review", ticket_id="AUTO-12", iteration=1, verdict="PASS", summary="lgtm", ts=ts),
    dict(event="merged", ticket_id="AUTO-12", app="automatixy", ts=ts),
    dict(event="ticket_start", ticket_id="AUTO-13", app="signaldesk", branch="auto/AUTO-13", ts=ts),
    dict(event="build", ticket_id="AUTO-13", app="signaldesk", iteration=1, turns=4,
         tools=["Read"], summary="wip", ts=ts),
    dict(event="security_block", ticket_id="AUTO-09", app="automatixy", ts=ts),
]
audit.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

ns = types.SimpleNamespace
cfg = ns(audit_path=str(audit), apps=[ns(name="automatixy"), ns(name="signaldesk")])

from orchestrator import warroom

tasks = warroom.D.load_tasks(cfg.audit_path)
page = warroom.render_page(cfg, None, {"active": True}, "<div class=bar>BAR</div>", {"ok": True, "checks": []})
board_all = warroom.render_board(cfg, None, {"active": True})
board_sd = warroom.render_board(cfg, "signaldesk", {"active": True})

print("PAGE_LEN", len(page), "BOARD_LEN", len(board_all))
print("KPIS", [(c["label"], c["value"], c.get("tone")) for c in warroom.kpis(cfg, tasks, None)])
print("ROSTER", [(r["name"], r["dot"], r["last"]) for r in warroom.roster(cfg, tasks, True)])
print("FEED_all", [(f["ticket"], f["tone"], f["text"][:34]) for f in warroom.feed(cfg, tasks, None)])
run_obj = warroom.active_run(cfg, tasks, None, True)
print("RUN_all", run_obj)
print("AUTO-12 in board_all?", "AUTO-12" in board_all)
print("AUTO-13 in board_sd?", "AUTO-13" in board_sd)
print("AUTO-12 in board_sd?", "AUTO-12" in board_sd)

assert "SQUAD" in page and "HQ" in page, "brand title missing (SQUAD rebrand 2026-07-21)"
assert "roster" in page, "roster missing"
assert "BAR" in page, "control bar not injected"
assert "Merged" in page, "kpi missing"
assert "AUTO-13" in board_sd, "signaldesk active run missing"
assert "AUTO-12" not in board_sd, "scope leak: automatixy task showed under signaldesk"

# EU-55 / F12: active_run() must expose the phase metadata the phasebar renders from.
assert "phases" in run_obj and run_obj["phases"] == list(warroom.PHASES), \
    "EU-55: phases list in active_run() must match the PHASES constant"
assert "reached" in run_obj and isinstance(run_obj["reached"], int), \
    "EU-55: reached must be an int index into phases"
assert "failed_phase" in run_obj, "EU-55: failed_phase key missing from active_run()"

# EU-76: active_run() must include a sparkline list for the trend chart.
assert "sparkline" in run_obj, "EU-76: sparkline key missing from active_run()"
assert isinstance(run_obj["sparkline"], list), "EU-76: sparkline must be a list"
# Only completed (merged→dev) runs appear; AUTO-12 merged, AUTO-13 still in flight.
assert all(isinstance(p, int) for p in run_obj["sparkline"]), \
    "EU-76: sparkline values must all be ints"

# EU-76: _run_html() must render sparkline HTML when data has ≥2 points.
# Build a synthetic run_obj with two sparkline points to exercise the chart path.
import copy
two_point_run = copy.copy(run_obj)
two_point_run["sparkline"] = [1, 3]
spark_html = warroom._run_html(two_point_run)
assert "passes trend" in spark_html, "EU-76: sparkline label missing from _run_html()"
assert "<svg" in spark_html and "<polyline" in spark_html, \
    "EU-76: SVG sparkline element missing from _run_html()"
assert "polyline" in spark_html, "EU-76: sparkline polyline missing"

# EU-55: _run_html() must inject the now::after gradient CSS when the run is live.
live_run = copy.copy(run_obj)
live_run["live"] = True
live_run["reached"] = 1  # Gate is "now"
live_html = warroom._run_html(live_run)
assert "ph.now::after" in live_html, \
    "EU-55: now-phase connector CSS not injected by _run_html() for live runs"
assert "linear-gradient" in live_html, \
    "EU-55: now-phase connector gradient animation missing in live run HTML"

# Idle runs (live=False) must NOT inject the now-connector CSS (it would be misleading).
idle_run = copy.copy(run_obj)
idle_run["live"] = False
idle_html = warroom._run_html(idle_run)
assert "ph.now::after" not in idle_html, \
    "EU-55: now-connector CSS must not appear in idle (non-live) run HTML"

# EU-33: a long feed note is trimmed to a word boundary + ellipsis, not a mid-word cut.
long_note = ("The reviewer blocked this run because the tenant filter was missing on the "
             "leads query and several types were loosened to any during the build")
errored = [dict(ticket_id="AUTO-99", app="automatixy", outcome="errored",
                ended=now, started=now, note=long_note)]
fnote = next(f for f in warroom.feed(cfg, errored, None) if f["ticket"] == "AUTO-99")["text"]
print("FEED_trim", repr(fnote))
assert fnote.endswith("…"), "long feed note should end with an ellipsis"
assert " any" not in fnote and "…" in fnote, "feed note should be trimmed before the end"
assert "wa…" not in fnote and "missin…" not in fnote, "feed note cut mid-word"
# the kept text (sans trailing …) must end on a whole word from the source
kept = fnote.split("—", 1)[1].strip().rstrip("…").rstrip()
assert long_note.startswith(kept), "trim is not a clean prefix of the original note"
assert kept.split()[-1] in long_note.split(), "feed note cut mid-word"

# EU-35: the health pill appends "· N warnings" in BOTH branches. An unhealthy summary with
# warnings must surface them in the label (matching the healthy branch), not hide them behind the
# dropdown. Singular/plural is respected on each count.
unhealthy_warns = {"healthy": False, "checks": [
    {"status": "bad", "name": "db", "detail": ""},
    {"status": "bad", "name": "auth", "detail": ""},
    {"status": "warn", "name": "cache", "detail": ""},
]}
pill = warroom.health_pill(unhealthy_warns)
print("HPILL_unhealthy", repr(pill[:90]))
assert "2 problems · 1 warning" in pill, "unhealthy pill must show the warning count"
assert "1 warnings" not in pill, "warning count must be singular for one warning"

# Single problem + multiple warnings, still unhealthy — plural warnings, singular problem.
one_prob = {"healthy": False, "checks": [
    {"status": "bad", "name": "db", "detail": ""},
    {"status": "warn", "name": "cache", "detail": ""},
    {"status": "warn", "name": "disk", "detail": ""},
]}
assert "1 problem · 2 warnings" in warroom.health_pill(one_prob), "plural warnings on unhealthy pill"

# Healthy branch is unchanged (regression guard).
healthy_warn = {"healthy": True, "checks": [{"status": "warn", "name": "cache", "detail": ""}]}
assert "System healthy · 1 warning" in warroom.health_pill(healthy_warn), "healthy pill warning suffix regressed"

# No warnings → no suffix in either branch.
no_warn = {"healthy": False, "checks": [{"status": "bad", "name": "db", "detail": ""}]}
assert "warning" not in warroom.health_pill(no_warn), "no-warning pill must not mention warnings"

print("OK")
