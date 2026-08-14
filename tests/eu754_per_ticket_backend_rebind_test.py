#!/usr/bin/env python3
"""EU-754: Per-ticket backend rebind in serial drain.

Verifies that the model-backend preference is re-read between every ticket in the
serial _run_inner_serial while-loop — so a switch binds on the NEXT ticket instead
of waiting up to 90 min for the next cycle boundary.

Fail-first checks for each testable acceptance criterion:
  1. Serial 2-ticket worklist, preference flipped A→B after ticket #1:
     backends.current() at ticket #2 start is B, not A.
  2. cfg.model_backend is also updated to B at that same ticket-#2 boundary.
  3. With active plan_limit_hit=True, a preference change STILL binds on the
     next ticket (the re-read path has NO plan-limit gate).
  4. AC3 preserved: ticket #1 dispatches entirely on its original backend even
     though the preference changes during its pass — switch observed only from
     next pick onward.
  5. Corrupt/missing preference store during the boundary re-read never raises;
     the drain continues on the current backend.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import types

# ── Stub external modules before any orchestrator import ────────────────
for _mod in ("claude_agent_sdk",):
    _sdk = types.ModuleType(_mod)

    class _D:
        def __init__(self, *a, **k):
            self.__dict__.update(k)

        def __call__(self, *a, **k):
            return self

    _sdk.__getattr__ = lambda n: _D
    sys.modules[_mod] = _sdk

os.environ.pop("GLM_MODEL", None)
os.environ.pop("GLM_MODEL_MID", None)

sys.path.insert(0, ".")


results: list[tuple[str, bool, str]] = []


def chk(n: str, ok: bool, detail: str = "") -> None:
    results.append((n, ok, detail))


# ===========================================================================
# Minimal stubs
# ===========================================================================

class _StubAudit:
    def __init__(self):
        self.events: list[dict] = []

    def record(self, event: str, **fields: object) -> None:
        self.events.append({"event": event, **fields})


class _WorklistTicket:
    def __init__(self, key: str):
        self.id = key
        self.key = key
        self.ephemeral = False


class _WorklistApp:
    def __init__(self, name: str = "automatixy"):
        self.name = name


class _StubGit:
    def ensure_clean(self) -> None:
        pass


def _stub_make_git(*a, **k):
    return _StubGit()


def _stub_make_backlog(*a, **k):
    return None


class _NullExitStack:
    def enter_context(self, cm):
        class NCM:
            __enter__ = lambda s: None
            __exit__ = lambda s, *a: None
        return NCM()

    def close(self) -> None:
        pass


def _store_path(cfg) -> str:
    """Derive the model_backend.json store path from cfg.audit_path."""
    audit = getattr(cfg, "audit_path", None)
    if audit:
        return os.path.join(os.path.dirname(audit), "model_backend.json")
    raise ValueError("cfg must have audit_path")


def _write_pref(cfg, backend: str) -> None:
    """Write a backend preference JSON into cfg's store directory."""
    store = _store_path(cfg)
    os.makedirs(os.path.dirname(store), exist_ok=True)
    with open(store, "w") as f:
        json.dump({"backend": backend}, f)


# Captured state across process_ticket calls
_captured: dict = {}


async def _run_serial(worklist, initial_backend="opus", capture_fn=None):
    """Run _run_inner_serial with all dependencies mocked.

    All tests share a single temp dir for the Config's state, so preferences
    written via _write_pref inside cap() are read correctly by active().
    """
    from orchestrator.backends import current as bk_current
    from orchestrator.config import Config, AppConfig
    import orchestrator.loop as _lm

    if capture_fn is None:
        capture_fn = lambda tid, caps, cfg: None

    old_pt = _lm.process_ticket
    old_make_git = _lm._make_git

    async def fake_process_ticket(ticket, app, cfg, git, backlog, audit, budget,
                                  stop_event=None):
        _captured["id"] = ticket.id
        _captured["backend"] = bk_current()
        _captured["cfg_mb"] = getattr(cfg, "model_backend", "<unset>")
        capture_fn(ticket.id, _captured, cfg)
        from orchestrator.contracts import Outcome, TicketReport
        return TicketReport(ticket.id, Outcome.SUCCESS, 0, 0.0, app.name)

    _lm.process_ticket = fake_process_ticket
    _lm._make_git = _stub_make_git
    _lm.make_backlog = _stub_make_backlog
    _lm.run_logger = types.SimpleNamespace(
        open_run_log=lambda *a, **k: None,
        close_run_log=lambda *a, **k: None,
    )
    _lm.WorktreeBusy = Exception
    _lm.ExitStack = _NullExitStack

    try:
        tmpdir = tempfile.mkdtemp()
        store = _store_path(Config(apps=[],
                                   audit_path=os.path.join(tmpdir, "audit.jsonl")))

        # Pre-write initial preference
        with open(store, "w") as f:
            json.dump({"backend": initial_backend}, f)

        cfg = Config(apps=[AppConfig(name="automatixy", repo_path=tmpdir,
                                    base_branch="DEV", protected_branch="MAIN",
                                    backlog_backend="none")],
                     audit_path=os.path.join(tmpdir, "audit.jsonl"),
                     use_worktree=False)
        await _lm._run_inner_serial(cfg, worklist, _StubAudit())
        return cfg
    finally:
        _lm.process_ticket = old_pt
        _lm._make_git = old_make_git
        _lm.make_backlog = _stub_make_backlog
        _lm.WorktreeBusy = Exception
        _lm.ExitStack = _NullExitStack


# ===========================================================================
# TESTS
# ===========================================================================

async def test_ac1_preference_flip_after_ticket_1():
    """AC1: Serial 2-ticket worklist, preference flipped A->B after ticket #1:
    backends.current() at ticket #2 start is B, not A."""
    from orchestrator.backends import set_backend, reset_backend, NATIVE

    tok = set_backend(NATIVE)
    try:
        tickets_seen: list[str] = []
        ticket_a_backend: str | None = None
        ticket_b_backend: str | None = None

        def cap(tid, caps, cfg):
            tickets_seen.append(tid)
            if tid == "TICKET-A":
                nonlocal ticket_a_backend
                ticket_a_backend = caps["backend"]
                # Simulate Commander changing pref AFTER ticket finishes
                _write_pref(cfg, "glm")
            elif tid == "TICKET-B":
                nonlocal ticket_b_backend
                ticket_b_backend = caps["backend"]

        _captured.clear()
        worklist = [
            (_WorklistApp(), _WorklistTicket("TICKET-A")),
            (_WorklistApp(), _WorklistTicket("TICKET-B")),
        ]
        await _run_serial(worklist, initial_backend="opus", capture_fn=cap)

        assert tickets_seen == ["TICKET-A", "TICKET-B"], f"Wrong order: {tickets_seen}"
        chk("AC1: ticket A runs before ticket B", True)
        chk("AC1: backends.current() at ticket A start is 'opus'",
            ticket_a_backend == "opus", f"got {ticket_a_backend!r}")
        chk("AC1: backends.current() at ticket B start is 'glm' (new pref)",
            ticket_b_backend == "glm", f"got {ticket_b_backend!r}")
    finally:
        reset_backend(tok)


async def test_ac2_cfg_model_backend_updated_at_boundary():
    """AC2: At ticket #2 boundary cfg.model_backend is also updated to the new
    backend value."""
    from orchestrator.backends import set_backend, reset_backend, NATIVE

    tok = set_backend(NATIVE)
    try:
        ticket_a_cfg_mb: str | None = None
        ticket_b_cfg_mb: str | None = None

        def cap(tid, caps, cfg):
            if tid == "TICKET-A":
                nonlocal ticket_a_cfg_mb
                ticket_a_cfg_mb = caps["cfg_mb"]
                _write_pref(cfg, "glm")
            elif tid == "TICKET-B":
                nonlocal ticket_b_cfg_mb
                ticket_b_cfg_mb = caps["cfg_mb"]

        _captured.clear()
        worklist = [
            (_WorklistApp(), _WorklistTicket("TICKET-A")),
            (_WorklistApp(), _WorklistTicket("TICKET-B")),
        ]
        await _run_serial(worklist, initial_backend="opus", capture_fn=cap)

        chk("AC2: cfg.model_backend stays 'opus' at ticket A",
            ticket_a_cfg_mb == "opus", f"got {ticket_a_cfg_mb!r}")
        chk("AC2: cfg.model_backend updated to 'glm' at ticket B boundary",
            ticket_b_cfg_mb == "glm", f"got {ticket_b_cfg_mb!r}")
    finally:
        reset_backend(tok)


async def test_ac3_no_plan_limit_gate_on_re_read():
    """AC3: With simulated active Claude plan limit, a preference change still
    binds on the next ticket."""
    from orchestrator.backends import set_backend, reset_backend, NATIVE
    from orchestrator.usage import _plan_limit_hit_cache

    tok = set_backend(NATIVE)
    try:
        # Seed plan limit cache as hit
        _plan_limit_hit_cache["hit"] = True
        _plan_limit_hit_cache["over_limits"] = [{"key": "session"}]
        _plan_limit_hit_cache["ts"] = 0.0

        ticket_x_backend: str | None = None
        ticket_y_backend: str | None = None

        def cap(tid, caps, cfg):
            if tid == "TICKET-X":
                nonlocal ticket_x_backend
                ticket_x_backend = caps["backend"]
                _write_pref(cfg, "glm")
            elif tid == "TICKET-Y":
                nonlocal ticket_y_backend
                ticket_y_backend = caps["backend"]

        _captured.clear()
        worklist = [
            (_WorklistApp(), _WorklistTicket("TICKET-X")),
            (_WorklistApp(), _WorklistTicket("TICKET-Y")),
        ]
        await _run_serial(worklist, initial_backend="opus", capture_fn=cap)

        chk("AC3: no plan-limit gate — pref change STILL binds at ticket Y",
            ticket_y_backend == "glm", f"got {ticket_y_backend!r}")
    finally:
        reset_backend(tok)


async def test_ac4_mid_pass_preference_change_does_not_affect_current_ticket():
    """AC4: Ticket #1 dispatches entirely on backend A even though the
    preference changes during its pass."""
    from orchestrator.backends import set_backend, reset_backend, NATIVE

    tok = set_backend(NATIVE)
    try:
        ticket_m_backend: str | None = None
        ticket_n_backend: str | None = None

        def cap(tid, caps, cfg):
            if tid == "TICKET-M":
                nonlocal ticket_m_backend
                ticket_m_backend = caps["backend"]
                # Change preference DURING ticket M's pass
                _write_pref(cfg, "glm")
            elif tid == "TICKET-N":
                nonlocal ticket_n_backend
                ticket_n_backend = caps["backend"]

        _captured.clear()
        worklist = [
            (_WorklistApp(), _WorklistTicket("TICKET-M")),
            (_WorklistApp(), _WorklistTicket("TICKET-N")),
        ]
        await _run_serial(worklist, initial_backend="opus", capture_fn=cap)

        chk("AC4: ticket M dispatched on ORIGINAL backend ('opus')",
            ticket_m_backend == "opus", f"got {ticket_m_backend!r}")
        chk("AC4: ticket N picks up NEW backend ('glm')",
            ticket_n_backend == "glm", f"got {ticket_n_backend!r}")
    finally:
        reset_backend(tok)


async def test_ac5_corrupt_preference_store_does_not_break_drain():
    """AC5: A corrupt/missing preference store during the boundary re-read
    never raises out of the loop."""
    from orchestrator.backends import set_backend, reset_backend, NATIVE
    import orchestrator.backend_pref as bp_mod

    tok = set_backend(NATIVE)
    try:
        orig_active = bp_mod.active

        def _bad_active(*a, **k):
            raise OSError("corrupt preference store")

        bp_mod.active = _bad_active
        try:
            tickets_processed = [0]

            def cap(tid, caps, cfg):
                tickets_processed[0] += 1

            _captured.clear()
            worklist = [
                (_WorklistApp(), _WorklistTicket("TICKET-CORRUPT-1")),
                (_WorklistApp(), _WorklistTicket("TICKET-CORRUPT-2")),
            ]
            # Should NOT raise
            await _run_serial(worklist, initial_backend="opus", capture_fn=cap)

            chk("AC5: corrupt pref store does not crash drain",
                tickets_processed[0] == 2,
                f"{tickets_processed[0]}/2 processed")
            chk("AC5: drain continued on the current backend",
                _captured.get("backend") in ("opus", "glm"),
                f"got {_captured.get('backend')!r}")
        finally:
            bp_mod.active = orig_active
    finally:
        reset_backend(tok)


if __name__ == "__main__":
    asyncio.run(test_ac1_preference_flip_after_ticket_1())
    asyncio.run(test_ac2_cfg_model_backend_updated_at_boundary())
    asyncio.run(test_ac3_no_plan_limit_gate_on_re_read())
    asyncio.run(test_ac4_mid_pass_preference_change_does_not_affect_current_ticket())
    asyncio.run(test_ac5_corrupt_preference_store_does_not_break_drain())

    print("\n========== EU-754 PER-TICKET BACKEND REBIND QA ==========")
    passed = sum(1 for _, ok, _ in results if ok)
    for n, ok, det in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
    print("---------------------------------------------------------")
    print(f"  {passed}/{len(results)} passed")
    print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
    sys.exit(0 if passed == len(results) else 1)
