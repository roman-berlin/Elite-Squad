"""EU-89 — Stateful-chat integration suite.

Three targeted checks that the propose→approve→materialize pipeline is
stateful, dedup-safe, and injects context into the LLM prompt:

  1. Happy-path integration: enqueue_proposals stores a roster body;
     route_message('create it') calls materialize_proposal exactly once
     and emits a single success Telegram — no follow-up question.
  2. Dedup: decisions.add() called twice with identical (ticket, question)
     stores exactly one entry in pending_decisions.json and dispatches
     exactly one Telegram notification.
  3. Context injection: respond_to_commander() with a message referencing
     a pending proposal injects the stored proposal body into the LLM
     prompt (so the CTO can resolve 'it' / 'approve' without re-asking).
"""
import asyncio
import sys
import tempfile
import types
from pathlib import Path

# ---------------------------------------------------------------------------
# Stub the Claude Agent SDK so we can import orchestrator modules without the
# real network-capable SDK installed.
# ---------------------------------------------------------------------------
sdk = types.ModuleType("claude_agent_sdk")


class _Dummy:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self


sdk.__getattr__ = lambda n: _Dummy
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import approvals, decisions, council, notify
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
results: list[tuple[str, bool, str]] = []


def check(name: str, cond, detail: str = "") -> None:
    """Record one pass/fail assertion with an optional detail string."""
    results.append((name, bool(cond), detail))


def _make_cfg(tmp: Path) -> Config:
    """Minimal in-memory config backed by a temp directory; no real Jira."""
    audit = tmp / "audit.jsonl"
    audit.write_text("")
    return Config(
        apps=[AppConfig(name="automatixy", repo_path=str(tmp),
                        base_branch="DEV", protected_branch="MAIN",
                        backlog_backend="none")],
        audit_path=str(audit),
        use_worktree=False,
    )


def _ticket(tid: str) -> Ticket:
    """Minimal ephemeral ticket for decisions.add() calls."""
    return Ticket(id=tid, key=tid, summary=f"summary of {tid}", description="desc",
                  acceptance_criteria=[], app="automatixy", ephemeral=True)


# Suppress real Jira transitions and Telegram calls throughout.
decisions._park_on_tracker = lambda *a, **k: None
notify.send = lambda *a, **k: None


# ---------------------------------------------------------------------------
# 1. Happy-path integration: propose → 'create it' → materialize
#
# Verify that route_message('create it') calls materialize_proposal exactly
# once and sends exactly one success Telegram — NOT a follow-up question.
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _td:
    cfg = _make_cfg(Path(_td))

    # Enqueue a proposal batch whose source mentions AUTO-77.
    approvals.enqueue_proposals(
        cfg,
        app_name="automatixy",
        officer_label="council",
        source="council/daily AUTO-77 follow-up",
        report=[{
            "title": "Add retry logic to background worker",
            "type": "Task",
            "severity": "medium",
            "body": "Retries prevent dropped jobs on transient failures.",
        }],
    )

    # Plant recent chat mentioning AUTO-77 so route_message can find the ref.
    chat_path = Path(cfg.audit_path).with_name("commander_chat.md")
    chat_path.write_text(
        "Q: What about AUTO-77?\nA (General): We have a proposal queued.\n",
        encoding="utf-8",
    )

    # Track both materialize_proposal calls and outgoing Telegram messages.
    telegram_msgs: list[str] = []
    notify.send = lambda m: telegram_msgs.append(m)

    materialize_calls: list[str] = []
    _real_mat = approvals.materialize_proposal

    def _fake_mat(cfg, ref):
        """Stub that records the call and returns a success string."""
        materialize_calls.append(ref)
        return f"✅ Filed 1 ticket(s) for {ref}."

    approvals.materialize_proposal = _fake_mat

    class _FakeAudit:
        def record(self, *a, **k): pass

    routed = decisions.route_message(cfg, _FakeAudit(), "create it")
    approvals.materialize_proposal = _real_mat

    check("happy-path: materialize_proposal called exactly once",
          len(materialize_calls) == 1, str(materialize_calls))
    check("happy-path: materialize_proposal received AUTO-77 as ref",
          materialize_calls == ["AUTO-77"], str(materialize_calls))
    check("happy-path: exactly one Telegram message sent",
          len(telegram_msgs) == 1, str(telegram_msgs))
    check("happy-path: Telegram message is a success — no follow-up question",
          bool(telegram_msgs) and "✅" in telegram_msgs[0] and "?" not in telegram_msgs[0],
          str(telegram_msgs))
    check("happy-path: route_message returns True", routed is True)

# Reset notify.send to a no-op between tests.
notify.send = lambda *a, **k: None


# ---------------------------------------------------------------------------
# 2. Dedup: two identical (ticket, question) calls → one store entry and
#    one Telegram dispatch.
#
# decisions.add() itself does not call notify.send; the caller (loop.py) is
# expected to notify only when a NEW entry is written.  We simulate that
# contract here: add, then notify iff the returned eid was absent from the
# store before the call.  The dedup gate must collapse the second add so that
# only one notification ever fires.
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _td:
    cfg = _make_cfg(Path(_td))

    QUESTION = "Which authentication strategy should we adopt?"
    tkt = _ticket("EU-89")

    telegram_sent: list[str] = []

    def _park_and_notify(cfg, ticket, app_name, question):
        """Add a decision and notify exactly once for new entries — mirrors loop.py's contract."""
        existing_ids = {e["id"] for e in decisions.load(cfg)}
        eid = decisions.add(cfg, ticket, app_name, question)
        # Only notify when this is a genuinely new entry (dedup hit → eid was already there).
        if eid and eid not in existing_ids:
            notify.send(f"❓ New decision parked for {eid}: {question}")
        return eid

    _real_send = notify.send
    notify.send = lambda m: telegram_sent.append(m)

    first_eid = _park_and_notify(cfg, tkt, "automatixy", QUESTION)
    second_eid = _park_and_notify(cfg, tkt, "automatixy", QUESTION)

    notify.send = _real_send

    stored = decisions.load(cfg)
    eu89_entries = [e for e in stored if str(e.get("id", "")).startswith("EU-89")]

    check("dedup: same (ticket, question) returns the existing entry id",
          first_eid is not None and first_eid == second_eid,
          f"first={first_eid!r} second={second_eid!r}")
    check("dedup: only one entry in pending_decisions.json",
          len(eu89_entries) == 1, str([e["id"] for e in stored]))
    check("dedup: exactly one Telegram message dispatched",
          len(telegram_sent) == 1, str(telegram_sent))

# Reset notify.send.
notify.send = lambda *a, **k: None


# ---------------------------------------------------------------------------
# 3. Context injection: respond_to_commander() injects the stored proposal
#    body into the LLM prompt.
#
# When the Commander references a ticket (AUTO-5) in a message, the CTO must
# receive the full pending proposal body in the prompt so short replies like
# 'approve' or 'create it' can resolve without a second round-trip to ask
# "which one?".
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _td:
    cfg = _make_cfg(Path(_td))

    # A distinctive proposal body — short enough to avoid truncation (<200 chars).
    PROPOSAL_BODY = "Null pointer in the login flow causes 500 responses in production."

    # Enqueue a pending batch whose source mentions AUTO-5.
    approvals.enqueue_proposals(
        cfg,
        app_name="automatixy",
        officer_label="council",
        source="daily AUTO-5",
        report=[{
            "title": "Fix AUTO-5 null check",
            "type": "Bug",
            "severity": "high",
            "body": PROPOSAL_BODY,
        }],
    )

    # Plant a chat turn so the thread context is non-empty.
    chat_path = Path(cfg.audit_path).with_name("commander_chat.md")
    chat_path.write_text(
        "Q: What about AUTO-5?\nA (General): Proposal queued for your approval.\n",
        encoding="utf-8",
    )

    captured_prompts: list[str] = []

    async def _fake_run_agent(prompt, options, tag=None):
        """Intercept the LLM call and capture the prompt for inspection."""
        captured_prompts.append(prompt)
        return types.SimpleNamespace(
            final="Acknowledged.", text="Acknowledged.",
            is_error=False, cost_usd=0.0, num_turns=1, tools=[],
        )

    council.run_agent = _fake_run_agent
    council.notify.send = lambda m: None   # suppress outgoing Telegram

    asyncio.run(council.respond_to_commander(cfg, "approve AUTO-5"))

    # EU-602: _needs_specialist also calls run_agent (triage), so use the LAST captured prompt
    # (the main CTO call), not the first (which is now the triage call).
    injected = captured_prompts[-1] if captured_prompts else ""
    check("context injection: LLM prompt contains Thread context section",
          "Thread context" in injected, repr(injected[:300]))
    check("context injection: stored proposal body appears in the LLM prompt",
          PROPOSAL_BODY in injected,
          repr(injected[injected.find("Thread context"):injected.find("Thread context") + 600])
          if "Thread context" in injected else "(Thread context section absent)")
    check("context injection: proposal title appears in the LLM prompt",
          "Fix AUTO-5 null check" in injected, repr(injected[:800]))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
print("\n======= EU-89 STATEFUL CHAT QA =======")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  ({detail})" if detail and not ok else ""))
print(f"  {passed}/{len(results)} passed",
      "✅" if passed == len(results) else "❌")
assert passed == len(results), f"{len(results) - passed} test(s) failed"
