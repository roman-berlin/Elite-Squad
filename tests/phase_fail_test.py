"""EU-28: a terminal-but-FAILED run must light its STOPPING phase red, not render the phases
behind it as cleanly-done. Fixture audit with errored tickets → active_run.failed_phase + render."""
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

now = datetime.now().astimezone()
ts = now.strftime("%Y-%m-%dT%H:%M:%S%z")
ns = types.SimpleNamespace
from orchestrator import warroom


def _audit(rows):
    d = Path(tempfile.mkdtemp())
    a = d / "audit.jsonl"
    a.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return ns(audit_path=str(a), apps=[ns(name="automatixy")])


# --- Case 1: errored run whose review verdict is FAIL -> Review (index 3) red --------------------
# (EU-55: Review moved from index 2 to 3 once the Tests/Security phases joined the shared bar.)
cfg = _audit([
    dict(event="ticket_start", ticket_id="AUTO-90", app="automatixy", branch="auto/AUTO-90", ts=ts),
    dict(event="build", ticket_id="AUTO-90", app="automatixy", iteration=1, turns=8,
         tools=["Read", "Edit"], summary="built", ts=ts),
    dict(event="review", ticket_id="AUTO-90", iteration=1, verdict="FAIL", summary="nope", ts=ts),
    dict(event="ticket_exception", ticket_id="AUTO-90", app="automatixy", error="boom", ts=ts),
])
tasks = warroom.D.load_tasks(cfg.audit_path)
run = warroom.active_run(cfg, tasks, None, False)   # active=False -> this is a finished 'last run'
html_fail = warroom._run_html(run, mode="live", manual=False)

# --- Case 2: gate failure (built, errored before review) -> Gate (index 1) red -------------------
cfg2 = _audit([
    dict(event="ticket_start", ticket_id="AUTO-91", app="automatixy", branch="auto/AUTO-91", ts=ts),
    dict(event="build", ticket_id="AUTO-91", app="automatixy", iteration=1, turns=3,
         tools=["Read"], summary="built", ts=ts),
    dict(event="ticket_exception", ticket_id="AUTO-91", app="automatixy", error="gate failed", ts=ts),
])
tasks2 = warroom.D.load_tasks(cfg2.audit_path)
run2 = warroom.active_run(cfg2, tasks2, None, False)
html_gate = warroom._run_html(run2, mode="live", manual=False)

# --- Case 3: cleanly merged run -> no failed node ------------------------------------------------
cfg3 = _audit([
    dict(event="ticket_start", ticket_id="AUTO-92", app="automatixy", branch="auto/AUTO-92", ts=ts),
    dict(event="build", ticket_id="AUTO-92", app="automatixy", iteration=1, turns=5,
         tools=["Edit"], summary="built", ts=ts),
    dict(event="review", ticket_id="AUTO-92", iteration=1, verdict="PASS", summary="lgtm", ts=ts),
    dict(event="merged", ticket_id="AUTO-92", app="automatixy", ts=ts),
])
tasks3 = warroom.D.load_tasks(cfg3.audit_path)
run3 = warroom.active_run(cfg3, tasks3, None, False)
html_ok = warroom._run_html(run3, mode="live", manual=False)

print("Case1 failed_phase", run.get("failed_phase"))
print("Case2 failed_phase", run2.get("failed_phase"))
print("Case3 failed_phase", run3.get("failed_phase"))

checks = []
def chk(name, cond):
    checks.append(cond)
    print(("  ok " if cond else "  XX ") + name)

# Acceptance: review-FAIL lights Review red, gate failure lights Gate red.
chk("review-FAIL -> failed_phase == 3 (Review)", run.get("failed_phase") == 3)
chk("review-FAIL renders Review node red", '<div class="ph failed"><span></span>Review</div>' in html_fail)
chk("review-FAIL does NOT mark Review as done", '<span></span>Review</div>' not in html_fail.replace(
    '<div class="ph failed"><span></span>Review</div>', ""))
chk("gate failure -> failed_phase == 1 (Gate)", run2.get("failed_phase") == 1)
chk("gate failure renders Gate node red", '<div class="ph failed"><span></span>Gate</div>' in html_gate)
chk("gate failure keeps Build done", '<div class="ph done"><span></span>Build</div>' in html_gate)
chk("merged run has no failed_phase", run3.get("failed_phase") is None)
chk("merged run renders no failed node", '"ph failed"' not in html_ok)
chk("failed CSS rule present in page", ".phasebar .ph.failed" in warroom.render_page(
    cfg, None, {"active": False}, "<div>BAR</div>", {"ok": True, "checks": []}))

ok = sum(1 for c in checks if c)
print(f"{ok}/{len(checks)} passed")
assert ok == len(checks), "EU-28 phase-fail rendering broken"
print("OK")
