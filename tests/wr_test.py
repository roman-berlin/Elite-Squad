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
print("RUN_all", warroom.active_run(cfg, tasks, None, True))
print("AUTO-12 in board_all?", "AUTO-12" in board_all)
print("AUTO-13 in board_sd?", "AUTO-13" in board_sd)
print("AUTO-12 in board_sd?", "AUTO-12" in board_sd)

assert "War Room" in page, "title missing"
assert "roster" in page, "roster missing"
assert "BAR" in page, "control bar not injected"
assert "Merged" in page, "kpi missing"
assert "AUTO-13" in board_sd, "signaldesk active run missing"
assert "AUTO-12" not in board_sd, "scope leak: automatixy task showed under signaldesk"
print("OK")
