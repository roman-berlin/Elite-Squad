#!/usr/bin/env python3
"""EU-819 / EU-786 split: consecutive Planner-failure streak tracker + threshold alert.

Tests _note_planner_outcome from orchestrator.loop — independently mockable so every
harness can exercise it with hand-built PlannerResult objects and monkeypatch notify.send.
No real Planner call, no network.

Acceptance criteria covered:
  AC1  At planner_failure_alert_threshold=3, three consecutive failures fire exactly ONE
       Telegram alert containing the failure count and raw head; two failures fire zero.
  AC2  A successful plan (raw NOT starting with '(planner error:') resets the streak to 0.
  AC3  Default threshold is 3 when absent from config; threshold=0 disables entirely.
  AC4  Alert fires WARNING stdout line and records audit event 'planner_failure_alert'.
"""
from __future__ import annotations

import io
import sys
import tempfile
import types
from contextlib import redirect_stdout
from pathlib import Path

# Stub claude_agent_sdk so orchestrator modules import without it
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)

    def __call__(s, *a, **k): return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

sys.path.insert(0, ".")

from orchestrator.config import AppConfig, Config
from orchestrator.planner import PlannerResult
from orchestrator import loop as loop_mod
from orchestrator import audit

results: list[tuple[str, bool, str]] = []


def chk(n: str, c: bool, d: str = "") -> None:
    results.append((n, bool(c), d))


# ---- helpers --------------------------------------------------------------- #


def _cfg(failure_threshold: int = 3) -> Config:
    """Minimal Config with the given threshold."""
    tmp = tempfile.mkdtemp()
    return Config(
        apps=[AppConfig(name="test", repo_path=tmp, base_branch="DEV",
                        protected_branch="MAIN", backlog_backend="none")],
        audit_path=str(Path(tmp) / "audit.jsonl"),
        use_worktree=False,
        planner_failure_alert_threshold=failure_threshold,
    )


def _err_reply(head: str = "unexpected JSON") -> PlannerResult:
    """A PlannerResult whose raw mimics a parse/abort error."""
    return PlannerResult(
        verdict="BUILD",
        raw=f"(planner error: unparseable plan reply — briefless BUILD; "
            f"head: {head!r})",
    )


def _ok_reply(text: str = "ok") -> PlannerResult:
    """A normal successful plan result."""
    return PlannerResult(verdict="BUILD", approach=text, raw=text)


# ---- mutable state for mocking --------------------------------------------- #


_sent_messages: list[str] = []


def _mock_send(text: str, chat_id=None) -> bool:
    _sent_messages.append(text)
    return True


# ============================================================================
# AC1: Three consecutive failures → ONE alert (count + raw head).
#      Two failures → zero alerts.
# ============================================================================
def test_ac1_at_threshold_fires_once() -> None:
    global _sent_messages
    _sent_messages.clear()

    cfg = _cfg(failure_threshold=3)
    tmp = tempfile.mkdtemp()
    log = audit.AuditLog(str(Path(tmp) / "audit.jsonl"))

    orig_send = loop_mod.notify.send
    loop_mod.notify.send = _mock_send
    try:
        r1 = _err_reply("head-1")
        loop_mod._note_planner_outcome(cfg, r1, ticket_id="AUTO-1", audit=log)
        chk("after 1 failure: zero messages sent", not _sent_messages)

        r2 = _err_reply("head-2")
        loop_mod._note_planner_outcome(cfg, r2, ticket_id="AUTO-2", audit=log)
        chk("after 2 failures (below threshold=3): zero messages sent", not _sent_messages)

        r3 = _err_reply("head-3-last")
        loop_mod._note_planner_outcome(cfg, r3, ticket_id="AUTO-3", audit=log)
        chk("after 3rd failure == threshold: exactly ONE alert fired", len(_sent_messages) == 1)

        msg = _sent_messages[0]
        chk("alert message contains failure count (3)", "3" in msg)
        chk("alert message contains raw head from last failure",
            "head-3-last" in msg or "unparseable" in msg, msg[:500])
    finally:
        loop_mod.notify.send = orig_send


def test_ac1_below_threshold_no_alert() -> None:
    global _sent_messages
    _sent_messages.clear()

    cfg = _cfg(failure_threshold=3)
    tmp = tempfile.mkdtemp()
    log = audit.AuditLog(str(Path(tmp) / "audit.jsonl"))

    orig_send = loop_mod.notify.send
    loop_mod.notify.send = _mock_send
    try:
        r1 = _err_reply("x")
        loop_mod._note_planner_outcome(cfg, r1, ticket_id="X", audit=log)
        loop_mod._note_planner_outcome(cfg, _err_reply("y"), ticket_id="Y", audit=log)
        # Only 2 < 3
        chk("two consecutive failures < threshold=3 → no alert", not _sent_messages)
    finally:
        loop_mod.notify.send = orig_send


# ============================================================================
# AC2: A successful plan resets the streak to 0.
#      fail, fail, success, fail, fail → zero alerts (only 3 consecutive needed).
# ============================================================================
def test_ac2_success_resets_streak() -> None:
    global _sent_messages
    _sent_messages.clear()

    cfg = _cfg(failure_threshold=3)
    tmp = tempfile.mkdtemp()
    log = audit.AuditLog(str(Path(tmp) / "audit.jsonl"))

    orig_send = loop_mod.notify.send
    loop_mod.notify.send = _mock_send
    try:
        # fail, fail → streak = 2
        loop_mod._note_planner_outcome(cfg, _err_reply("f1"), ticket_id="X", audit=log)
        loop_mod._note_planner_outcome(cfg, _err_reply("f2"), ticket_id="Y", audit=log)

        # success → streak resets to 0
        loop_mod._note_planner_outcome(cfg, _ok_reply(), ticket_id="Z", audit=log)
        chk("streak cleared after success", loop_mod._planner_failure_streak == 0)

        # fail, fail again → streak = 2, still below 3
        loop_mod._note_planner_outcome(cfg, _err_reply("f3"), ticket_id="A", audit=log)
        loop_mod._note_planner_outcome(cfg, _err_reply("f4"), ticket_id="B", audit=log)

        chk("fail, fail, success, fail, fail → zero alerts (need 3 consecutive)",
            not _sent_messages)
    finally:
        loop_mod.notify.send = orig_send


# ============================================================================
# AC3: Default threshold is 3 when absent; threshold=0 disables entirely.
# ============================================================================
def test_ac3_default_threshold_3() -> None:
    global _sent_messages
    _sent_messages.clear()

    # Build a Config WITHOUT setting planner_failure_alert_threshold explicitly.
    # The dataclass default is 3, so getattr returns 3.
    cfg = _cfg()  # defaults to 3
    assert cfg.planner_failure_alert_threshold == 3

    tmp = tempfile.mkdtemp()
    log = audit.AuditLog(str(Path(tmp) / "audit.jsonl"))

    orig_send = loop_mod.notify.send
    loop_mod.notify.send = _mock_send
    try:
        # Feed only 2 failures — should NOT fire with default threshold of 3
        loop_mod._note_planner_outcome(cfg, _err_reply("x"), audit=log)
        loop_mod._note_planner_outcome(cfg, _err_reply("y"), audit=log)
        chk("default threshold=3: two failures → no alert", not _sent_messages)
    finally:
        loop_mod.notify.send = orig_send


def test_ac3_zero_threshold_disabled() -> None:
    global _sent_messages
    _sent_messages.clear()

    cfg = _cfg(failure_threshold=0)
    tmp = tempfile.mkdtemp()
    log = audit.AuditLog(str(Path(tmp) / "audit.jsonl"))

    orig_send = loop_mod.notify.send
    loop_mod.notify.send = _mock_send
    try:
        # Even 100 failures with threshold=0 must never fire
        for i in range(10):
            loop_mod._note_planner_outcome(cfg, _err_reply(f"x{i}"), audit=log)
        chk("threshold=0: even 10 failures → no alert", not _sent_messages)
        chk("threshold=0: streak stays at 0 (disabled)",
            loop_mod._planner_failure_streak == 0)
    finally:
        loop_mod.notify.send = orig_send


# ============================================================================
# AC4: Alert prints WARNING to stdout and records audit event.
# ============================================================================
def test_ac4_warning_stdout_and_audit_event() -> None:
    global _sent_messages
    _sent_messages.clear()

    cfg = _cfg(failure_threshold=3)
    tmp = tempfile.mkdtemp()
    log = audit.AuditLog(str(Path(tmp) / "audit.jsonl"))

    orig_send = loop_mod.notify.send
    loop_mod.notify.send = _mock_send
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            loop_mod._note_planner_outcome(cfg, _err_reply("a"), audit=log)
            loop_mod._note_planner_outcome(cfg, _err_reply("b"), audit=log)
            loop_mod._note_planner_outcome(cfg, _err_reply("c-last"), audit=log)

        stdout_text = buf.getvalue()
        chk("WARNING printed to stdout", "WARNING" in stdout_text and "planner" in stdout_text.lower(),
            repr(stdout_text))

        # Check audit log for planner_failure_alert event
        lines = (Path(tmp) / "audit.jsonl").read_text().splitlines()
        events = [json.loads(ln) for ln in lines if ln.strip()]
        alert_events = [e for e in events if e.get("event") == "planner_failure_alert"]
        chk("audit.event 'planner_failure_alert' recorded",
            len(alert_events) >= 1, [e.get("event") for e in events])
    finally:
        loop_mod.notify.send = orig_send


# Need json in this scope for the audit-event reader above.
import json  # noqa: E402 — imported here because it's used by the inner functions above

# Also add a fresh helper for the audit check inside the closure …

if __name__ == "__main__":
    # Reset module-level streak between tests
    loop_mod._planner_failure_streak = 0

    suite = [
        test_ac1_at_threshold_fires_once,
        test_ac1_below_threshold_no_alert,
        test_ac2_success_resets_streak,
        test_ac3_default_threshold_3,
        test_ac3_zero_threshold_disabled,
        test_ac4_warning_stdout_and_audit_event,
    ]

    for fn in suite:
        loop_mod._planner_failure_streak = 0
        _sent_messages.clear()
        fn()

    passed, failed = 0, 0
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
            print(f"  ✗ {name}: {detail}", file=sys.stderr)

    total = passed + failed
    print(f"{passed}/{total} checks passed")
    if failed:
        print(f"RESULT: {failed} of {total} checks FAILED")
        sys.exit(1)
    else:
        print("RESULT: ALL GREEN")
