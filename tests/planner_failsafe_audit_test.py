"""Planner fail-safe must be VISIBLE — the audit event carries the swallowed error (2026-07-16).

Live incident: three GLM-routed planner calls in a row (AUTO-155/156/157) ran full
multi-minute tool sessions, then run_agent raised — plan()'s fail-safe returned
verdict=BUILD with 0 testable AC / 0 in-scope files / cost $0 / empty provider, and
the audit "planner" event recorded NO error field (the exception text lives only in
res.raw, which is never persisted). Result: the builder proceeded briefless (AUTO-156
then hit the turn limit and parked), the burn was unmetered (no usage-ledger row, $0
against the per-ticket budget), and the failure class is undiagnosable from audit.

Pins:
  (1) fail-safe behavior unchanged: run_agent raising → verdict BUILD, never raises
      (control — this is the designed never-block contract);
  (2) the audit "planner" event now carries the swallowed error text (exception type
      + message) — this is the fail-first RED before the fix;
  (3) a SUCCESSFUL plan records NO error field (no noise on the happy path).
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

# ── minimal SDK / requests stubs so the orchestrator imports without real deps ──
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self


sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)

req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers={}, update=lambda *a, **k: None)
sys.modules.setdefault("requests", req)

sys.path.insert(0, ".")

from orchestrator import planner  # noqa: E402
from orchestrator.config import AppConfig, Config  # noqa: E402
from orchestrator.contracts import Ticket  # noqa: E402

_TMP = Path(tempfile.mkdtemp())
_CFG = Config(
    apps=[AppConfig(name="testapp", repo_path=str(_TMP), base_branch="dev",
                    protected_branch="main", backlog_backend="none")],
    audit_path=str(_TMP / "a.jsonl"),
    use_worktree=False,
)
_TICKET = Ticket(id="T-1", key="T-1", summary="s", description="d", app="testapp")

checks = 0


def ok(name: str, cond, detail: str = "") -> None:
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


class FakeAudit:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def record(self, event: str, **kw):
        self.events.append((event, kw))


async def _boom(*a, **k):
    raise RuntimeError("GLM stream aborted after 40 tool calls")


# (1)+(2) run_agent raises → BUILD fail-safe, and the audit event exposes the error
audit = FakeAudit()
with patch.object(planner, "run_agent", _boom):
    res = asyncio.run(planner.plan(_CFG, _TICKET, audit=audit))
ok("(1) fail-safe still returns BUILD and never raises", res.verdict == "BUILD")

pl_events = [kw for ev, kw in audit.events if ev == "planner"]
ok("(1b) exactly one planner audit event", len(pl_events) == 1, f"got {len(pl_events)}")
err = str(pl_events[0].get("error") or "")
ok("(2) audit event carries the swallowed error (type + message)",
   "RuntimeError" in err and "GLM stream aborted" in err,
   f"error field = {err!r} — a silent fail-safe leaves the failure class "
   f"undiagnosable from audit (AUTO-155/156/157, 2026-07-16)")


# (2b) the planner turn cap gives a batching-poor backend room to finish exploring —
# GLM planners batch ~1 tool call per turn and hit the old cap of 14 mid-exploration
# (4 of 6 GLM planner calls on 2026-07-16 fail-safed on "Reached maximum number of
# turns (14)"), producing exactly the briefless BUILDs pinned above.
class _OptsCapture:
    def __init__(self, **kw):
        _OptsCapture.last = kw


audit_cap = FakeAudit()
with patch.object(planner, "ClaudeAgentOptions", _OptsCapture), \
     patch.object(planner, "run_agent", _boom):
    asyncio.run(planner.plan(_CFG, _TICKET, audit=audit_cap))
ok("(2b) planner max_turns is at least 24 (GLM turn-batching headroom)",
   _OptsCapture.last.get("max_turns", 0) >= 24,
   f"max_turns = {_OptsCapture.last.get('max_turns')}")


# (3) happy path records no error field
class _GoodRun:
    final = '{"verdict": "BUILD", "approach": "x", "testable_ac": ["a"], "in_scope_files": ["f"]}'
    text = final
    cost_usd = 0.5
    num_turns = 3
    input_tokens = 100
    output_tokens = 10
    provider = "Anthropic"
    model_version = "claude-opus-4-8"


async def _good(*a, **k):
    return _GoodRun()


audit2 = FakeAudit()
with patch.object(planner, "run_agent", _good):
    res2 = asyncio.run(planner.plan(_CFG, _TICKET, audit=audit2))
pl2 = [kw for ev, kw in audit2.events if ev == "planner"]
ok("(3) successful plan records no error field",
   len(pl2) == 1 and "error" not in pl2[0],
   f"event = {pl2}")

print(f"\n{checks}/{checks} passed")
