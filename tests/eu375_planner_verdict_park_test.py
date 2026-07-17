"""Planner CLOSE/ANSWER/REFILE without code evidence must PARK, not burn a full build (2026-07-17).

Live proof (AUTO-57, 2026-07-16 16:57–18:10): the Planner correctly verdicted CLOSE — the work had
landed 2026-07-07 — the loop recorded `planner_nonbuild_verdict` and then BUILT IT ANYWAY, burning
1.5M tokens into the 60-minute wall-clock cap before erroring. EU-225 only auto-closes when the
verdict is double-keyed with provable code evidence (`_already_landed`); AUTO-57's landing carried
no ticket key in its commit, so it fell through to "building anyway (conservative)".

The conservatism is right about one thing: a wrong AUTO-CLOSE is the senior_pm mistake §2 undid, so
this never closes anything. It parks the ticket with the Planner's reason for the Commander, which
costs one cheap Planner call instead of a full build.

Loop-safety: a park would otherwise cycle forever (Commander /unblocks → Planner re-verdicts CLOSE →
parks again). So the park fires ONCE per ticket — a prior planner_verdict_park in the audit means the
Commander already saw it and re-queued anyway, which is an explicit "build it". That fails OPEN
(toward building = today's behaviour), so the guard can never strand a real ticket.

Pins:
  (1) CLOSE with no code evidence → parked (no build), reported ESCALATED;
  (2) the parked question is plain-language, carries the Planner's reason, and passes the
      decisions.add ask-quality validator (a leaked-monologue reason must not void the park);
  (3) BUILD verdicts are untouched — the default path never parks;
  (4) park-once: a ticket with a prior planner_verdict_park BUILDS instead of re-parking;
  (5) EU-225 still wins when there IS code evidence (auto-close to QA, not a park);
  (6) the knob is honoured (planner_verdict_park=False → build anyway, the old behaviour);
  (7) dry-run / ephemeral tickets never park (no real board to park onto).
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, ".")

sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self


sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)

from orchestrator import decisions, loop  # noqa: E402

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


# ── (1) the helper exists and is the park-once gate ──────────────────────────────────────────
ok("loop exposes the park-once guard", hasattr(loop, "_planner_verdict_parked_before"))

_tmp = Path(tempfile.mkdtemp())
_audit = _tmp / "audit.jsonl"
cfg = types.SimpleNamespace(audit_path=str(_audit))

_audit.write_text("")
ok("(4a) no prior park → the guard permits parking",
   not loop._planner_verdict_parked_before(cfg, "AUTO-57"))

_audit.write_text(json.dumps({"ts": "2026-07-16T16:57:48+03:00", "event": "planner_verdict_park",
                              "ticket_id": "AUTO-57", "verdict": "CLOSE"}) + "\n")
ok("(4b) a prior park → the guard says BUILD (the Commander re-queued deliberately)",
   loop._planner_verdict_parked_before(cfg, "AUTO-57"),
   "without this the park loops forever: /unblock → CLOSE → park → /unblock …")
ok("(4c) the guard is per-ticket, not global",
   not loop._planner_verdict_parked_before(cfg, "AUTO-99"))

# a corrupt/absent audit must fail OPEN (toward building), never strand a ticket
_audit.write_text("{not json\n")
ok("(4d) an unreadable audit fails open (permits building, never strands)",
   not loop._planner_verdict_parked_before(cfg, "AUTO-57"))
ok("(4e) a missing audit file fails open",
   not loop._planner_verdict_parked_before(
       types.SimpleNamespace(audit_path=str(_tmp / "nope.jsonl")), "AUTO-57"))


# ── (2) the parked question is Commander-readable AND survives the ask-quality validator ─────
ok("loop exposes the question builder", hasattr(loop, "_planner_verdict_question"))

_reason_clean = "AUTO-57's coverage gate landed 2026-07-07 in commit 0474c1c; nothing left to build."
q = loop._planner_verdict_question("AUTO-57", "CLOSE", _reason_clean)
valid, why = decisions._validate_question_format(q)
ok("(2a) the question passes the ask-quality validator", valid, why)
ok("(2b) it states the verdict in plain language", "CLOSE" in q and "AUTO-57" in q, q[:120])
ok("(2c) it carries the Planner's reason", "0474c1c" in q, q[:200])
ok("(2d) it offers the Commander concrete options", "/unblock" in q.lower(), q[:240])

# A Planner reason full of leaked monologue must NOT void the park (decisions.add returns None on a
# bad question → the caller would fall through and BUILD, i.e. the exact burn this prevents).
_reason_leaky = ("## ANALYSIS\nLooking at the ticket, I need to say this is done.\n"
                 "Based on the changelog it shipped in 0474c1c.")
q2 = loop._planner_verdict_question("AUTO-57", "CLOSE", _reason_leaky)
valid2, why2 = decisions._validate_question_format(q2)
ok("(2e) a leaked-monologue Planner reason is sanitised, so the park still validates",
   valid2, f"{why2} | q={q2[:160]!r}")
ok("(2f) sanitising keeps the substance (the commit sha survives)", "0474c1c" in q2, q2[:200])

# ── (3)/(5)/(6)/(7) the routing decision table, as source pins on the loop branch ────────────
src = Path("orchestrator/loop.py").read_text()
seg = src[src.find("planner_nonbuild_verdict"):src.find("# 0b) ARCHITECT")]
ok("(5) EU-225's evidence-keyed auto-close still takes precedence over the park",
   seg.find("already_landed_autoclose") < seg.find("planner_verdict_park"),
   "the park must be the fallback for NO evidence, not a replacement for the close")
ok("(6) the park is behind a config knob", "planner_verdict_park" in src and
   'getattr(cfg, "planner_verdict_park", True)' in seg)
# The guard is useless unless the BRANCH consults it. Pinning only the helper (4a-4e) let a mutation
# that deletes this call from the branch pass green — the loop-forever bug shipping under a green
# test. Mutation-verified: dropping the call from the branch turns this RED.
ok("(4f) the park branch actually consults the park-once guard",
   "_planner_verdict_parked_before(cfg, ticket.id)" in seg,
   "guard exists but the branch never calls it → /unblock would re-park forever")
ok("(3b) the park fires ONLY for the non-BUILD verdicts, never a widened set",
   '_pres.verdict in ("CLOSE", "ANSWER", "REFILE")' in seg,
   "a widened condition would park BUILD tickets — the default path must never park")
ok("(7) dry-run and ephemeral tickets never park",
   "ticket.ephemeral" in seg and "dry_run" in seg)
ok("(3) BUILD/SPLIT never reach the park branch (it lives under the non-BUILD guard)",
   seg.find("planner_verdict_park") > 0 and 'if _pres.verdict not in ("BUILD", "SPLIT")' in src)
ok("(1b) the park returns ESCALATED, not a build",
   "Outcome.ESCALATED" in seg, seg[-400:])

print(f"\n{checks}/{checks} passed")
