"""Light daily stand-up (2026-07-07) — best-practice ceremony split: a fast DAILY brief and a deep
WEEKLY council.

The old daily muster (council.hold_council) fired ~8 model calls (7 officer stand-ups + a CTO chair).
council.daily_brief replaces the DAILY with a deterministic digest (dashboard.standup — 0 model calls)
plus ONE short CTO synthesis (today's focus + at most one decision). This harness pins that contract:
daily_brief exists, fires EXACTLY ONE model call (not the 7-officer loop), sends the deterministic
facts + the synthesis to Telegram, and surfaces a FOR-THE-COMMANDER question when the CTO raises one.

Written fail-first: against the code before daily_brief exists, the very first check goes RED.
"""
import sys, types, asyncio, tempfile
from pathlib import Path

REPO = "."
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, REPO)

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))

from orchestrator import council
from orchestrator.agent import AgentRun
from orchestrator.config import Config

check("council.daily_brief exists", hasattr(council, "daily_brief"))

if hasattr(council, "daily_brief"):
    # ---- stubs: count model calls, capture Telegram sends, no side effects ----
    calls: list[dict] = []
    async def _fake_run_agent(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
        calls.append({"tag": tag, "model": getattr(options, "model", ""),
                      "effort": getattr(options, "effort", ""), "prompt": prompt})
        return AgentRun(text="", final="**FOCUS** — land EU-129.\n\n**FOR THE COMMANDER**\nShip the paid tier now?",
                        cost_usd=0.01, num_turns=1, is_error=False)
    sent: list[str] = []
    # A kwarg-storing options stub so the test can see model/effort the code passes (the generic SDK
    # stub swallows kwargs).
    class _Opts:
        def __init__(self, **k): self.__dict__.update(k)
    _orig_run_agent = council.run_agent
    _orig_send = council.notify.send
    _orig_opts = council.ClaudeAgentOptions
    council.run_agent = _fake_run_agent
    council.notify.send = lambda msg, *a, **k: sent.append(msg) or True
    council.ClaudeAgentOptions = _Opts

    d = tempfile.mkdtemp()
    cfg = Config(apps=[])
    cfg.audit_path = str(Path(d) / "audit.jsonl")
    try:
        # EU-303: the daily send is now host-elected; this harness tests the CONTENT of the send,
        # so force broadcast=True (the election gating is pinned in eu303_daily_single_sender_test).
        out = asyncio.run(council.daily_brief(cfg, audit=None, broadcast=True))
    finally:
        council.run_agent = _orig_run_agent
        council.notify.send = _orig_send
        council.ClaudeAgentOptions = _orig_opts

    # ---- exactly ONE model call (the light synthesis — NOT the 7-officer stand-up loop) ----
    check("daily_brief fires EXACTLY ONE model call", len(calls) == 1, f"{len(calls)} calls")
    check("the single call is the CTO synthesis (tag the-general)",
          calls and calls[0]["tag"] == "the-general", str(calls))
    check("the synthesis runs cheap (low effort)", calls and calls[0]["effort"] == "low", str(calls))

    # ---- the phone message carries the deterministic stand-up + the CTO synthesis ----
    joined = "\n".join(sent)
    check("a Telegram message was sent", len(sent) >= 1)
    check("message carries the deterministic stand-up facts", "standup" in joined.lower() or "stand-up" in joined.lower(), joined[:120])
    check("message carries the CTO FOCUS synthesis", "FOCUS" in joined, joined[:160])

    # ---- a genuine Commander decision is surfaced as 'needs your call' ----
    check("FOR THE COMMANDER question is surfaced as a separate 'needs your call' ping",
          any("needs your call" in s.lower() for s in sent), str(sent))
    check("daily_brief returns the synthesis text", isinstance(out, str) and "FOCUS" in out)

    # ── NEW (2026-07-08): the daily must read as "recently shipped / today's focus / needs you" and
    #    must NOT carry cumulative all-time history (Commander: "I don't need to know 91/134 landed"). ──
    from orchestrator import dashboard
    from datetime import datetime, timedelta
    import json as _json
    _dd = tempfile.mkdtemp()
    _ap = Path(_dd) / "audit.jsonl"
    # Start 2 days ago but MERGE 3h ago: the run must bucket by MERGE time (ended), so it lands in the
    # shipped window — this pins that standup keys on ended, not started, and (with the astimezone fix)
    # on the reader's clock.
    #
    # EU-336 CONTRACT CHANGE (2026-07-17): the shipped headline moved from a yesterday/today calendar
    # split to a rolling 24h window, so this fixture was deliberately re-pinned — the merge moved from
    # "yesterday 12:00" to "3h ago" and the assertion from the word "yesterday" to the window wording.
    # The load-bearing intent (ended-not-started bucketing) is unchanged and still discriminates: a run
    # started 2d ago is outside the window, so keying on `started` would drop AUTO-99 and go red. The
    # old fixture was also latently FLAKY under the new window — a merge at yesterday 12:00 is inside
    # 24h only when the suite runs before local noon.
    _startts = (datetime.now() - timedelta(days=2)).replace(minute=0, second=0, microsecond=0).isoformat()
    _mergets = (datetime.now() - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0).isoformat()
    with open(_ap, "w") as _f:
        _f.write(_json.dumps({"ts": _startts, "event": "ticket_start", "ticket_id": "AUTO-99", "app": "automatixy"}) + "\n")
        _f.write(_json.dumps({"ts": _mergets, "event": "merged", "ticket_id": "AUTO-99", "app": "automatixy"}) + "\n")
    _cfg2 = Config(apps=[]); _cfg2.audit_path = str(_ap)
    _su = dashboard.standup(_cfg2)
    check("standup surfaces recently shipped work by MERGE time (started 2d ago, merged 3h ago)",
          "last 24h" in _su.lower() and "AUTO-99" in _su, _su[:220])
    # A directive test, not a mere keyword mention: the prompt must NEGATE ('do not cite') the cumulative
    # concept AND name a concrete forbidden token — a reworded 'DO cite the all-time history' fails this.
    _ds = council._DAILY_SYSTEM.lower()
    check("_DAILY_SYSTEM explicitly FORBIDS cumulative/all-time history (directive + a concrete token)",
          ("do not cite" in _ds or "not cite" in _ds)
          and ("cumulative" in _ds or "all-time" in _ds) and "avg passes" in _ds,
          council._DAILY_SYSTEM[:180])
    check("the daily CTO prompt is NOT fed the cumulative signals digest (no 'avg passes')",
          all("avg passes" not in (c.get("prompt") or "").lower() for c in calls),
          str([(c.get("prompt") or "")[:50] for c in calls]))

print("\n============ LIGHT DAILY BRIEF QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
