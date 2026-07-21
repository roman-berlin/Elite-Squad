"""EU-395 QA — cross-run memory: a re-picked ticket carries a bounded digest of its own PAST
attempts (errored/needs_human/review-FAIL events read from the audit log) into the Planner/Builder
context, instead of re-planning and re-building blind every run.

Pins:
  - a first attempt (no prior audit events for this ticket_id) carries NO digest.
  - a second attempt carries the first attempt's failure reason.
  - the digest is capped in items+chars via builder._cap_feedback's own trimming (EU-38 discipline),
    keeping the NEWEST points and marking elisions, exactly like the reviewer-feedback cap.
  - an unreadable audit log fails toward "no digest" — never raises, never blocks the build.
"""
import json
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

# ---- SDK stub (must precede orchestrator imports that reach agent.py) ---- #
sdk = types.ModuleType("claude_agent_sdk")


class _Msg:
    def __init__(self, **kw):
        self.__dict__.update(kw)


for _n in ("AssistantMessage", "ResultMessage", "TextBlock", "ToolUseBlock", "UserMessage",
           "SystemMessage", "ThinkingBlock", "ToolResultBlock"):
    setattr(sdk, _n, type(_n, (_Msg,), {}))


class ClaudeAgentOptions:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class ProcessError(Exception):
    def __init__(self, message="", exit_code=None, stderr=None):
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


class CLINotFoundError(Exception):
    pass


async def _query(*a, **k):
    if False:
        yield None


sdk.ClaudeAgentOptions = ClaudeAgentOptions
sdk.ProcessError = ProcessError
sdk.CLINotFoundError = CLINotFoundError
sdk.query = _query
sys.modules["claude_agent_sdk"] = sdk

sys.path.insert(0, ".")

results: list[tuple[str, bool, str]] = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


from orchestrator import loop as loop_mod  # noqa: E402
from orchestrator.contracts import Ticket  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="eu395_"))


class _Audit:
    """Minimal stand-in for AuditLog.record — just appends a JSON line, same shape the real
    AuditLog writes (ts + event + fields)."""
    def __init__(self, path: Path):
        self.path = path

    def record(self, event, **fields):
        row = {"ts": "2026-07-21T00:00:00+0000", "event": event}
        row.update(fields)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")


# ================= first attempt: no prior events -> no digest ================= #
audit_p1 = _tmp / "first" / "audit.jsonl"
audit_p1.parent.mkdir(parents=True, exist_ok=True)
audit_p1.write_text(
    json.dumps({"ts": "2026-07-20T00:00:00+0000", "event": "ticket_start", "ticket_id": "EU-OTHER"}) + "\n",
    encoding="utf-8")
cfg1 = SimpleNamespace(audit_path=str(audit_p1))
chk("first attempt: no digest when no prior events exist for this ticket",
    loop_mod._prior_attempts_digest(cfg1, "EU-900") is None)

tk1 = Ticket(id="EU-900", key="EU-900", summary="s", description="original description",
             acceptance_criteria=["a"])
audit1 = _Audit(audit_p1)
out1 = loop_mod._inject_prior_attempts(cfg1, tk1, audit1)
chk("first attempt: ticket description is untouched", out1.description == "original description",
    out1.description)
chk("first attempt: no prior_attempts_injected event recorded",
    not any('"prior_attempts_injected"' in ln for ln in audit_p1.read_text().splitlines()))

# ================= second attempt: carries the first attempt's failure reason ================= #
audit_p2 = _tmp / "second" / "audit.jsonl"
audit_p2.parent.mkdir(parents=True, exist_ok=True)
FAIL_REASON = "TypeError: cannot read property 'x' of undefined in leads.ts:42"
audit_p2.write_text(
    json.dumps({"ts": "2026-07-20T10:00:00+0000", "event": "ticket_exception",
                "ticket_id": "EU-901", "error": FAIL_REASON}) + "\n"
    + json.dumps({"ts": "2026-07-20T10:00:01+0000", "event": "ticket_exception",
                  "ticket_id": "EU-OTHER", "error": "unrelated ticket, must not leak in"}) + "\n",
    encoding="utf-8")
cfg2 = SimpleNamespace(audit_path=str(audit_p2))
digest2 = loop_mod._prior_attempts_digest(cfg2, "EU-901")
chk("second attempt: digest is produced", digest2 is not None, str(digest2))
chk("second attempt: digest carries the first attempt's failure reason",
    bool(digest2) and FAIL_REASON in digest2, str(digest2))
chk("second attempt: digest does not leak another ticket's events",
    bool(digest2) and "unrelated ticket" not in digest2, str(digest2))

tk2 = Ticket(id="EU-901", key="EU-901", summary="s", description="original description",
             acceptance_criteria=["a"])
audit2 = _Audit(audit_p2)
out2 = loop_mod._inject_prior_attempts(cfg2, tk2, audit2)
chk("second attempt: description now carries the digest",
    FAIL_REASON in (out2.description or ""), out2.description)
chk("second attempt: original description is preserved",
    (out2.description or "").startswith("original description"), out2.description)
chk("second attempt: prior_attempts_injected event recorded",
    any('"prior_attempts_injected"' in ln and '"EU-901"' in ln for ln in audit_p2.read_text().splitlines()))

# ================= review-FAIL events feed the digest too ================= #
audit_p3 = _tmp / "review" / "audit.jsonl"
audit_p3.parent.mkdir(parents=True, exist_ok=True)
audit_p3.write_text(
    json.dumps({"ts": "2026-07-20T11:00:00+0000", "event": "review", "ticket_id": "EU-902",
                "verdict": "FAIL", "summary": "missing edge-case test",
                "required_changes": ["add a null-input test"]}) + "\n"
    + json.dumps({"ts": "2026-07-20T11:00:01+0000", "event": "review", "ticket_id": "EU-902",
                  "verdict": "PASS", "summary": "looks fine"}) + "\n",
    encoding="utf-8")
cfg3 = SimpleNamespace(audit_path=str(audit_p3))
digest3 = loop_mod._prior_attempts_digest(cfg3, "EU-902")
chk("review FAIL: digest captures the review failure", bool(digest3) and "missing edge-case test" in digest3,
    str(digest3))
chk("review FAIL: a PASS verdict is not folded in", bool(digest3) and "looks fine" not in digest3, str(digest3))

# ================= capped: items + chars, newest kept, elision marked ================= #
audit_p4 = _tmp / "capped" / "audit.jsonl"
audit_p4.parent.mkdir(parents=True, exist_ok=True)
lines = []
for i in range(10):
    lines.append(json.dumps({"ts": f"2026-07-20T{i:02d}:00:00+0000", "event": "ticket_exception",
                              "ticket_id": "EU-903", "error": f"failure number {i}"}))
audit_p4.write_text("\n".join(lines) + "\n", encoding="utf-8")
cfg4 = SimpleNamespace(audit_path=str(audit_p4))
digest4 = loop_mod._prior_attempts_digest(cfg4, "EU-903")
chk("cap: digest bounded to at most PRIOR_ATTEMPTS_MAX_ITEMS(+marker) lines",
    bool(digest4) and digest4.count("\n") + 1 <= loop_mod._PRIOR_ATTEMPTS_MAX_ITEMS + 1, str(digest4))
chk("cap: digest total length respects the char ceiling",
    bool(digest4) and len(digest4) <= loop_mod._PRIOR_ATTEMPTS_MAX_CHARS + 200, str(len(digest4 or "")))
chk("cap: newest failure survives the cap", bool(digest4) and "failure number 9" in digest4, str(digest4))
chk("cap: oldest failure is dropped", bool(digest4) and "failure number 0" not in digest4, str(digest4))

# ================= an unreadable audit log fails toward "no digest" ================= #
cfg_bad = SimpleNamespace(audit_path=str(_tmp))   # a directory, not a file — read must fail cleanly
try:
    bad_digest = loop_mod._prior_attempts_digest(cfg_bad, "EU-904")
    chk("unreadable audit log: fails toward no digest, never raises", bad_digest is None, str(bad_digest))
except Exception as exc:  # noqa: BLE001
    chk("unreadable audit log: fails toward no digest, never raises", False, str(exc))

# ================= tally ================= #
print("\n============ EU-395 PRIOR-ATTEMPTS DIGEST QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
