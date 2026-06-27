"""EU-88 — Specialist-provisioning approval: ask-once / park / no-spam.

Tests (all without real models or Jira):
  1. get/set spec_approval round-trip (pending → approved → declined).
  2. pending_specialist_approvals returns only pending entries.
  3. Dedup: _run_synthesis returns None on second call when "pending" — no model call, no Telegram.
  4. Declined state: _run_synthesis returns None without model call.
  5. Approved state: _run_synthesis proceeds to synthesis and returns BuildResult.
  6. Jira existence gate: _run_synthesis returns None and notifies when ticket not in Jira.
  7. Non-automode first call: Telegram msg posted exactly once, status recorded as "pending".
  8. Automode: no Telegram gate, no state recorded, proceeds directly.
  9. resolve_specialist_approval_reply — approve path: sets "approved" + triggers re-run.
  10. resolve_specialist_approval_reply — decline path: sets "declined", no re-run.
  11. decisions.handle_reply delegates to hr.resolve_specialist_approval_reply when no pending
      decision entry matches.
  12. No roster/approval is proposed for a non-existent Jira ticket (phantom guard).
  13. needs.summary includes specialist_approvals in total.
"""
import asyncio
import sys
import tempfile
import types
from pathlib import Path

# ── Stub the Claude Agent SDK so no model calls happen ──────────────────────
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import hr
from orchestrator import squad
from orchestrator.agent import AgentRun
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket, BuildRequest

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ── helpers ──────────────────────────────────────────────────────────────────

def _cfg(tmp_dir: Path, *, auto_mode: bool = False, dry_run: bool = False) -> Config:
    audit = tmp_dir / "audit.jsonl"
    return Config(
        apps=[AppConfig(name="automatixy", repo_path=str(tmp_dir),
                        base_branch="DEV", protected_branch="MAIN",
                        backlog_backend="none")],
        audit_path=str(audit),
        use_worktree=False,
        auto_model=False,
        auto_mode=auto_mode,
        dry_run=dry_run,
    )


def _ticket(tid="AUTO-90", summary="Build MQL5 EA") -> Ticket:
    return Ticket(id=tid, key=tid, summary=summary,
                  description="Build an expert advisor in MQL5.",
                  acceptance_criteria=["EA compiles", "strategy tester passes"],
                  app="automatixy")


def _req(tid="AUTO-90") -> BuildRequest:
    return BuildRequest(_ticket(tid), "DEV", iteration=1)


GOOD_CHARTERS = [
    {"name": "MQL5 Algo Engineer", "lane_key": "mql5-algo", "identity": "owns the EA",
     "knowledge": "mql5 docs", "skills": ["write EA", "verify"], "constraints": "no other files",
     "domain_gate": "echo gate-pass", "ephemeral": True},
]

_notify_calls: list[str] = []

def _patch_notify():
    """Replace notify.send with a list-collector for the test session."""
    import orchestrator.notify as _n
    _n._orig_send = getattr(_n, "_orig_send", _n.send)
    _n.send = lambda msg: _notify_calls.append(msg)

_patch_notify()


# ── 1. get/set round-trip ────────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as _d:
    _cfg1 = _cfg(Path(_d))
    chk("get_spec_approval: None initially", hr.get_spec_approval(_cfg1, "AUTO-90", "mql5") is None)
    hr.set_spec_approval(_cfg1, "AUTO-90", "mql5", "pending")
    chk("get_spec_approval: pending after set", hr.get_spec_approval(_cfg1, "AUTO-90", "mql5") == "pending")
    hr.set_spec_approval(_cfg1, "AUTO-90", "mql5", "approved")
    chk("get_spec_approval: approved after update", hr.get_spec_approval(_cfg1, "AUTO-90", "mql5") == "approved")
    hr.set_spec_approval(_cfg1, "AUTO-91", "rust", "declined")
    chk("get_spec_approval: different ticket+domain independent",
        hr.get_spec_approval(_cfg1, "AUTO-91", "rust") == "declined")
    chk("get_spec_approval: case-insensitive key (ticket)",
        hr.get_spec_approval(_cfg1, "auto-90", "MQL5") == "approved")


# ── 2. pending_specialist_approvals filters by status ────────────────────────
with tempfile.TemporaryDirectory() as _d:
    _cfg2 = _cfg(Path(_d))
    hr.set_spec_approval(_cfg2, "AUTO-90", "mql5", "pending")
    hr.set_spec_approval(_cfg2, "AUTO-91", "rust", "approved")
    hr.set_spec_approval(_cfg2, "AUTO-92", "go", "declined")
    _pend = hr.pending_specialist_approvals(_cfg2)
    chk("pending_specialist_approvals: only pending entries", len(_pend) == 1, str(len(_pend)))
    chk("pending_specialist_approvals: correct ticket", _pend[0]["ticket_id"] == "AUTO-90")


# ── stub run_agent for squad._run_synthesis tests ────────────────────────────
_model_call_count = [0]

async def _fake_run_agent_ok(prompt, opts, tag="", **kw):
    _model_call_count[0] += 1
    import json
    return AgentRun(text=json.dumps(GOOD_CHARTERS),
                    final=json.dumps(GOOD_CHARTERS),
                    cost_usd=0.05, num_turns=2, is_error=False, tools=[])

async def _fake_soldier(st, req, app, cfg, idx, total, specialists=None, iteration=1):
    return (AgentRun(text="done", final="implemented", cost_usd=0.1, num_turns=3,
                     is_error=False, tools=["Edit"]), "sonnet")

async def _fake_run_gate(cmd, cwd):
    return ("pass", "gate passed")

_orig_run_agent = squad.run_agent
_orig_soldier = squad._soldier
_orig_run_gate_fn = squad._run_gate
squad.run_agent = _fake_run_agent_ok
squad._soldier = _fake_soldier
squad._run_gate = _fake_run_gate

# Also stub hr.run_agent so synthesize_specialists uses the same stub
hr.run_agent = _fake_run_agent_ok


# ── 3. Dedup: second call with "pending" returns None without model call ─────
with tempfile.TemporaryDirectory() as _d:
    _cfg3 = _cfg(Path(_d), auto_mode=False)
    hr.set_spec_approval(_cfg3, "AUTO-90", "mql5", "pending")
    _model_call_count[0] = 0
    _notify_calls.clear()
    _res = asyncio.run(squad._run_synthesis("mql5", _req(), _cfg3.apps[0], _cfg3))
    chk("dedup (pending): returns None", _res is None)
    chk("dedup (pending): no model call", _model_call_count[0] == 0, str(_model_call_count[0]))
    chk("dedup (pending): no Telegram re-post", all("Specialist roster" not in m for m in _notify_calls))


# ── 4. Declined state: returns None without model call ──────────────────────
with tempfile.TemporaryDirectory() as _d:
    _cfg4 = _cfg(Path(_d), auto_mode=False)
    hr.set_spec_approval(_cfg4, "AUTO-90", "mql5", "declined")
    _model_call_count[0] = 0
    _notify_calls.clear()
    _res = asyncio.run(squad._run_synthesis("mql5", _req(), _cfg4.apps[0], _cfg4))
    chk("declined: returns None", _res is None)
    chk("declined: no model call", _model_call_count[0] == 0, str(_model_call_count[0]))


# ── 5. Approved state: proceeds to dispatch, returns BuildResult ─────────────
with tempfile.TemporaryDirectory() as _d:
    _cfg5 = _cfg(Path(_d), auto_mode=False)
    hr.set_spec_approval(_cfg5, "AUTO-90", "mql5", "approved")
    _model_call_count[0] = 0
    _res = asyncio.run(squad._run_synthesis("mql5", _req(), _cfg5.apps[0], _cfg5))
    chk("approved: returns BuildResult (not None)", _res is not None)
    chk("approved: ok=True (gate passed)", getattr(_res, "ok", None) is True)
    chk("approved: model was called (synthesis ran)", _model_call_count[0] > 0, str(_model_call_count[0]))


# ── 6. Jira existence gate: returns None + notifies for phantom ticket ────────
# Override _ticket_in_backlog to simulate a 404 for "PHANTOM-1".
_orig_ticket_in_backlog = squad._ticket_in_backlog
squad._ticket_in_backlog = lambda tid, app, cfg: tid != "PHANTOM-1"

with tempfile.TemporaryDirectory() as _d:
    _cfg6 = _cfg(Path(_d), auto_mode=False)
    _phantom_req = BuildRequest(
        Ticket(id="PHANTOM-1", key="PHANTOM-1", summary="ghost", description="",
               acceptance_criteria=[], app="automatixy"),
        "DEV", iteration=1,
    )
    _model_call_count[0] = 0
    _notify_calls.clear()
    _res = asyncio.run(squad._run_synthesis("mql5", _phantom_req, _cfg6.apps[0], _cfg6))
    chk("phantom ticket: returns None", _res is None)
    chk("phantom ticket: no model call", _model_call_count[0] == 0, str(_model_call_count[0]))
    chk("phantom ticket: Telegram warning posted",
        any("does not exist in Jira" in m for m in _notify_calls),
        str(_notify_calls))

squad._ticket_in_backlog = _orig_ticket_in_backlog   # restore


# ── 7. Non-automode first call: Telegram posted ONCE, status set to "pending" ─
with tempfile.TemporaryDirectory() as _d:
    _cfg7 = _cfg(Path(_d), auto_mode=False)
    _notify_calls.clear()
    _model_call_count[0] = 0
    _res = asyncio.run(squad._run_synthesis("mql5", _req(), _cfg7.apps[0], _cfg7))
    chk("first call: returns None (parked)", _res is None)
    chk("first call: Telegram msg posted",
        any("Specialist roster" in m for m in _notify_calls), str(_notify_calls))
    chk("first call: status recorded as pending",
        hr.get_spec_approval(_cfg7, "AUTO-90", "mql5") == "pending")
    chk("first call: model was called once (roster generation)", _model_call_count[0] >= 1, str(_model_call_count[0]))
    # Second call with same ticket+domain: dedup fires
    _notify_calls.clear()
    _model_call_count[0] = 0
    _res2 = asyncio.run(squad._run_synthesis("mql5", _req(), _cfg7.apps[0], _cfg7))
    chk("second call: returns None (dedup)", _res2 is None)
    chk("second call: no model call (dedup)", _model_call_count[0] == 0, str(_model_call_count[0]))
    chk("second call: no re-post to Telegram",
        all("Specialist roster" not in m for m in _notify_calls))


# ── 8. Automode: no Telegram gate, no state recorded ────────────────────────
with tempfile.TemporaryDirectory() as _d:
    _cfg8 = _cfg(Path(_d), auto_mode=True)
    _notify_calls.clear()
    _res = asyncio.run(squad._run_synthesis("mql5", _req(), _cfg8.apps[0], _cfg8))
    chk("automode: returns BuildResult (no gate)", _res is not None)
    chk("automode: ok=True", getattr(_res, "ok", None) is True)
    chk("automode: no Telegram notification",
        all("Specialist roster" not in m for m in _notify_calls))
    chk("automode: no state written",
        hr.get_spec_approval(_cfg8, "AUTO-90", "mql5") is None)


# ── 9. resolve_specialist_approval_reply — approve path ──────────────────────
with tempfile.TemporaryDirectory() as _d:
    _cfg9 = _cfg(Path(_d), auto_mode=False)
    # pre-load a pending approval with minimal ticket metadata
    hr.set_spec_approval(_cfg9, "AUTO-90", "mql5", "pending",
                         ticket_summary="Build MQL5 EA",
                         ticket_description="Build an expert advisor.",
                         ticket_ac=[], app_name="automatixy")
    _notify_calls.clear()
    # Stub _rerun_after_specialist_approval so no real bg thread is spawned
    _rerun_calls: list[dict] = []
    _orig_rerun = hr._rerun_after_specialist_approval
    hr._rerun_after_specialist_approval = lambda cfg, audit, entry: _rerun_calls.append(entry)
    _handled = hr.resolve_specialist_approval_reply(_cfg9, None, "AUTO-90", "approve")
    chk("resolve approve: returns True", _handled is True)
    chk("resolve approve: status set to approved",
        hr.get_spec_approval(_cfg9, "AUTO-90", "mql5") == "approved")
    chk("resolve approve: Telegram confirmation sent",
        any("approved" in m.lower() for m in _notify_calls), str(_notify_calls))
    chk("resolve approve: re-run triggered", len(_rerun_calls) == 1, str(_rerun_calls))
    hr._rerun_after_specialist_approval = _orig_rerun   # restore


# ── 10. resolve_specialist_approval_reply — decline path ─────────────────────
with tempfile.TemporaryDirectory() as _d:
    _cfg10 = _cfg(Path(_d), auto_mode=False)
    hr.set_spec_approval(_cfg10, "AUTO-90", "mql5", "pending",
                         ticket_summary="Build MQL5 EA", app_name="automatixy")
    _notify_calls.clear()
    _rerun_calls_dec: list[dict] = []
    _orig_rerun = hr._rerun_after_specialist_approval
    hr._rerun_after_specialist_approval = lambda cfg, audit, entry: _rerun_calls_dec.append(entry)
    _handled = hr.resolve_specialist_approval_reply(_cfg10, None, "AUTO-90", "decline")
    chk("resolve decline: returns True", _handled is True)
    chk("resolve decline: status set to declined",
        hr.get_spec_approval(_cfg10, "AUTO-90", "mql5") == "declined")
    chk("resolve decline: Telegram decline notification sent",
        any("declined" in m.lower() for m in _notify_calls), str(_notify_calls))
    chk("resolve decline: no re-run triggered", len(_rerun_calls_dec) == 0, str(_rerun_calls_dec))
    hr._rerun_after_specialist_approval = _orig_rerun   # restore


# ── 11. decisions.handle_reply delegates to hr.resolve_specialist_approval_reply ──
import orchestrator.decisions as _dec_mod

with tempfile.TemporaryDirectory() as _d:
    _cfg11 = _cfg(Path(_d), auto_mode=False)
    hr.set_spec_approval(_cfg11, "AUTO-90", "mql5", "pending",
                         ticket_summary="Build MQL5 EA", app_name="automatixy")
    # Stub audit
    class _Audit:
        def record(self, *a, **k): pass
    _notify_calls.clear()
    _rerun_calls11: list[dict] = []
    _orig_rerun = hr._rerun_after_specialist_approval
    hr._rerun_after_specialist_approval = lambda cfg, audit, entry: _rerun_calls11.append(entry)
    _handled11 = _dec_mod.handle_reply(_cfg11, _Audit(), "AUTO-90: approve")
    chk("handle_reply delegates specialist approval: returns True", _handled11 is True)
    chk("handle_reply delegates specialist approval: status approved",
        hr.get_spec_approval(_cfg11, "AUTO-90", "mql5") == "approved")
    chk("handle_reply delegates specialist approval: re-run triggered",
        len(_rerun_calls11) == 1, str(_rerun_calls11))
    hr._rerun_after_specialist_approval = _orig_rerun   # restore


# ── 12. No roster for a phantom/non-existent Jira ticket ──────────────────────
# (already tested in #6 above, but confirm message content precisely)
squad._ticket_in_backlog = lambda tid, app, cfg: False   # simulate all tickets absent
with tempfile.TemporaryDirectory() as _d:
    _cfg12 = _cfg(Path(_d), auto_mode=False)
    _notify_calls.clear()
    asyncio.run(squad._run_synthesis("mql5", _req("GHOST-1"), _cfg12.apps[0], _cfg12))
    chk("phantom guard: no specialist_approval state written",
        hr.get_spec_approval(_cfg12, "GHOST-1", "mql5") is None)
    chk("phantom guard: warning contains 'does not exist'",
        any("does not exist" in m for m in _notify_calls), str(_notify_calls))
squad._ticket_in_backlog = _orig_ticket_in_backlog   # restore


# ── 13. needs.summary includes specialist_approvals ──────────────────────────
from orchestrator import needs
with tempfile.TemporaryDirectory() as _d:
    _cfg13 = _cfg(Path(_d))
    hr.set_spec_approval(_cfg13, "AUTO-90", "mql5", "pending",
                         ticket_summary="EA", app_name="automatixy")
    _s = needs.summary(_cfg13)
    chk("needs.summary: specialist_approvals key present", "specialist_approvals" in _s)
    chk("needs.summary: specialist_approvals has pending entry",
        len(_s["specialist_approvals"]) == 1, str(len(_s.get("specialist_approvals", []))))
    chk("needs.summary: total counts specialist_approvals",
        _s["total"] >= 1, str(_s["total"]))


# ── 14. Approved re-run reuses the PINNED roster (no re-synthesis) ────────────
with tempfile.TemporaryDirectory() as _d:
    _cfg14 = _cfg(Path(_d), auto_mode=False)
    # First call posts the request and PINS the synthesized charters to the entry.
    _model_call_count[0] = 0
    asyncio.run(squad._run_synthesis("mql5", _req(), _cfg14.apps[0], _cfg14))
    _pinned = hr.get_spec_charters(_cfg14, "AUTO-90", "mql5")
    chk("pin: charters stored on the pending entry",
        bool(_pinned) and _pinned[0]["lane_key"] == "mql5-algo", str(_pinned))
    # Commander approves → status advances to approved; the pinned charters survive.
    hr.set_spec_approval(_cfg14, "AUTO-90", "mql5", "approved")
    _model_call_count[0] = 0
    _res14 = asyncio.run(squad._run_synthesis("mql5", _req(), _cfg14.apps[0], _cfg14))
    chk("pin: approved re-run returns BuildResult", _res14 is not None)
    chk("pin: approved re-run REUSED pinned charters (no re-synthesis / no model call)",
        _model_call_count[0] == 0, str(_model_call_count[0]))


# ── 15. Ambiguous reply does NOT silently decline ────────────────────────────
with tempfile.TemporaryDirectory() as _d:
    _cfg15 = _cfg(Path(_d), auto_mode=False)
    hr.set_spec_approval(_cfg15, "AUTO-90", "mql5", "pending",
                         ticket_summary="EA", app_name="automatixy")
    _notify_calls.clear()
    _rerun15: list[dict] = []
    _orig_rerun = hr._rerun_after_specialist_approval
    hr._rerun_after_specialist_approval = lambda cfg, audit, entry: _rerun15.append(entry)
    _handled15 = hr.resolve_specialist_approval_reply(_cfg15, None, "AUTO-90", "hmm not sure")
    chk("ambiguous reply: returns True (consumed, not double-handled)", _handled15 is True)
    chk("ambiguous reply: status stays pending (no silent decline)",
        hr.get_spec_approval(_cfg15, "AUTO-90", "mql5") == "pending")
    chk("ambiguous reply: no re-run triggered", len(_rerun15) == 0, str(_rerun15))
    chk("ambiguous reply: Commander asked to clarify (approve or decline)",
        any("approve" in m and "decline" in m for m in _notify_calls), str(_notify_calls))
    hr._rerun_after_specialist_approval = _orig_rerun   # restore


# ── 16. Approved re-run EXPIRES the entry (a future run re-asks) ──────────────
with tempfile.TemporaryDirectory() as _d:
    _cfg16 = _cfg(Path(_d), auto_mode=False)
    hr.set_spec_approval(_cfg16, "AUTO-90", "mql5", "approved", charters=GOOD_CHARTERS)
    _res16 = asyncio.run(squad._run_synthesis("mql5", _req(), _cfg16.apps[0], _cfg16))
    chk("expiry: approved re-run returns BuildResult", _res16 is not None)
    chk("expiry: approval entry cleared after provisioning (re-asks next run)",
        hr.get_spec_approval(_cfg16, "AUTO-90", "mql5") is None)


# ── restore originals ────────────────────────────────────────────────────────
squad.run_agent = _orig_run_agent
squad._soldier = _orig_soldier
squad._run_gate = _orig_run_gate_fn
hr.run_agent = getattr(hr, "run_agent", None)  # already patched at import time


# ── report ───────────────────────────────────────────────────────────────────
print("\n================ EU-88 SPECIALIST APPROVAL QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if (det and not ok) else ""))
print("--------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAILURE(S) ❌")
import sys as _sys
_sys.exit(0 if passed == len(results) else 1)
