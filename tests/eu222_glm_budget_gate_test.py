"""EU-222: GLM budget pricing gate wired into loop._run_inner.

Commander's audit (2026-07-12) established the ticket's stale premise is wrong: max_cost_usd
ALREADY prices/counts GLM spend via the SDK's real total_cost_usd (it was only ever inert because
the default max_cost_usd=0.0 disables the cap). So this is NOT a parallel GLM price table — it is
a fail-closed GLM quota-AVAILABILITY pre-flight in loop._run_inner, gated on the ACTIVE backend
being GLM and usage.dual_provider_budget_status() reporting GLM over/near its quota.

Five cases (all stubbed — no live LLM/Jira/git calls):
  1. GLM active + GLM budget over/bad -> _run_inner returns [] WITHOUT dispatching to either
     drain, and records a 'glm_budget_preflight_block' audit event with a clear GLM-quota message.
  2. GLM active + GLM budget healthy -> _run_inner proceeds to the normal serial dispatch
     unchanged (the gate is a no-op).
  3. Active backend is opus/Claude (model_backend != 'glm') -> a GLM-over-quota status does NOT
     block the run; the gate only fires when GLM is the active backend.
  4. dual_provider_budget_status(cfg) is actually invoked inside _run_inner before dispatch.
  5. python3 tests/run_all.py stays green (verified by this file's own exit code + harness run).
"""
import asyncio
import sys
import tempfile
import types
from pathlib import Path

# ---------------------------------------------------------------------------
# Stub the Agent SDK so importing the orchestrator never reaches the network.
# ---------------------------------------------------------------------------
sdk = types.ModuleType("claude_agent_sdk")

class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self

sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.loop as loop
from orchestrator import usage
from orchestrator.audit import AuditLog
from orchestrator.config import Config

results = []

def chk(name, cond, detail=""):
    results.append((name, bool(cond), detail))


tmp = Path(tempfile.mkdtemp())
audit_path = tmp / "audit.jsonl"


def _cfg(model_backend="glm"):
    return Config(apps=[], audit_path=str(audit_path), model_backend=model_backend)


def _read_events():
    if not audit_path.exists():
        return []
    return [line for line in audit_path.read_text().splitlines() if line.strip()]


class _StatusStub:
    """Swap usage.dual_provider_budget_status for a fixed return value and record the call."""
    def __init__(self, status):
        self.status = status
        self.calls = 0
        self._orig = usage.dual_provider_budget_status

    def __enter__(self):
        def fake(cfg):
            self.calls += 1
            return self.status
        usage.dual_provider_budget_status = fake
        loop.usage.dual_provider_budget_status = fake
        return self

    def __exit__(self, *a):
        usage.dual_provider_budget_status = self._orig
        loop.usage.dual_provider_budget_status = self._orig


def _drain_stub(name):
    calls = []
    async def fake(cfg, worklist, audit, stop_event, stop_between_tickets, *rest):
        calls.append(worklist)
        return ["dispatched"]
    return fake, calls


# ---------------------------------------------------------------------------
# Test 1: GLM active + over quota -> run blocked, no dispatch, audit event recorded.
# ---------------------------------------------------------------------------
audit_path.unlink(missing_ok=True)
audit = AuditLog(str(audit_path))
cfg = _cfg("glm")
status_over = {"active_provider": "glm", "glm": {"over": True, "bad": False, "pct": 1.1},
               "claude": {}, "healthy": False, "bad_provider": "glm"}

orig_serial, orig_concurrent = loop._run_inner_serial, loop._run_inner_concurrent
serial_fake, serial_calls = _drain_stub("serial")
concurrent_fake, concurrent_calls = _drain_stub("concurrent")
loop._run_inner_serial = serial_fake
loop._run_inner_concurrent = concurrent_fake
try:
    with _StatusStub(status_over) as stub:
        out = asyncio.run(loop._run_inner(cfg, [("app", "TICKET-1")], audit))
        chk("GLM over-quota: _run_inner returns [] (blocked)", out == [], f"got {out}")
        chk("GLM over-quota: dual_provider_budget_status was called", stub.calls >= 1)
    chk("GLM over-quota: neither drain was dispatched",
        serial_calls == [] and concurrent_calls == [],
        f"serial={serial_calls}, concurrent={concurrent_calls}")
    events = _read_events()
    block_events = [e for e in events if '"event": "glm_budget_preflight_block"' in e]
    chk("GLM over-quota: audit records glm_budget_preflight_block", len(block_events) == 1,
        f"events={events}")
finally:
    loop._run_inner_serial = orig_serial
    loop._run_inner_concurrent = orig_concurrent

# ---------------------------------------------------------------------------
# Test 2: GLM active + healthy budget -> gate is a no-op, normal dispatch proceeds.
# ---------------------------------------------------------------------------
audit_path.unlink(missing_ok=True)
audit = AuditLog(str(audit_path))
cfg = _cfg("glm")
status_healthy = {"active_provider": "glm", "glm": {"over": False, "bad": False, "pct": 0.1},
                   "claude": {}, "healthy": True, "bad_provider": None}

serial_fake, serial_calls = _drain_stub("serial")
concurrent_fake, concurrent_calls = _drain_stub("concurrent")
loop._run_inner_serial = serial_fake
loop._run_inner_concurrent = concurrent_fake
try:
    with _StatusStub(status_healthy):
        worklist = [("app", "TICKET-2")]
        out = asyncio.run(loop._run_inner(cfg, worklist, audit))
        chk("GLM healthy: _run_inner dispatches to serial drain (no-op gate)",
            serial_calls == [worklist], f"serial_calls={serial_calls}")
        chk("GLM healthy: _run_inner returns the drain's result",
            out == ["dispatched"], f"got {out}")
    events = _read_events()
    chk("GLM healthy: no glm_budget_preflight_block audit event recorded",
        not any('"event": "glm_budget_preflight_block"' in e for e in events), f"events={events}")
finally:
    loop._run_inner_serial = orig_serial
    loop._run_inner_concurrent = orig_concurrent

# ---------------------------------------------------------------------------
# Test 3: active backend is opus/Claude -> a GLM-over-quota status must NOT block the run.
# ---------------------------------------------------------------------------
audit_path.unlink(missing_ok=True)
audit = AuditLog(str(audit_path))
cfg = _cfg("opus")

serial_fake, serial_calls = _drain_stub("serial")
concurrent_fake, concurrent_calls = _drain_stub("concurrent")
loop._run_inner_serial = serial_fake
loop._run_inner_concurrent = concurrent_fake
try:
    # Even if dual_provider_budget_status somehow reported GLM as active+over (e.g. a stale sticky
    # pref), the pre-flight must gate on cfg.model_backend being GLM, not just the status blob.
    with _StatusStub(status_over):
        worklist = [("app", "TICKET-3")]
        out = asyncio.run(loop._run_inner(cfg, worklist, audit))
        chk("Opus active: GLM over-quota status does not block the run",
            serial_calls == [worklist], f"serial_calls={serial_calls}")
        chk("Opus active: _run_inner returns the drain's result unblocked",
            out == ["dispatched"], f"got {out}")
    events = _read_events()
    chk("Opus active: no glm_budget_preflight_block audit event recorded",
        not any('"event": "glm_budget_preflight_block"' in e for e in events), f"events={events}")
finally:
    loop._run_inner_serial = orig_serial
    loop._run_inner_concurrent = orig_concurrent

# ---------------------------------------------------------------------------
# Test 4: dual_provider_budget_status(cfg) is called inside _run_inner, receiving cfg.
# ---------------------------------------------------------------------------
audit_path.unlink(missing_ok=True)
audit = AuditLog(str(audit_path))
cfg = _cfg("glm")
received_cfg = []

def fake_status(passed_cfg):
    received_cfg.append(passed_cfg)
    return status_healthy

serial_fake, serial_calls = _drain_stub("serial")
loop._run_inner_serial = serial_fake
orig_status = usage.dual_provider_budget_status
usage.dual_provider_budget_status = fake_status
loop.usage.dual_provider_budget_status = fake_status
try:
    asyncio.run(loop._run_inner(cfg, [("app", "TICKET-4")], audit))
    chk("dual_provider_budget_status(cfg) is invoked inside _run_inner",
        received_cfg == [cfg], f"received_cfg={received_cfg}")
finally:
    usage.dual_provider_budget_status = orig_status
    loop.usage.dual_provider_budget_status = orig_status
    loop._run_inner_serial = orig_serial
    loop._run_inner_concurrent = orig_concurrent

# ---------------------------------------------------------------------------
# Test 5 (iter-2 regression, Reviewer): the gate must key off cfg.model_backend — the backend
# dispatch ACTUALLY uses — NOT status['active_provider']. If model_backend='glm' but the status
# blob reports a DIVERGENT active_provider='claude' (a stale/global sticky pref) while GLM is over
# quota, the run MUST still fail closed. The old `active_provider != 'glm'` early-return deferred to
# that divergent source and opened a fail-closed BYPASS — this test locks it shut.
# ---------------------------------------------------------------------------
audit_path.unlink(missing_ok=True)
audit = AuditLog(str(audit_path))
cfg = _cfg("glm")
status_divergent = {"active_provider": "claude", "glm": {"over": True, "bad": False, "pct": 1.2},
                    "claude": {}, "healthy": False, "bad_provider": "glm"}

serial_fake, serial_calls = _drain_stub("serial")
concurrent_fake, concurrent_calls = _drain_stub("concurrent")
loop._run_inner_serial = serial_fake
loop._run_inner_concurrent = concurrent_fake
try:
    with _StatusStub(status_divergent):
        out = asyncio.run(loop._run_inner(cfg, [("app", "TICKET-5")], audit))
        chk("Divergent pref: model_backend=glm + GLM over-quota still blocks despite "
            "active_provider=claude", out == [], f"got {out}")
    chk("Divergent pref: neither drain dispatched",
        serial_calls == [] and concurrent_calls == [],
        f"serial={serial_calls}, concurrent={concurrent_calls}")
    events = _read_events()
    block_events = [e for e in events if '"event": "glm_budget_preflight_block"' in e]
    chk("Divergent pref: audit records glm_budget_preflight_block", len(block_events) == 1,
        f"events={events}")
finally:
    loop._run_inner_serial = orig_serial
    loop._run_inner_concurrent = orig_concurrent

print("\n=============== EU-222 GLM BUDGET PRE-FLIGHT GATE QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
