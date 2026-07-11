"""EU-213: end-to-end harness discriminating a transient Sonnet 429 from a genuine weekly-cap,
verifying both EU-210 (backoff-retry classification) and EU-212 (fallback audit) already-shipped
behaviour, PLUS the one genuinely-unbuilt slice this ticket adds: on-disk dedup for the plan-limit
Telegram alert (``notify.py``) so a process restart doesn't re-fire an alert already sent.

Reconciling the ticket's stale pre-split wording with the real EU-210/EU-212 design: there is NO
fallback state FILE — the Sonnet-cap -> Opus fallback is per-call/auto-exit and persists nothing
(agent.py _run_agent_with_fallback). "Fallback state written with audit reason" means the single
`sonnet_fallback_activated` audit event (reason='cap' + original error) — this harness asserts
that event AND asserts no `sonnet_fallback_state.json` (or any weekly-pin state) is ever written.

Scenarios:
  1. Transient 429 with backoff success -> no `sonnet_fallback_activated` event, no fallback state,
     every call stays on Sonnet (agent.py, EU-210 path).
  2. Genuine weekly-cap error -> exactly one `sonnet_fallback_activated` audit event (reason='cap',
     error text carries the original 'usage limit' message), no state file written (agent.py,
     EU-212 path).
  3. Genuine weekly-cap alert -> `notify.plan_limit_alert` sends once, dedups an immediate repeat
     in the same process.
  4. Alert dedup survives a restart -> the on-disk flag (EU-213, new) makes a fresh "process" (in-
     memory flag reset, disk untouched) see "already sent" for the SAME episode and refuse to
     re-send; resetting clears both the flag and the file so a later alert can fire again.
  5. Stale flag from a PRIOR episode does NOT eat a NEW episode's alert (EU-213 restart-robustness):
     with an on-disk `{plan_limit_alert_sent: true, episode: <old>}` and the in-memory flag reset
     (simulated restart), firing the alert for a DIFFERENT cap (new limits + new reset time) STILL
     sends — suppression is scoped to the episode signature, not a bare boolean.

Written fail-first:
  - Scenario 4's "restart" step, against notify.py before EU-213 (in-memory-only
    `_plan_limit_alert_sent`), comes back with the flag simply False again — no on-disk memory to
    adopt — so it re-sends and the harness's checks go RED.
  - Scenario 5, against the FIRST EU-213 attempt (boolean-only on-disk flag, no episode identity),
    sees the stale `plan_limit_alert_sent: true` and suppresses the new-episode alert — so its
    "STILL sends" checks go RED. It passes only once suppression is episode-scoped.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

# Stub the Agent SDK (imported transitively) so import never needs a real model / network.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import agent as agent_mod
from orchestrator import notify
from orchestrator.agent import AgentRun

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))


# Zero out the transient backoff so the test doesn't actually sleep.
agent_mod._TRANSIENT_RETRY_BACKOFF_S = 0.0

_orig_run_agent = agent_mod.run_agent


class _Opts:
    def __init__(self, model):
        self.model = model


class _FakeSink:
    def __init__(self):
        self.events = []

    def record(self, event, **fields):
        self.events.append((event, fields))


# ══════════════════════════════ 1. transient 429 + backoff success ══════════════════════════════ #
def _make_fake_transient():
    calls = {"n": 0}
    async def _fake(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return AgentRun(text="", final="Error: 429 rate limit exceeded", cost_usd=0.0,
                            num_turns=0, is_error=True, is_plan_limit=True, plan_limit_kind="transient")
        return AgentRun(text="ok", final="ok", cost_usd=0.1, num_turns=1, is_error=False)
    return _fake, calls


sink1 = _FakeSink()
agent_mod.configure_audit(sink1)
fake1, calls1 = _make_fake_transient()
agent_mod.run_agent = fake1
try:
    res1 = asyncio.run(agent_mod.run_agent_with_fallback(
        "p", _Opts("claude-sonnet-5"), tag="builder", ticket_id="EU-213", pass_number=1))
finally:
    agent_mod.run_agent = _orig_run_agent
    agent_mod.configure_audit(None)

check("transient 429 + clean retry -> is_plan_limit False",
      res1 is not None and not res1.is_plan_limit, res1)
check("transient 429 + clean retry -> result surfaced ('ok')",
      res1 is not None and res1.final == "ok", res1 and res1.final)
check("transient path never probes Opus (2 Sonnet calls only)", calls1["n"] == 2, calls1)
check("transient path emits NO sonnet_fallback_activated audit event",
      not any(e == "sonnet_fallback_activated" for e, _ in sink1.events), sink1.events)


# ══════════════════════════════ 2. genuine weekly-cap error (agent) ══════════════════════════════ #
async def _fake_cap(prompt, options, tag="", ticket_id=None, pass_number=None, routing_tier=None):
    model = getattr(options, "model", "")
    if "sonnet" in model.lower():
        return AgentRun(text="", final="Error: Claude usage limit reached for your plan",
                        cost_usd=0.0, num_turns=0, is_error=True, is_plan_limit=True,
                        plan_limit_kind="cap")
    return AgentRun(text="ok", final="ok", cost_usd=0.1, num_turns=1, is_error=False)


tmpdir = Path(tempfile.mkdtemp())
sink2 = _FakeSink()
agent_mod.configure_audit(sink2)
agent_mod.run_agent = _fake_cap
try:
    res2 = asyncio.run(agent_mod.run_agent_with_fallback(
        "p", _Opts("claude-sonnet-5"), tag="builder", ticket_id="EU-213", pass_number=1))
finally:
    agent_mod.run_agent = _orig_run_agent
    agent_mod.configure_audit(None)

cap_events = [f for e, f in sink2.events if e == "sonnet_fallback_activated"]
check("genuine cap -> exactly one sonnet_fallback_activated event", len(cap_events) == 1, sink2.events)
if cap_events:
    ev = cap_events[0]
    check("cap event carries reason='cap'", ev.get("reason") == "cap", ev)
    check("cap event carries the original 'usage limit' error text",
          "usage limit" in str(ev.get("error", "")), ev)
check("genuine cap -> Sonnet call still completes via Opus (fallback result returned)",
      res2 is not None and not res2.is_error and res2.final == "ok")
check("no sonnet_fallback_state.json (or any weekly-pin state) written anywhere",
      not any(tmpdir.rglob("sonnet_fallback_state.json")) and
      not Path("sonnet_fallback_state.json").exists() and
      not any(Path(".").glob("**/sonnet_fallback_state.json")))


# ══════════════════════════════ 3+4. genuine-cap alert + restart dedup (notify.py) ═══════════════ #
_notify_dedup_dir = Path(tempfile.mkdtemp())
_orig_dedup_file = notify._PLAN_LIMIT_DEDUP_FILE
notify._PLAN_LIMIT_DEDUP_FILE = _notify_dedup_dir / "sonnet_alert_dedup.json"
notify._plan_limit_alert_sent = False

over_limits = [{"label": "Weekly Sonnet tokens", "utilization": 1.0}]
reset_times = ["Fri Jul 11 00:00 UTC"]

try:
    with patch("orchestrator.notify.configured", return_value=True), \
         patch("orchestrator.notify.send", return_value=True) as mock_send:
        # AC3: first call sends, immediate repeat in the same session is deduped.
        sent_first = notify.plan_limit_alert(over_limits, reset_times)
        check("genuine-cap alert sends once (first call)", sent_first is True)
        check("genuine-cap alert sent exactly once so far", mock_send.call_count == 1, mock_send.call_count)

        sent_repeat = notify.plan_limit_alert(over_limits, reset_times)
        check("immediate repeat in same session is deduped (returns False)", sent_repeat is False)
        check("immediate repeat never calls send again", mock_send.call_count == 1, mock_send.call_count)

        # AC4: simulate a process restart — in-memory flag resets, on-disk dedup file persists.
        check("dedup file was written to disk after the first send",
              notify._PLAN_LIMIT_DEDUP_FILE.exists(), notify._PLAN_LIMIT_DEDUP_FILE)
        notify._plan_limit_alert_sent = False  # <- the "restart": fresh process, in-memory flag gone

        sent_after_restart = notify.plan_limit_alert(over_limits, reset_times)
        check("post-restart alert loads dedup from disk -> refuses to re-fire",
              sent_after_restart is False)
        check("post-restart repeat never calls send", mock_send.call_count == 1, mock_send.call_count)

        # reset_plan_limit_alert() clears BOTH the in-memory flag and the on-disk file, so a later
        # alert (e.g. after the limit renews next week) can fire again.
        notify.reset_plan_limit_alert()
        check("reset clears the in-memory flag", notify._plan_limit_alert_sent is False)
        dedup_after_reset = json.loads(notify._PLAN_LIMIT_DEDUP_FILE.read_text(encoding="utf-8"))
        check("reset clears the on-disk dedup flag too",
              dedup_after_reset.get("plan_limit_alert_sent") is False, dedup_after_reset)

        sent_after_reset = notify.plan_limit_alert(over_limits, reset_times)
        check("a later alert (post-reset) can fire again", sent_after_reset is True)
        check("post-reset alert actually called send", mock_send.call_count == 2, mock_send.call_count)
finally:
    notify._PLAN_LIMIT_DEDUP_FILE = _orig_dedup_file
    notify._plan_limit_alert_sent = False
    notify._plan_limit_alert_episode = None


# ══════════ 5. stale prior-episode flag must NOT suppress a NEW-episode alert (restart-robust) ═════ #
# Regression guard for the iteration-2 rejection: a bare on-disk `plan_limit_alert_sent: true` left
# over from an earlier (already-resolved) cap must never eat the alert for a genuinely different cap
# after a restart. Suppression is keyed on the episode signature, so a new episode alerts regardless.
_ep_dir = Path(tempfile.mkdtemp())
notify._PLAN_LIMIT_DEDUP_FILE = _ep_dir / "sonnet_alert_dedup.json"

prior_over_limits = [{"key": "weekly_sonnet", "label": "Weekly Sonnet tokens"}]
prior_reset_times = ["Fri Jul 11 00:00 UTC"]
new_over_limits = [{"key": "weekly_opus", "label": "Weekly Opus tokens"}]
new_reset_times = ["Fri Jul 18 00:00 UTC"]

# Sanity: the two episodes really do differ, else the test proves nothing.
check("prior and new episodes have distinct signatures",
      notify._episode_signature(prior_over_limits, prior_reset_times)
      != notify._episode_signature(new_over_limits, new_reset_times))

try:
    with patch("orchestrator.notify.configured", return_value=True), \
         patch("orchestrator.notify.send", return_value=True) as mock_send_ep:
        # Persist an on-disk dedup for the PRIOR episode (as a now-restarted process would have left).
        notify._plan_limit_dedup_write(True, notify._episode_signature(prior_over_limits, prior_reset_times))
        # Simulate the restart: no in-memory memory of any episode.
        notify._plan_limit_alert_sent = False
        notify._plan_limit_alert_episode = None

        disk_sent, disk_ep = notify._plan_limit_dedup_read()
        check("on-disk stale flag is present for the prior episode before the new cap", disk_sent is True,
              (disk_sent, disk_ep))

        sent_new_episode = notify.plan_limit_alert(new_over_limits, new_reset_times)
        check("stale prior-episode flag does NOT suppress a new-episode alert (still sends)",
              sent_new_episode is True, sent_new_episode)
        check("new-episode alert actually called send", mock_send_ep.call_count == 1, mock_send_ep.call_count)

        # And the SAME (now-current) episode is deduped as normal afterwards.
        sent_new_repeat = notify.plan_limit_alert(new_over_limits, new_reset_times)
        check("the now-current episode dedups a repeat (returns False)", sent_new_repeat is False,
              sent_new_repeat)
        check("repeat of the current episode never re-sends", mock_send_ep.call_count == 1,
              mock_send_ep.call_count)

        # Disk now records the NEW episode, not the stale one.
        _, disk_ep_after = notify._plan_limit_dedup_read()
        check("disk dedup now records the NEW episode signature",
              disk_ep_after == notify._episode_signature(new_over_limits, new_reset_times), disk_ep_after)
finally:
    notify._PLAN_LIMIT_DEDUP_FILE = _orig_dedup_file
    notify._plan_limit_alert_sent = False
    notify._plan_limit_alert_episode = None


print("\n============ EU-213 SONNET CAP DISCRIMINATION QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
