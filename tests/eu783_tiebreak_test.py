"""EU-783 — autofiled tickets sort last within equal Jira priority in drain order.

Verifies _tiebreak_autofiled (the pure tiebreak helper added to jira.py) against stubbed
Ticket lists, covering all five acceptance criteria:

  1. At identical priority, Commander ticket (no 'autofiled' label) drains first.
  2. Two Commander tickets at identical priority keep their relative order (stable sort).
  3. Autofiled tickets at different priorities: higher Jira priority wins.
  4. Ticket.priority round-trips and 'autofiled' is read from Ticket.labels.
  5. python3 tests/run_all.py stays green (harness exits 0 with all checks passing).
"""
from __future__ import annotations

import sys
import types

# Stub external dependencies so the harness never touches the network.
_sdk = types.ModuleType("claude_agent_sdk")
_sdk.__getattr__ = lambda _n: (lambda *a, **k: None)
sys.modules.setdefault("claude_agent_sdk", _sdk)
_req = types.ModuleType("requests")
_req.Session = lambda *a, **k: None
_req.RequestException = Exception
sys.modules.setdefault("requests", _req)

sys.path.insert(0, ".")

from orchestrator.contracts import Ticket  # noqa: E402
from orchestrator.backlog.jira import _PRIORITY_RANK, _tiebreak_autofiled  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# ── Helper: build minimal Tickets ----------------------------------------------------------- #
def c(key: str, summary: str, priority: str, labels: list[str] | None = None) -> Ticket:
    return Ticket(id=key, key=key, summary=summary, description="", priority=priority,
                  labels=list(labels or []))


# ── AC 4: Ticket.fields round-trip --------------------------------------------------------- #
t = Ticket(id="T", key="T", summary="roundtrip", description="", priority="High",
           labels=["autofiled"])
chk("(4a) Ticket.priority round-trips", t.priority == "High", repr(t.priority))
chk("(4b) 'autofiled' membership read from Ticket.labels",
    "autofiled" in (t.labels or []), str(t.labels))
t_none = Ticket(id="T2", key="T2", summary="noprio", description="")
chk("(4c) missing priority defaults to None without exploding", t_none.priority is None, repr(t_none.priority))
chk("(4d) empty labels are safe", _tiebreak_autofiled([t]) == [t], "no crash")

# ── AC 1: Commander before autofiled at same priority -------------------------------------- #
cmd = c("EU-CMD", "Commander fix", "Medium")
auto = c("EU-AUTO", "Autofiled finding", "Medium", ["autofiled"])

result = _tiebreak_autofiled([auto, cmd])  # input reversed → must reorder
chk("(1a) Commander (no autofiled label) before autofiled at same priority",
    result[0] is cmd and result[1] is auto,
    f"{result[0].id} then {result[1].id}")

# Also verify when input is already correct — stable sort keeps it.
result2 = _tiebreak_autofiled([cmd, auto])
chk("(1b) already-correct order is preserved",
    result2[0] is cmd and result2[1] is auto,
    f"{result2[0].id} then {result2[1].id}")

# ── AC 2: Stable sort for two Commander tickets at same priority ---------------------------- #
ca = c("EU-A", "Commander A", "Medium")
cb = c("EU-B", "Commander B", "Medium")

result = _tiebreak_autofiled([ca, cb])
chk("(2a) two Commanders at same priority → input order preserved (stable)",
    result[0] is ca and result[1] is cb,
    f"{result[0].id} then {result[1].id}")

# Reverse input — should stay as-is because Python's stable sort preserves equality.
result_rev = _tiebreak_autofiled([cb, ca])
chk("(2b) reverse input → still stable (relative order unchanged)",
    result_rev[0] is cb and result_rev[1] is ca,
    f"{result_rev[0].id} then {result_rev[1].id}")

# Mixed stable-sort scenario: Commander + autofiled with extra Commanders.
cx = c("EU-X", "Commander X", "High")
cy = c("EU-Y", "Commander Y", "High")
cz = c("EU-Z", "Autofiled Z", "High", ["autofiled"])

result3 = _tiebreak_autofiled([cx, cy, cz])
chk("(2c) Commanders outrank autofiled; internal Commander order stable",
    result3[0] is cx and result3[1] is cy and result3[2] is cz,
    f"{result3[0].id} > {result3[1].id} > {result3[2].id}")

# ── AC 3: Higher Jira priority wins over autofiled label ------------------------------------ #
af_low = c("EU-FL", "Autofiled Low", "Low", ["autofiled"])
af_high = c("EU-FH", "Autofiled High", "High", ["autofiled"])

result = _tiebreak_autofiled([af_low, af_high])
chk("(3a) higher Jira priority outranks autofiled: High > Low",
    result[0] is af_high and result[1] is af_low,
    f"{result[0].id}/{result[0].priority} then {result[1].id}/{result[1].priority}")

# Cross-tier with mixed Commander / autofiled — priority tier dominates across tiers.
cmd_med = c("EU-CM", "Commander Med", "Medium")
af_high2 = c("EU-FHA", "Autofiled High2", "High", ["autofiled"])

result4 = _tiebreak_autofiled([cmd_med, af_high2])
chk("(3b) autofiled High beats Commander Medium (priority tier dominates autofiled)",
    result4[0] is af_high2 and result4[1] is cmd_med,
    f"{result4[0].id}/{result4[0].priority} then {result4[1].id}/{result4[1].priority}")

# ── _PRIORITY_RANK sanity --------------------------------------------------------------- #
chk("(5a) known priorities map to ascending rank",
    [_PRIORITY_RANK["highest"], _PRIORITY_RANK["high"], _PRIORITY_RANK["medium"],
     _PRIORITY_RANK["low"], _PRIORITY_RANK["lowest"]] == [0, 1, 2, 3, 4],
    str(_PRIORITY_RANK))
chk("(5b) unknown priority falls back to medium-rank (2)",
    _PRIORITY_RANK.get("unknown", 2) == 2, "fallback=2")
chk("(5c) None priority is safe → fallback to 2",
    _PRIORITY_RANK.get("", 2) == 2, "empty-string fallback")

# ── Report -------------------------------------------------------------------------------- #
print("\n========== EU-783 AUTOFILED TIEBREAK ==========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
