"""EU-337 — reviewer-findings escalations must page a PLAIN-LANGUAGE decision, not raw bullets.

Roman 2026-07-15: "The 'needs you' is not convenient — less code language; briefly tell me the problem
+ 3 solutions, one recommended (like Claude Code does)." The GOOD format (EU-327) is problem + options
+ Rec; the BAD one (EU-330) pasted raw reviewer prose + code identifiers (_state.get("drilling")…) with
no options and no recommendation. Root cause: the PM-findings/reviewer-decisions escalation paged the
raw `bullets`, while every OTHER escalation site routes through _decision_brief (which distils to the
problem + ≤3 options + 'Rec:' format).

Pins:
  (1) _decision_brief distils raw reviewer notes into the phone format (first line = the decision,
      then option bullets, last line = 'Rec: ...') — stubbing the cheap model;
  (2) it fails safe to the rule-based brief when the model errors (an escalation is never lost);
  (3) source: the reviewer-findings escalation pages via _decision_brief, NOT the raw bullets.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import patch

sdk = types.ModuleType("claude_agent_sdk")
sdk.__getattr__ = lambda n: (lambda *a, **k: None)
sys.modules.setdefault("claude_agent_sdk", sdk)

sys.path.insert(0, ".")

from orchestrator import loop  # noqa: E402

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


class _Cfg:
    pass


RAW = ("Reviewer raised scope/product ambiguities that need your call:\n"
       "• [blocker/tests] Builder's handoff says the test suite run was still in progress, and per the "
       "EU-249 rule an unconfirmed test run is always blocking; _state.get('drilling') / "
       "_state['drilling'] = True; _STATE_KEYS/_new_state() were referenced.")

BRIEF = ("Ship EU-330 now or require a confirmed green suite first?\n"
         "• Require: re-run the suite to green before landing\n"
         "• Ship: accept the unconfirmed run as low-risk\n"
         "Rec: Require — an unconfirmed suite is blocking per EU-249.")


# (1) _decision_brief distils to the phone format (cheap model stubbed via notify.distill)
async def _fake_distill(cfg, system, user, tag, fallback):
    # assert the reviewer notes reached the distiller, then return a well-formed brief
    assert "drilling" in user  # the raw notes are the input
    return BRIEF

with patch.object(loop.notify, "distill", _fake_distill):
    out = asyncio.run(loop._decision_brief(_Cfg(), "EU-330", RAW))
ok("(1) distils to first-line decision + option bullets + Rec line",
   out.splitlines()[0].endswith("?") and "• " in out and "Rec:" in out, f"got {out!r}")
ok("(1b) it does NOT just echo the raw code identifiers",
   "_state.get" not in out and "_STATE_KEYS" not in out, f"got {out!r}")

# (2) model error → fail safe to the rule-based brief (escalation never lost)
async def _boom_distill(cfg, system, user, tag, fallback):
    return fallback()   # notify.distill calls fallback on model error

with patch.object(loop.notify, "distill", _boom_distill):
    out2 = asyncio.run(loop._decision_brief(_Cfg(), "EU-330", RAW))
ok("(2) fails safe to a non-empty brief when the model errors", bool(out2.strip()), f"got {out2!r}")

# empty raw → empty (nothing to page)
ok("(2b) empty raw → empty", asyncio.run(loop._decision_brief(_Cfg(), "EU-1", "")) == "")

# (3) source pin: the reviewer-findings escalation pages via _decision_brief, not raw bullets
src = Path("orchestrator/loop.py").read_text()
ok("(3) reviewer-findings escalation uses _decision_brief for the phone ping",
   "needs YOUR decision:\\n" in src
   and "_decision_brief(cfg, ticket.id, question)" in src)
ok("(3b) it no longer pastes the raw reviewer bullets in that ping",
   'reviewer finding(s)):\\n{bullets}' not in src)

print(f"\n{checks}/{checks} passed")
