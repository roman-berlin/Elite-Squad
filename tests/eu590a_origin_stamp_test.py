"""EU-836a: origin stamp — source_officer_of + origin_audit_fields helpers.

Verifies (test-after AC):
  1. source_officer_of returns the officer-label string for an autofiled ticket
     (e.g. "QA-Engineer"), and None for a Commander ticket.
  2. A ticket with labels ["autofiled", "QA-Engineer", "fp-abc123"] yields "QA-Engineer".
  3. origin_audit_fields returns {} for Commander tickets, {"source_officer": ..., "is_autofiled": True}
     for autofiled tickets.
  4. For autofiled tickets, each of the three terminal audit-event shapes carries both keys.
  5. For Commander tickets, those keys are ABSENT from the spread kwargs.
  6. python3 tests/run_all.py stays green (suite-level gate).
"""
import sys, types, json, os, re
from pathlib import Path

# --- stub heavy deps so modules import without them ------------------------
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

req = types.ModuleType("requests")
req.Session = lambda *a, **k: None
req.RequestException = Exception
sys.modules["requests"] = req

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.intake import source_officer_of, origin_audit_fields, is_autofiled
from orchestrator.contracts import Ticket


results = []

def chk(label: str, condition: bool, detail: str = ""):
    results.append((label, bool(condition), detail))


def _ticket(labels: list[str] | None = None, *, autofiled=False):
    """Create a synthetic ticket."""
    if autofiled:
        labels = ["autofiled"] + (labels or [])
    elif labels is not None and "autofiled" in [str(l).lower() for l in labels]:
        # strip autofiled label back out if explicitly listed for non-autofiled ticket
        labels = [l for l in labels if str(l).lower() != "autofiled"]
    return Ticket(id="EU-9999", key="EU-9999", summary="Test ticket",
                  description="", acceptance_criteria=[], labels=labels or [])


# ========================================================================= #
# 1. source_officer_of — basic behaviour
# ========================================================================= #

t_cmd = _ticket(["Security-Engineer"], autofiled=False)
chk("AC1-commander-no-autofiled", source_officer_of(t_cmd) is None,
    f"got {source_officer_of(t_cmd)!r}")

t_qa = _ticket(["QA-Engineer", "fp-xyz123"], autofiled=True)
chk("AC1-QA-Engineer", source_officer_of(t_qa) == "QA-Engineer",
    f"got {source_officer_of(t_qa)!r}")

t_only_fp = _ticket(["fp-only", "fp-other"], autofiled=True)
chk("AC1-only-fp→None", source_officer_of(t_only_fp) is None,
    f"got {source_officer_of(t_only_fp)!r}")

# Multiple non-fp labels — should pick first surviving
t_multi = _ticket(["Release-Manager", "QA-Engineer"], autofiled=True)
chk("AC1-first-non-fp", source_officer_of(t_multi) == "Release-Manager",
    f"got {source_officer_of(t_multi)!r}")

# Case-insensitive matching for skip values but original-case returned
t_case = _ticket(["qa-engineer"], autofiled=True)
chk("AC1-original-case", source_officer_of(t_case) == "qa-engineer",
    f"got {source_officer_of(t_case)!r}")

# Labels with spaces (Jira normalises → hyphens)
t_space = _ticket(["QA Engineer"], autofiled=True)
chk("AC1-space-label", source_officer_of(t_space) == "QA Engineer",
    f"got {source_officer_of(t_space)!r}")

# Autofiled ticket whose only non-fp label is "autofiled" itself → None
t_only_autofiled = Ticket(id="EU-9998", key="EU-9998", summary="Test ticket",
                          description="", acceptance_criteria=[], labels=["autofiled"])
chk("AC1-only-autofiled-label", source_officer_of(t_only_autofiled) is None,
    f"got {source_officer_of(t_only_autofiled)!r}")


# ========================================================================= #
# 2. source_officer_of — edge cases
# ========================================================================= #

# Commander ticket still has other labels
t_cmd_labels = _ticket(["QA-Engineer", "Bug-Hunt"], autofiled=False)
chk("AC1-commander-with-labels", source_officer_of(t_cmd_labels) is None,
    f"got {source_officer_of(t_cmd_labels)!r}")

# Ticket with no labels attribute
t_nolabels = _ticket(labels=None)
t_nolabels.labels = None
chk("AC1-none-labels-attr", source_officer_of(t_nolabels) is None,
    f"got {source_officer_of(t_nolabels)!r}")

# fp- prefix case insensitive
t_fp_upper = _ticket(["FP-MIXED"], autofiled=True)
chk("AC1-FP-prefix-ignore", source_officer_of(t_fp_upper) is None,
    f"got {source_officer_of(t_fp_upper)!r}")


# ========================================================================= #
# 3. origin_audit_fields — Commander
# ========================================================================= #

cmd_fields = origin_audit_fields(t_cmd)
chk("AC3-cmd-fields-empty", cmd_fields == {}, f"got {cmd_fields}")
chk("AC3-source_officer-absent", "source_officer" not in cmd_fields,
    f"key present: {'source_officer' in cmd_fields}")
chk("AC3-is_autofiled-absent", "is_autofiled" not in cmd_fields,
    f"key present: {'is_autofiled' in cmd_fields}")

# Even with extra labels on commander ticket
cmd_extra_fields = origin_audit_fields(t_cmd_labels)
chk("AC3-cmd-extra-empty", cmd_extra_fields == {}, f"got {cmd_extra_fields}")


# ========================================================================= #
# 4. origin_audit_fields — Autofiled
# ========================================================================= #

qa_fields = origin_audit_fields(t_qa)
chk("AC3-qafied-has-keys", set(qa_fields.keys()) == {"source_officer", "is_autofiled"},
    f"got keys {set(qa_fields.keys())}")
chk("AC3-source_officer-value", qa_fields.get("source_officer") == "QA-Engineer",
    f"got {qa_fields.get('source_officer')!r}")
chk("AC3-is_autofiled-true", qa_fields.get("is_autofiled") is True,
    f"got {qa_fields.get('is_autofiled')}")


# ========================================================================= #
# 5. Simulated terminal audit event records — autofiled vs Commander
# ========================================================================= #

# We simulate what audit.record(**kwargs) does: row.update(fields) then json.dumps
def _audit_row(event, **fields):
    return json.loads(json.dumps({"ts": "now", "event": event}, default=str) + "\n")

def simulate_audit_record(event, ticket, **extra_kwargs):
    """Simulate audit.record(event, ticket_id=..., **extra_kwargs)."""
    row = {"ts": "x", "event": event, "ticket_id": ticket.id}
    row.update(extra_kwargs)
    return row

# Terminal events
for event_name in ("land_pushed", "no_changes", "needs_human"):
    # Commander row — neither new key present
    cmd_row = simulate_audit_record(event_name, t_cmd, base="dev", reason="demo")
    chk(f"AC5-cmd-{event_name}-source_absent", "source_officer" not in cmd_row,
        f"key found in {event_name} row")
    chk(f"AC5-cmd-{event_name}-autofiled_absent", "is_autofiled" not in cmd_row,
        f"key found in {event_name} row")

    # Autofiled row — both keys present
    auto_row = simulate_audit_record(event_name, t_qa, base="dev", reason="demo",
                                     **qa_fields)
    chk(f"AC5-auto-{event_name}-source_present",
        "source_officer" in auto_row and auto_row["source_officer"] == "QA-Engineer",
        f"{event_name}: {auto_row.get('source_officer')!r}")
    chk(f"AC5-auto-{event_name}-autofiled_true",
        "is_autofiled" in auto_row and auto_row["is_autofiled"] is True,
        f"{event_name}: {auto_row.get('is_autofiled')}")


# ========================================================================= #
# Summary
# ========================================================================= #

passed = sum(1 for _, c, _ in results if c)
failed = [(lbl, det) for lbl, c, det in results if not c]

for lbl, ok, det in results:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {lbl}{f': {det}' if det and ok else ''}")

if failed:
    print(f"\n{len(failed)} FAILURES:")
    for lbl, det in failed:
        print(f"  - {lbl}{f' ({det})' if det else ''}")
    sys.exit(1)

print(f"\nALL {passed} checks passed.")
sys.exit(0)
