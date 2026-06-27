"""EU-89 — Proposal persistence, materialization & dedup gate.

Tests:
  1. enqueue_proposals stores body_raw alongside the proposals list.
  2. materialize_proposal finds the single matching batch and files it (approve_proposals called).
  3. materialize_proposal with no matching batch returns a 'not found' message.
  4. materialize_proposal with multiple matching batches returns an ambiguity message.
  5. decisions.add() dedup gate: same ticket id + question fingerprint → returns existing id, no write.
  6. decisions.add() dedup gate: different question → both entries stored.
  7. decisions.add() dedup gate: different ticket → both entries stored.
  8. route_message 'create it' → materialize_proposal called when exactly one proposal matches.
  9. route_message 'yes' with no matching proposal → falls through to respond_to_commander.
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
    results.append((name, bool(cond), detail))


def _make_cfg(tmp: Path) -> Config:
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
    return Ticket(id=tid, key=tid, summary=f"summary of {tid}", description="desc",
                  acceptance_criteria=[], app="automatixy", ephemeral=True)


# Suppress Telegram and tracker calls throughout.
notify.send = lambda *a, **k: None
decisions._park_on_tracker = lambda *a, **k: None


# ---------------------------------------------------------------------------
# 1. enqueue_proposals stores body_raw
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _td:
    cfg = _make_cfg(Path(_td))
    bid = approvals.enqueue_proposals(
        cfg,
        app_name="automatixy",
        officer_label="council",
        source="council/daily AUTO-77 follow-up",
        report=[{
            "title": "Add retry logic to worker",
            "type": "Task",
            "severity": "medium",
            "body": "Retries prevent dropped jobs.",
        }],
    )
    items = approvals._load_proposals(cfg)
    batch = next((b for b in items if b.get("id") == bid), None)
    check("enqueue stores body_raw field", batch is not None and "body_raw" in batch,
          str(batch.keys()) if batch else "batch not found")
    check("body_raw is a non-empty string",
          isinstance(batch.get("body_raw"), str) and len(batch["body_raw"]) > 0,
          repr(batch.get("body_raw", "")[:60]))

# ---------------------------------------------------------------------------
# 2. materialize_proposal — single match → approve_proposals called
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _td:
    cfg = _make_cfg(Path(_td))
    approvals.enqueue_proposals(
        cfg,
        app_name="automatixy",
        officer_label="council",
        source="council/daily AUTO-55 analysis",
        report=[{"title": "Index the users table", "type": "Task",
                 "severity": "high", "body": "Speeds up user lookup."}],
    )
    # Stub approve_proposals so we don't need a real Jira connection.
    from orchestrator.filing import FilingResult
    approve_calls: list[str] = []
    _real_approve = approvals.approve_proposals

    def _fake_approve(cfg, batch_id, titles=None):
        approve_calls.append(batch_id)
        r = FilingResult()
        r.filed = ["AUTO-101"]
        r.deduped = []
        r.failed = []
        return r

    approvals.approve_proposals = _fake_approve
    msg = approvals.materialize_proposal(cfg, "AUTO-55")
    approvals.approve_proposals = _real_approve

    check("materialize_proposal calls approve_proposals", len(approve_calls) == 1,
          str(approve_calls))
    check("materialize_proposal returns success message",
          "✅" in msg and "AUTO-55" in msg, repr(msg))

# ---------------------------------------------------------------------------
# 3. materialize_proposal — no match
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _td:
    cfg = _make_cfg(Path(_td))
    msg = approvals.materialize_proposal(cfg, "AUTO-99")
    check("materialize_proposal: no match returns not-found message",
          "No pending" in msg and "AUTO-99" in msg, repr(msg))

# ---------------------------------------------------------------------------
# 4. materialize_proposal — multiple matches → ambiguity message
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _td:
    cfg = _make_cfg(Path(_td))
    approvals.enqueue_proposals(
        cfg, app_name="automatixy", officer_label="council",
        source="council/sprint AUTO-33 alpha",
        report=[{"title": "Add caching", "type": "Task", "severity": "low", "body": ""}],
    )
    approvals.enqueue_proposals(
        cfg, app_name="automatixy", officer_label="council",
        source="council/sprint AUTO-33 beta",
        report=[{"title": "Add rate-limiting", "type": "Task", "severity": "low", "body": ""}],
    )
    msg = approvals.materialize_proposal(cfg, "AUTO-33")
    check("materialize_proposal: multiple matches returns ambiguity message",
          "Multiple" in msg and "AUTO-33" in msg, repr(msg))

# ---------------------------------------------------------------------------
# 5. decisions.add() dedup gate — same ticket + same question → skip
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _td:
    cfg = _make_cfg(Path(_td))
    tkt = _ticket("EU-44")
    first_id = decisions.add(cfg, tkt, "automatixy", "Which DB engine to use?")
    second_id = decisions.add(cfg, tkt, "automatixy", "Which DB engine to use?")
    stored = decisions.load(cfg)
    check("dedup gate: same question returns existing id",
          second_id == first_id, f"first={first_id!r} second={second_id!r}")
    check("dedup gate: only one entry in store",
          len([e for e in stored if e.get("id", "").startswith("EU-44")]) == 1,
          str([e.get("id") for e in stored]))

# ---------------------------------------------------------------------------
# 6. decisions.add() dedup gate — different question → both stored
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _td:
    cfg = _make_cfg(Path(_td))
    tkt = _ticket("EU-45")
    decisions.add(cfg, tkt, "automatixy", "Which DB engine to use?")
    decisions.add(cfg, tkt, "automatixy", "Should we add an index?",
                  entry_id="EU-45#index")
    stored = decisions.load(cfg)
    ids = [e.get("id") for e in stored]
    check("dedup gate: different questions both stored",
          "EU-45" in ids and "EU-45#index" in ids, str(ids))

# ---------------------------------------------------------------------------
# 7. decisions.add() dedup gate — different ticket → both stored
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _td:
    cfg = _make_cfg(Path(_td))
    decisions.add(cfg, _ticket("EU-10"), "automatixy", "Which DB engine to use?")
    decisions.add(cfg, _ticket("EU-11"), "automatixy", "Which DB engine to use?")
    stored = decisions.load(cfg)
    ids = [e.get("id") for e in stored]
    check("dedup gate: same question for different tickets → both stored",
          "EU-10" in ids and "EU-11" in ids, str(ids))

# ---------------------------------------------------------------------------
# 8. route_message 'create it' → materialize_proposal called
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _td:
    cfg = _make_cfg(Path(_td))

    # Plant a proposal batch for AUTO-77.
    approvals.enqueue_proposals(
        cfg, app_name="automatixy", officer_label="council",
        source="council/daily AUTO-77 follow-up",
        report=[{"title": "Add retry logic", "type": "Task",
                 "severity": "medium", "body": "Prevents dropped jobs."}],
    )

    # Plant recent chat that mentions AUTO-77.
    chat_path = Path(cfg.audit_path).with_name("commander_chat.md")
    chat_path.write_text(
        "Q: What about AUTO-77?\nA (General): We have a proposal queued.\n",
        encoding="utf-8",
    )

    materialize_calls: list[str] = []
    _real_mat = approvals.materialize_proposal

    def _fake_mat(cfg, ref):
        materialize_calls.append(ref)
        return f"✅ Filed 1 ticket(s) for {ref}."

    approvals.materialize_proposal = _fake_mat

    class _FakeAudit:
        def record(self, *a, **k): pass

    result = decisions.route_message(cfg, _FakeAudit(), "create it")
    approvals.materialize_proposal = _real_mat

    check("route_message 'create it' calls materialize_proposal",
          len(materialize_calls) == 1 and materialize_calls[0] == "AUTO-77",
          str(materialize_calls))
    check("route_message 'create it' returns True", result is True)

# ---------------------------------------------------------------------------
# 9. route_message 'yes' with no matching proposal → falls through to CTO
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _td:
    cfg = _make_cfg(Path(_td))
    # No proposals, no chat history → no match → must fall through to respond_to_commander.
    cto_called: list[str] = []

    async def _fake_respond(cfg, msg):
        cto_called.append(msg)
        return "Noted."

    council.respond_to_commander = _fake_respond

    class _FakeAudit:
        def record(self, *a, **k): pass

    decisions.route_message(cfg, _FakeAudit(), "yes")
    import time as _time; _time.sleep(0.05)   # let the background thread fire

    check("route_message 'yes' with no proposal falls through to CTO",
          len(cto_called) == 1, str(cto_called))

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
print("\n======= EU-89 PROPOSAL MATERIALIZE & DEDUP QA =======")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  ({detail})" if detail and not ok else ""))
print(f"  {passed}/{len(results)} passed",
      "✅" if passed == len(results) else "❌")
assert passed == len(results), f"{len(results) - passed} test(s) failed"
