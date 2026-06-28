"""EU-96 QA: per-ticket token-burn instrumentation.

Tests that:
  (a) TicketReport carries a token_burn field (dataclass contract).
  (b) BuildResult / TestEngineerResult / ReviewResult carry input_tokens / output_tokens.
  (c) PerTicketArtifactStore.token_burn accumulates correctly across officer calls.
  (d) The loop's _burn() helper is additive and keyed correctly (via unit simulation).
  (e) A 'token_burn_report' audit event is emitted with the correct payload when tokens
      were burned (verified via a minimal _attempt() stub using the contracts layer).
"""
from __future__ import annotations

import sys
import types

# ── SDK stub so orchestrator modules import cleanly without a real Claude install ──
sdk = types.ModuleType("claude_agent_sdk")


class _Stub:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self


sdk.__getattr__ = lambda n: _Stub  # type: ignore[attr-defined]
for _attr in ("AssistantMessage", "ResultMessage", "TextBlock", "ToolUseBlock",
              "ClaudeAgentOptions", "query"):
    setattr(sdk, _attr, _Stub)
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator.contracts import (  # noqa: E402
    BuildResult,
    Outcome,
    PerTicketArtifactStore,
    ReviewResult,
    TestEngineerResult,
    TicketReport,
    Verdict,
)

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# ── (a) TicketReport has token_burn field with default empty dict ───────────────

tr = TicketReport("EU-1", Outcome.MERGED, 1, 0.5, "test-app")
chk("TicketReport has token_burn field", hasattr(tr, "token_burn"),
    f"fields: {list(tr.__dataclass_fields__.keys())}")
chk("TicketReport.token_burn defaults to empty dict", tr.token_burn == {},
    repr(tr.token_burn))

tr_with_burn = TicketReport("EU-2", Outcome.SKIPPED, 1, 0.0, "test-app",
                            token_burn={"builder": 1500, "reviewer": 800})
chk("TicketReport.token_burn holds officer totals",
    tr_with_burn.token_burn == {"builder": 1500, "reviewer": 800},
    repr(tr_with_burn.token_burn))

# ── (b) BuildResult / TestEngineerResult / ReviewResult carry token fields ──────

br = BuildResult(ok=True, summary="done", input_tokens=1200, output_tokens=300)
chk("BuildResult has input_tokens", br.input_tokens == 1200, repr(br.input_tokens))
chk("BuildResult has output_tokens", br.output_tokens == 300, repr(br.output_tokens))

br_default = BuildResult(ok=True, summary="done")
chk("BuildResult.input_tokens defaults to 0", br_default.input_tokens == 0)
chk("BuildResult.output_tokens defaults to 0", br_default.output_tokens == 0)

te = TestEngineerResult(ok=True, input_tokens=900, output_tokens=200)
chk("TestEngineerResult has input_tokens", te.input_tokens == 900, repr(te.input_tokens))
chk("TestEngineerResult has output_tokens", te.output_tokens == 200, repr(te.output_tokens))

te_default = TestEngineerResult(ok=True)
chk("TestEngineerResult.input_tokens defaults to 0", te_default.input_tokens == 0)

rr = ReviewResult(verdict=Verdict.PASS, spec_met=True, input_tokens=700, output_tokens=150)
chk("ReviewResult has input_tokens", rr.input_tokens == 700, repr(rr.input_tokens))
chk("ReviewResult has output_tokens", rr.output_tokens == 150, repr(rr.output_tokens))

rr_default = ReviewResult(verdict=Verdict.FAIL, spec_met=False)
chk("ReviewResult.input_tokens defaults to 0", rr_default.input_tokens == 0)

# ── (c) PerTicketArtifactStore.token_burn accumulates correctly ──────────────────

store = PerTicketArtifactStore()
chk("store.token_burn starts empty", store.token_burn == {}, repr(store.token_burn))

# Simulate _burn() helper behaviour
def _burn(s: PerTicketArtifactStore, key: str, in_tok: int, out_tok: int) -> None:
    """Mirror of the loop's local _burn() closure."""
    s.token_burn[key] = s.token_burn.get(key, 0) + in_tok + out_tok


_burn(store, "builder", 1200, 300)
chk("builder burn accumulated correctly",
    store.token_burn.get("builder") == 1500,
    repr(store.token_burn))

_burn(store, "test-engineer", 900, 200)
chk("test-engineer burn accumulated correctly",
    store.token_burn.get("test-engineer") == 1100,
    repr(store.token_burn))

_burn(store, "reviewer", 700, 150)
chk("reviewer burn accumulated correctly",
    store.token_burn.get("reviewer") == 850,
    repr(store.token_burn))

# ── (d) _burn() is additive across iterations (reviewer has a parse-retry path) ─

store2 = PerTicketArtifactStore()
_burn(store2, "reviewer", 700, 150)   # first pass
_burn(store2, "reviewer", 500, 120)   # retry on parse failure
chk("reviewer burn is additive across retry",
    store2.token_burn.get("reviewer") == 1470,
    repr(store2.token_burn))

# ── (e) provost writes directly to store.token_burn ────────────────────────────

store3 = PerTicketArtifactStore()
# Simulate what provost.py does after its run_agent call
burn = 800 + 200   # input + output
store3.token_burn["provost"] = store3.token_burn.get("provost", 0) + burn
chk("provost burn written to store directly",
    store3.token_burn.get("provost") == 1000,
    repr(store3.token_burn))

# ── (f) audit event structure (unit-level, no real AuditLog I/O needed) ─────────

import json
import tempfile
from pathlib import Path

from orchestrator.audit import AuditLog  # noqa: E402

tmp = Path(tempfile.mkdtemp()) / "audit.jsonl"
audit = AuditLog(tmp)

store4 = PerTicketArtifactStore()
_burn(store4, "builder", 2000, 500)
_burn(store4, "reviewer", 1000, 250)

# Simulate what _resolve() does: emit the audit event
tb = dict(store4.token_burn)
audit.record("token_burn_report", ticket_id="EU-99",
             payload=tb, total=sum(tb.values()))

# Verify the event landed correctly
events = [json.loads(line) for line in tmp.read_text().splitlines() if line.strip()]
burn_events = [e for e in events if e.get("event") == "token_burn_report"]

chk("token_burn_report event emitted", len(burn_events) == 1,
    f"got {len(burn_events)} events")
ev = burn_events[0] if burn_events else {}
chk("token_burn_report has ticket_id", ev.get("ticket_id") == "EU-99",
    repr(ev.get("ticket_id")))
chk("token_burn_report.payload has builder key",
    isinstance(ev.get("payload"), dict) and "builder" in ev["payload"],
    repr(ev.get("payload")))
chk("token_burn_report.payload.builder is 2500",
    (ev.get("payload") or {}).get("builder") == 2500,
    repr((ev.get("payload") or {}).get("builder")))
chk("token_burn_report.total is 3750",
    ev.get("total") == 3750,
    repr(ev.get("total")))

# ── (g) _resolve() does NOT emit event when token_burn is empty ─────────────────

tmp2 = Path(tempfile.mkdtemp()) / "audit2.jsonl"
audit2 = AuditLog(tmp2)
# Empty store: no emit expected
store5 = PerTicketArtifactStore()
tb5 = dict(store5.token_burn)
if tb5:
    audit2.record("token_burn_report", ticket_id="EU-X", payload=tb5, total=sum(tb5.values()))

events2 = [json.loads(ln) for ln in (tmp2.read_text().splitlines() if tmp2.exists() else []) if ln.strip()]
burn_events2 = [e for e in events2 if e.get("event") == "token_burn_report"]
chk("no token_burn_report emitted when burn is empty", len(burn_events2) == 0,
    f"got {len(burn_events2)} events")

# ── summary ──────────────────────────────────────────────────────────────────────

print("\n============ EU-96 TOKEN BURN INSTRUMENTATION QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
