"""EU-303 — the daily brief / council must have a single-sender host election (no duplicate dailies).

Roman 2026-07-14: the daily Telegram messages look doubled. council.daily_brief / hold_council called
notify.send unconditionally — the ONLY thing preventing duplicates was which machine held the cron, so
a second cron/timer/host (or install-server-cron run twice) each broadcast its own copy. There was no
should_poll_telegram/host-election gate on the SEND (contrast the Telegram poller, host-elected EU-185).

Pins (behavioral, run_agent stubbed — no model call):
  (1) broadcast=False → daily_brief computes + returns the brief but does NOT notify.send;
  (2) broadcast=True → it DOES notify.send;
  (3) broadcast=None → it elects via decisions.should_poll_telegram (elected host sends, others don't);
  (4) source: hold_council has the same election; interactive /daily and /council force broadcast=True.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, ".")

from orchestrator import council  # noqa: E402

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


class _Run:
    final = "TODAY: ship EU-303."
    text = final
    provider = "Anthropic"
    model_version = "claude-opus-4-8"


class _Cfg:
    discussion_model = "claude-opus-4-8"
    audit_path = "/tmp/eu303/audit.jsonl"


def run_daily(broadcast, elected=True):
    """Drive daily_brief with everything external stubbed; return the list of notify.send payloads."""
    sends = []

    async def _fake_run_agent(*a, **k):
        return _Run()

    with patch.object(council, "run_agent", _fake_run_agent), \
         patch.object(council.notify, "send", lambda msg: sends.append(msg)), \
         patch.object(council, "recent_commander_notes", lambda cfg: ""), \
         patch.object(council, "_commander_questions", lambda s: []), \
         patch.object(council, "_save_transcript", lambda *a, **k: None), \
         patch("orchestrator.memory.preamble", lambda: ""), \
         patch("orchestrator.dashboard.standup", lambda cfg: "FACTS"), \
         patch("orchestrator.decisions.should_poll_telegram", lambda cfg: (elected, "test")):
        kwargs = {} if broadcast is None else {"broadcast": broadcast}
        out = asyncio.run(council.daily_brief(_Cfg(), audit=None, **kwargs))
    return sends, out


# (1) broadcast=False → no send, but the brief is still computed/returned
sends, out = run_daily(broadcast=False)
ok("(1) broadcast=False suppresses the Telegram send", sends == [], f"sent {sends}")
ok("(1b) the brief is still computed and returned", "EU-303" in out, f"out={out!r}")

# (2) broadcast=True → sends
sends, out = run_daily(broadcast=True)
ok("(2) broadcast=True broadcasts the daily", any("FACTS" in s for s in sends), f"sent {sends}")

# (3) broadcast=None → elects: elected host sends, non-elected host does not
sends_elected, _ = run_daily(broadcast=None, elected=True)
sends_not, _ = run_daily(broadcast=None, elected=False)
ok("(3) elected host broadcasts when broadcast is unset", len(sends_elected) >= 1, f"{sends_elected}")
ok("(3b) non-elected host stays silent (de-dup)", sends_not == [], f"sent {sends_not}")

# (4) source pins
csrc = Path("orchestrator/council.py").read_text()
ok("(4) hold_council also elects a single sender",
   "async def hold_council" in csrc and csrc.count("should_poll_telegram(cfg)[0]") >= 2)
dsrc = Path("orchestrator/decisions.py").read_text()
ok("(4b) interactive /daily forces broadcast=True", "daily_brief(cfg, audit=audit, broadcast=True)" in dsrc)
ok("(4c) interactive /council forces broadcast=True",
   "hold_council(cfg, topic=arg or None, audit=audit, broadcast=True)" in dsrc)

print(f"\n{checks}/{checks} passed")
