"""EU-55 / F12: the terminal phase bar (loop._bar) and the War Room web phase bar
(warroom.active_run) must derive from ONE shared PHASES constant, so they can never
drift apart again.

Guards three things:
  1. single source of truth — loop and warroom both bind the same phases.PHASES object;
  2. the pipeline phases are present, in pipeline order (Phase-2 §2: the Security phase
     AND the Test Engineer 'Tests' phase were removed — Build → Gate → Review → Land);
  3. the reached / failed_phase index logic matches the phase count/order (no drift).
"""
import io
import json
import sys
import tempfile
import types
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

sys.path.insert(0, ".")
_sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
_sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = _sdk

from orchestrator import loop, phases, warroom

ns = types.SimpleNamespace
now = datetime.now().astimezone()
ts = now.strftime("%Y-%m-%dT%H:%M:%S%z")


def _cfg(rows):
    d = Path(tempfile.mkdtemp())
    a = d / "audit.jsonl"
    a.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return ns(audit_path=str(a), apps=[ns(name="automatixy")])


def _run(rows, *, live):
    cfg = _cfg(rows)
    return warroom.active_run(cfg, warroom.D.load_tasks(cfg.audit_path), None, live)


checks = []
def chk(name, cond):
    checks.append(bool(cond))
    print(("  ok " if cond else "  XX ") + name)


# --- 1) Single source of truth -----------------------------------------------------------------
PH = phases.PHASES
# Phase-2 §2 (2026-07-06): the LLM per-diff Security phase and the Test Engineer 'Tests' coverage
# phase were both removed — 4 phases now (the Builder writes tests against the Planner's AC and the
# deterministic gate runs them).
chk("PHASES is the exact pipeline order", PH == ("Build", "Gate", "Review", "Land"))
chk("loop._bar derives from the shared PHASES (identity)", loop.PHASES is phases.PHASES)
chk("warroom bar derives from the shared PHASES (identity)", warroom.PHASES is phases.PHASES)

# --- 2) Removed phases stay removed; survivors are in the right order ----------------------------
chk("Test Engineer 'Tests' phase removed (Phase-2 §2)", "Tests" not in PH)
chk("Security phase removed (Phase-2 §2)", "Security" not in PH)
chk("Gate runs between Build and Review", PH.index("Build") < PH.index("Gate") < PH.index("Review"))
chk("Review runs just before Land", PH.index("Review") < PH.index("Land"))

# --- 3) Both bars render the SAME ordered phases from that one constant --------------------------
# Web bar: active_run hands the template list(PHASES).
web = _run([dict(event="ticket_start", ticket_id="AUTO-1", app="automatixy", branch="b", ts=ts),
            dict(event="build", ticket_id="AUTO-1", app="automatixy", iteration=1, tools=["Edit"],
                 summary="built", ts=ts)], live=True)
chk("web bar phases == list(PHASES)", web["phases"] == list(PH))

# Terminal bar: loop._bar prints every phase name from PHASES, in order.
buf = io.StringIO()
with redirect_stdout(buf):
    loop._bar(loop.BUILD, active=loop.BUILD)
term = buf.getvalue()
chk("terminal bar prints every phase name", all(p in term for p in PH))
chk("terminal bar prints no removed phases", "Tests" not in term and "Security" not in term)
chk("terminal bar phase order matches PHASES",
    [term.index(p) for p in PH] == sorted(term.index(p) for p in PH))

# --- 4) reached / failed_phase indices match the 4-phase order (drift guard) ---------------------
# build done + live, NO gate event yet -> still Building (EU-448: the bar must NOT jump to Gate
# the moment a build event exists; Gate is reached only once the gate phase actually fires).
chk("build-only live -> reached == Build index (EU-448)", web["reached"] == PH.index("Build"))

# reviewed + live -> Build, Gate, Review behind us; Land is next.
reviewed = _run([dict(event="ticket_start", ticket_id="AUTO-2", app="automatixy", branch="b", ts=ts),
                 dict(event="build", ticket_id="AUTO-2", app="automatixy", iteration=1, tools=["Edit"],
                      summary="built", ts=ts),
                 dict(event="review", ticket_id="AUTO-2", iteration=1, verdict="PASS", summary="ok", ts=ts)],
                live=True)
chk("has_review (live) -> reached == Land index", reviewed["reached"] == PH.index("Land"))

# merged -> every phase complete (reached == len), nothing failed.
merged = _run([dict(event="ticket_start", ticket_id="AUTO-3", app="automatixy", branch="b", ts=ts),
               dict(event="build", ticket_id="AUTO-3", app="automatixy", iteration=1, tools=["Edit"],
                    summary="built", ts=ts),
               dict(event="review", ticket_id="AUTO-3", iteration=1, verdict="PASS", summary="ok", ts=ts),
               dict(event="merged", ticket_id="AUTO-3", app="automatixy", ts=ts)], live=False)
chk("merged -> reached == len(PHASES)", merged["reached"] == len(PH))
chk("merged -> no failed_phase", merged["failed_phase"] is None)

# review-FAIL errored -> Review lights red at its NEW index (2, after Tests was removed).
rev_fail = _run([dict(event="ticket_start", ticket_id="AUTO-4", app="automatixy", branch="b", ts=ts),
                 dict(event="build", ticket_id="AUTO-4", app="automatixy", iteration=1, tools=["Edit"],
                      summary="built", ts=ts),
                 dict(event="review", ticket_id="AUTO-4", iteration=1, verdict="FAIL", summary="no", ts=ts),
                 dict(event="ticket_exception", ticket_id="AUTO-4", app="automatixy", error="x", ts=ts)],
                live=False)
chk("review-FAIL -> failed_phase == Review index (2)", rev_fail["failed_phase"] == PH.index("Review") == 2)

# built-but-not-reviewed errored -> Gate lights red (index 1, unchanged).
gate_fail = _run([dict(event="ticket_start", ticket_id="AUTO-5", app="automatixy", branch="b", ts=ts),
                  dict(event="build", ticket_id="AUTO-5", app="automatixy", iteration=1, tools=["Read"],
                       summary="built", ts=ts),
                  dict(event="ticket_exception", ticket_id="AUTO-5", app="automatixy", error="x", ts=ts)],
                 live=False)
chk("built-not-reviewed -> failed_phase == Gate index (1)", gate_fail["failed_phase"] == PH.index("Gate") == 1)

ok = sum(1 for c in checks if c)
print(f"{ok}/{len(checks)} passed")
assert ok == len(checks), "EU-55 shared-phase-bar regression failed"
print("OK")
