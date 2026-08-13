"""Actuate low-precision alerts: Unit Memory lesson, audit event, per-officer TICKET_BLOCK_RULE note.

Tests EU-838 — three additive changes triggered when ``filing_precision()`` drops below a bar
(default 50%) with enough outcomes:
  A — Unit Memory lesson folded via _fold()
  B — filing_precision_alerts list returned in run()'s dict
  C — set_officer_block_note + ticket_block_rule_for() wiring

Fail-first: every check uses a fresh Config pointing at a unique temp audit path so no
inter-test leakage corrupts results.
"""
import sys, types, tempfile, json
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import consolidate, filing

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())

# --------------------------------------------------------------------------- #
# Helper — build a distinct autofiled terminal event each iteration
# --------------------------------------------------------------------------- #
def _line(event, officer, idx):
    return json.dumps({"event": event, "is_autofiled": True,
                       "source_officer": officer, "idx": idx})

# --------------------------------------------------------------------------- #
# RESET per-officer notes between sub-tests so they don't compound
# --------------------------------------------------------------------------- #
filing._OFFICER_BLOCK_NOTES.clear()


# =========================================================================== #
# AC1 — low-precision officer (0.4 / 10 outcomes) generates a lesson with name
#       and percentage in the added bullets.
# =========================================================================== #
audit1 = tmp / "ac1_audit.jsonl"
lines1 = []
for i in range(4):
    lines1.append(_line("land_pushed", "OfficerX", i))          # 4 merged
for i in range(4):
    lines1.append(_line("needs_human", "OfficerX", 10 + i))     # 4 escalated
for i in range(2):
    lines1.append(_line("no_changes", "OfficerX", 14 + i))      # 2 unchanged
audit1.write_text("\n".join(lines1) + "\n", encoding="utf-8")
cfg1 = type("Cfg", (), {"apps": [], "audit_path": str(audit1)})()

r1 = consolidate.run(cfg1, write=False)

# r1["added"] must contain a bullet with "OfficerX" and "40%"
found_ac1 = False
for b in r1.get("added", []):
    if "OfficerX" in b and "40%" in b:
        found_ac1 = True
        break
chk("AC1: low-precision lesson contains officer name 'OfficerX' and '40%'",
    found_ac1, str(r1.get("added", [])))

# Also verify the filing_precision_alerts entry exists for this officer
found_alert = any(e.get("officer") == "OfficerX" for e in r1.get("filing_precision_alerts", []))
chk("AC1: filing_precision_alerts includes OfficerX", found_alert,
    str(r1.get("filing_precision_alerts")))


# =========================================================================== #
# AC2 — officer AT 50% does NOT get a lesson or note.
# =========================================================================== #
filing._OFFICER_BLOCK_NOTES.clear()

audit2 = tmp / "ac2_audit.jsonl"
lines2 = []
for i in range(5):
    lines2.append(_line("land_pushed", "SafeOfficer", i))        # 5 merged
for i in range(5):
    lines2.append(_line("needs_human", "SafeOfficer", 10 + i))   # 5 escalated => total=10, prec=0.5
audit2.write_text("\n".join(lines2) + "\n", encoding="utf-8")
cfg2 = type("Cfg", (), {"apps": [], "audit_path": str(audit2)})()

r2 = consolidate.run(cfg2, write=False)

prec_lesson = any("SafeOfficer" in b for b in r2.get("added", []))
chk("AC2: officer at 50% gets NO precision lesson in added bullets",
    not prec_lesson, str(r2.get("added", [])))

alert_2 = [e for e in r2.get("filing_precision_alerts", []) if e.get("officer") == "SafeOfficer"]
chk("AC2: SafeOfficer does NOT appear in filing_precision_alerts",
    len(alert_2) == 0, str(alert_2))
chk("AC2: SafeOfficer gets NO per-officer block note either",
    "SafeOfficer" not in filing._OFFICER_BLOCK_NOTES,
    str(sorted(filing._OFFICER_BLOCK_NOTES)))


# =========================================================================== #
# AC3 — run()'s filing_precision_alerts is a list of dicts with keys:
#       officer, precision, total (only low-bar officers included).
# =========================================================================== #
filing._OFFICER_BLOCK_NOTES.clear()

audit3 = tmp / "ac3_audit.jsonl"
lines3 = []
# OfficerA: 2 merged + 8 escalated => prec=0.2 (below bar)
for i in range(2):
    lines3.append(_line("land_pushed", "OfficerA", i))
for i in range(8):
    lines3.append(_line("needs_human", "OfficerA", 10 + i))
# OfficerB: 8 merged + 2 escalated => prec=0.8 (above bar, should NOT appear)
for i in range(8):
    lines3.append(_line("land_pushed", "OfficerB", 20 + i))
for i in range(2):
    lines3.append(_line("needs_human", "OfficerB", 30 + i))
audit3.write_text("\n".join(lines3) + "\n", encoding="utf-8")
cfg3 = type("Cfg", (), {"apps": [], "audit_path": str(audit3)})()

r3 = consolidate.run(cfg3, write=False)
alerts3 = r3.get("filing_precision_alerts", [])
chk("AC3: exactly one alert entry (OfficerA only)", len(alerts3) == 1, str(alerts3))
if alerts3:
    entry = alerts3[0]
    chk("AC3: entry has keys officer, precision, total",
        set(entry.keys()) == {"officer", "precision", "total"}, str(entry))
    chk("AC3: officer='OfficerA', precision~0.2, total=10",
        entry.get("officer") == "OfficerA" and abs(entry["precision"] - 0.2) < 1e-9
        and entry["total"] == 10, str(entry))


# =========================================================================== #
# AC4 — after run() processes a low-precision officer,
#       ticket_block_rule_for(officer_label) returns str > bare constant.
# =========================================================================== #
filing._OFFICER_BLOCK_NOTES.clear()

audit4 = tmp / "ac4_audit.jsonl"
lines4 = []
for i in range(4):
    lines4.append(_line("land_pushed", "LowPrecOff", i))           # 4 merged
for i in range(6):
    lines4.append(_line("needs_human", "LowPrecOff", 10 + i))     # 6 escalated => total=10, prec=0.4
audit4.write_text("\n".join(lines4) + "\n", encoding="utf-8")
cfg4 = type("Cfg", (), {"apps": [], "audit_path": str(audit4)})()

consolidate.run(cfg4, write=False)

full = filing.ticket_block_rule_for("LowPrecOff")
bare = filing.TICKET_BLOCK_RULE
chk("AC4: ticket_block_rule_for('LowPrecOff') longer than bare constant",
    len(full) > len(bare), f"full={len(full)} vs bare={len(bare)}")
chk("AC4: result still contains bare TICKET_BLOCK_RULE as substring",
    bare in full, "substring check failed")


# =========================================================================== #
# AC5 — ticket_block_rule_for("unknown") returns bare constant (equal, no KeyError).
# =========================================================================== #
note_before_len = len(filing._OFFICER_BLOCK_NOTES)
result5 = filing.ticket_block_rule_for("unknown-nonexistent-label")
chk("AC5: unknown label returns bare constant (equal length)",
    result5 == filing.TICKET_BLOCK_RULE, repr(result5[:60]))
chk("AC5: no extra key was created in _OFFICER_BLOCK_NOTES",
    len(filing._OFFICER_BLOCK_NOTES) == note_before_len,
    f"{list(filing._OFFICER_BLOCK_NOTES.keys())}")


# =========================================================================== #
# AC6 — (integration) grep-based verification lives in run_all; here we just
#       confirm the imports resolve correctly without throwing.
# =========================================================================== #
try:
    from orchestrator.scout import recon as _scout_recon
    from orchestrator.provost import inspect as _provost_inspect
    from orchestrator.quartermaster import inspect as _qm_inspect
    from orchestrator.reviewer import REVIEWER_SYSTEM
    from orchestrator.pm import PM_TRIAGE_SYSTEM
    # council needs app config, skip import-time check; already imported above
    chk("AC6: officer-module imports resolve cleanly", True)
except Exception as e:
    chk("AC6: officer-module imports resolve cleanly", False, str(e))

# Verify the constants still contain the bare TICKET_BLOCK_RULE substring
# (critical invariant: ticket_block_rule_for(x) must always include bare constant)
chk("AC6: REVIEWER_SYSTEM contains bare TICKET_BLOCK_RULE substring",
    filing.TICKET_BLOCK_RULE in REVIEWER_SYSTEM, "missing substring")
chk("AC6: PM_TRIAGE_SYSTEM contains bare TICKET_BLOCK_RULE substring",
    filing.TICKET_BLOCK_RULE in PM_TRIAGE_SYSTEM, "missing substring")


# =========================================================================== #
# Summary
# =========================================================================== #
print("\n============ EU-838 PRECISION ACTUATION QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAILS")
sys.exit(0 if passed == len(results) else 1)
