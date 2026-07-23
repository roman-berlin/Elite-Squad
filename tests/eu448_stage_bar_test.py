"""EU-448: the stage bar must reflect the CURRENT phase of a LIVE run, not jump ahead to the
next phase the moment a build audit event exists.

Root cause: ``active_run`` derived ``reached`` from ``has_build`` (warroom.py), which flips True
as soon as pass 1's ``build`` event exists and never resets — so a live build sat on "Gate" for
its whole life, including PM-driven continuation passes (``pm_review`` / ``pm_decided`` emit no
gate event and set no verdict). This is the same display-lie class as AUTO-198 ("Working · Land
next to verdict FAIL"): a status that is confidently wrong is worse than one that is vague.

Fix: branch the LIVE derivation off the gate-phase signal ``t["phase"]`` (the LATEST build/gate
event from ``dashboard.load_tasks``), leaving the idle/last-run structural path untouched. Pins:
  build + builder alive        -> Build
  build + gate event fired     -> Gate
  passing review               -> Land
  FAIL verdict on a live run   -> Build
  merged                       -> all phases complete
"""
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

from orchestrator import loop, phases, warroom

ns = types.SimpleNamespace
now = datetime.now().astimezone()
ts = now.strftime("%Y-%m-%dT%H:%M:%S%z")
PH = phases.PHASES


def _cfg(rows):
    d = Path(tempfile.mkdtemp())
    a = d / "audit.jsonl"
    a.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return ns(audit_path=str(a), apps=[ns(name="automatixy")])


def _run(rows, *, live):
    cfg = _cfg(rows)
    return warroom.active_run(cfg, warroom.D.load_tasks(cfg.audit_path), None, live)


def _html(run):
    return warroom._run_html(run, mode="live", manual=False)


checks = []
def chk(name, cond):
    checks.append(bool(cond))
    print(("  ok " if cond else "  XX ") + name)


# --- AC1 (the reported bug): a LIVE build with NO gate event -> Build, not Gate -----------------
# EU-438: ticket_start + build, builder still writing files. `has_build` was True so the bar
# jumped to Gate. The fix reads the gate-phase signal, which is still "build" here.
ac1 = _run([
    dict(event="ticket_start", ticket_id="EU-438", app="automatixy", branch="b", ts=ts),
    dict(event="build", ticket_id="EU-438", app="automatixy", iteration=1, tools=["Edit"],
         summary="built", ts=ts),
], live=True)
h1 = _html(ac1)
chk("AC1: live build -> reached == Build index (0)", ac1["reached"] == PH.index("Build") == 0)
chk("AC1: Build node marked 'now'", '<div class="ph now"><span></span>Build</div>' in h1)
chk("AC1: Gate node NOT done", '<div class="ph done"><span></span>Gate</div>' not in h1)
chk("AC1: Gate node NOT now", '<div class="ph now"><span></span>Gate</div>' not in h1)

# --- AC2 (PM continuation pass): build + pm_review + pm_decided, no gate -> Build ----------------
# The exact EU-438 scenario: the bar said Gate while the Builder wrote files 31 minutes later.
# pm_review / pm_decided emit no gate event and set no verdict (load_tasks only reads a `review`
# event's verdict), so the bar must stay on Build for the whole continuation pass.
ac2 = _run([
    dict(event="ticket_start", ticket_id="EU-438", app="automatixy", branch="b", ts=ts),
    dict(event="build", ticket_id="EU-438", app="automatixy", iteration=1, tools=["Edit"],
         summary="built", ts=ts),
    dict(event="pm_review", ticket_id="EU-438", iteration=1, verdict="CONTINUE", ts=ts),
    dict(event="pm_decided", ticket_id="EU-438", iteration=1, automode=True, ts=ts),
], live=True)
chk("AC2: PM continuation -> reached == Build index", ac2["reached"] == PH.index("Build"))

# --- AC3 (Gate pinned to the gate event) -------------------------------------------------------
# Only once the `gate` phase actually fired does the bar reach Gate. Adding deterministic_gate
# (which never resets `phase`) keeps it on Gate — still the gate phase, no review yet. A
# build-only live run reaching Gate is caught by AC1.
ac3 = _run([
    dict(event="ticket_start", ticket_id="EU-3", app="automatixy", branch="b", ts=ts),
    dict(event="build", ticket_id="EU-3", app="automatixy", iteration=1, tools=["Edit"],
         summary="built", ts=ts),
    dict(event="gate", ticket_id="EU-3", iteration=1, passed=True, ts=ts),
], live=True)
chk("AC3: gate event fired -> reached == Gate index", ac3["reached"] == PH.index("Gate"))

ac3b = _run([
    dict(event="ticket_start", ticket_id="EU-3b", app="automatixy", branch="b", ts=ts),
    dict(event="build", ticket_id="EU-3b", app="automatixy", iteration=1, tools=["Edit"],
         summary="built", ts=ts),
    dict(event="gate", ticket_id="EU-3b", iteration=1, passed=True, ts=ts),
    dict(event="deterministic_gate", ticket_id="EU-3b", iteration=1, passed=True, ts=ts),
], live=True)
chk("AC3: + deterministic_gate keeps reached == Gate index", ac3b["reached"] == PH.index("Gate"))

# --- AC4 (AUTO-198 regression guard): FAIL verdict on a live run -> Build, not Land -------------
ac4 = _run([
    dict(event="ticket_start", ticket_id="AUTO-198", app="automatixy", branch="b", ts=ts),
    dict(event="build", ticket_id="AUTO-198", app="automatixy", iteration=1, tools=["Edit"],
         summary="built", ts=ts),
    dict(event="gate", ticket_id="AUTO-198", iteration=1, passed=True, ts=ts),
    dict(event="review", ticket_id="AUTO-198", iteration=1, verdict="FAIL", summary="no", ts=ts),
], live=True)
chk("AC4: live FAIL verdict -> reached == Build index", ac4["reached"] == PH.index("Build"))

# --- Pin: passing review -> Land; merged -> all phases complete ---------------------------------
ac5 = _run([
    dict(event="ticket_start", ticket_id="EU-5", app="automatixy", branch="b", ts=ts),
    dict(event="build", ticket_id="EU-5", app="automatixy", iteration=1, tools=["Edit"],
         summary="built", ts=ts),
    dict(event="gate", ticket_id="EU-5", iteration=1, passed=True, ts=ts),
    dict(event="review", ticket_id="EU-5", iteration=1, verdict="PASS", summary="ok", ts=ts),
], live=True)
chk("Pin: passing review -> reached == Land index", ac5["reached"] == PH.index("Land"))

ac5m = _run([
    dict(event="ticket_start", ticket_id="EU-5m", app="automatixy", branch="b", ts=ts),
    dict(event="build", ticket_id="EU-5m", app="automatixy", iteration=1, tools=["Edit"],
         summary="built", ts=ts),
    dict(event="review", ticket_id="EU-5m", iteration=1, verdict="PASS", summary="ok", ts=ts),
    dict(event="merged", ticket_id="EU-5m", app="automatixy", ts=ts),
], live=True)
chk("Pin: merged (live) -> reached == len(PHASES)", ac5m["reached"] == len(PH))
chk("Pin: merged (live) -> failed_phase is None", ac5m["failed_phase"] is None)

# --- AC5 (one source of truth): the idle/last-run path is unchanged, and the identity pin holds -
# These are the phase_fail_test.py cases, re-asserted here so the EU-448 fix can't quietly regress
# the structural path while fixing the live path.
idle_rev = _run([
    dict(event="ticket_start", ticket_id="EU-r", app="automatixy", branch="b", ts=ts),
    dict(event="build", ticket_id="EU-r", app="automatixy", iteration=1, tools=["Edit"],
         summary="b", ts=ts),
    dict(event="review", ticket_id="EU-r", iteration=1, verdict="FAIL", summary="no", ts=ts),
    dict(event="ticket_exception", ticket_id="EU-r", app="automatixy", error="x", ts=ts),
], live=False)
chk("AC5: idle review-FAIL -> failed_phase == Review (2)",
    idle_rev["failed_phase"] == PH.index("Review") == 2)

idle_gate = _run([
    dict(event="ticket_start", ticket_id="EU-g", app="automatixy", branch="b", ts=ts),
    dict(event="build", ticket_id="EU-g", app="automatixy", iteration=1, tools=["Read"],
         summary="b", ts=ts),
    dict(event="ticket_exception", ticket_id="EU-g", app="automatixy", error="x", ts=ts),
], live=False)
chk("AC5: idle built-not-reviewed -> failed_phase == Gate (1)",
    idle_gate["failed_phase"] == PH.index("Gate") == 1)

idle_merged = _run([
    dict(event="ticket_start", ticket_id="EU-m", app="automatixy", branch="b", ts=ts),
    dict(event="build", ticket_id="EU-m", app="automatixy", iteration=1, tools=["Edit"],
         summary="b", ts=ts),
    dict(event="review", ticket_id="EU-m", iteration=1, verdict="PASS", summary="ok", ts=ts),
    dict(event="merged", ticket_id="EU-m", app="automatixy", ts=ts),
], live=False)
chk("AC5: idle merged -> no failed_phase", idle_merged["failed_phase"] is None)

# AC5: the shared-source identity pins still hold (loop and warroom bind phases.PHASES).
chk("AC5: loop.PHASES is phases.PHASES", loop.PHASES is phases.PHASES)
chk("AC5: warroom.PHASES is phases.PHASES", warroom.PHASES is phases.PHASES)
chk("AC5: PHASES == Build/Gate/Review/Land", PH == ("Build", "Gate", "Review", "Land"))

ok = sum(1 for c in checks if c)
print(f"{ok}/{len(checks)} passed")
assert ok == len(checks), "EU-448 stage-bar regression failed"
print("OK")
