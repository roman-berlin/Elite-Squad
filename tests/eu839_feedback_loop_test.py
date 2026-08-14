"""EU-839 — Filing-precision line in the daily standup brief.

Verifies EU-839 AC (the final integration seam): ``dashboard.standup(cfg)`` surfaces
one deterministic line summarizing per-officer filing precision when any officer's
merged/total drops below the bar (0.5 with >=5 outcomes).

The sibling pieces are assumed tested here; this test wires the **last unbuilt seam**:
the brief-line. It covers four acceptance criteria.

Fail-first: every sub-test uses a unique temp audit path so results don't compound.
"""
import sys, types, tempfile, json
from pathlib import Path
from datetime import datetime, timezone, timedelta

# --- Stub heavy deps so modules import without them ------------------------ #
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

sys.path.insert(0, ".")

from orchestrator import dashboard, consolidate
from orchestrator.config import Config

results: list[tuple[str, bool, str]] = []

def chk(label: str, condition: bool, detail: str = ""):
    """Record one assertion."""
    results.append((label, bool(condition), detail))


# --------------------------------------------------------------------------- #
# Helpers — synthesise autofiled terminal events and seed recent-shipped audits
# --------------------------------------------------------------------------- #

def _ts(offset_hours: int) -> str:
    """Return an ISO-8601 timestamp offset from ``now`` by the given hours."""
    return (datetime.now(timezone.utc) - timedelta(hours=offset_hours)).isoformat()


def _seed_audit(path: Path, entries: list[dict]) -> None:
    """Write audit lines; each entry becomes one JSON line via json.dumps."""
    lines = [json.dumps(e, default=str) for e in entries]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _filing_precision_line(brief_lines: list[str]) -> str | None:
    """Extract the 📉 filing-precision line (if present)."""
    for line in brief_lines:
        if "📉" in line and ("Filing precision" in line or "precision" in line.lower()):
            return line
    return None


# =========================================================================== #
# AC1 — Autofiled officer BELOW bar => standup contains a 📉 line
# =========================================================================== #

tmp1 = Path(tempfile.mkdtemp())

# Build 10 autofiled terminal events for OfficerLow: prec = 3 merged / 10 = 0.3
audit1 = tmp1 / "ac1.jsonl"
ac1_events = []
for i in range(3):
    ac1_events.append({"ts": _ts(i + 1), "event": "land_pushed", "ticket_id": f"AUTO-1{i}",
                       "is_autofiled": True, "source_officer": "OfficerLow"})
for i in range(5):
    ac1_events.append({"ts": _ts(i + 4), "event": "needs_human", "ticket_id": f"AUTO-2{i}",
                       "is_autofiled": True, "source_officer": "OfficerLow"})
for i in range(2):
    ac1_events.append({"ts": _ts(i + 9), "event": "no_changes", "ticket_id": f"AUTO-3{i}",
                       "is_autofiled": True, "source_officer": "OfficerLow"})

_seed_audit(audit1, ac1_events)
cfg1 = Config(apps=[], audit_path=str(audit1))

brief1 = dashboard.standup(cfg1)
lines1 = brief1.splitlines()
fp_line = _filing_precision_line(lines1)

chk("AC1: standup returns a non-empty string", isinstance(brief1, str) and len(brief1) > 0,
    f"len={len(brief1)}")
chk("AC1: 📉 filing-precision line is present", fp_line is not None,
    f"lines: {lines1[-5:] if lines1 else 'empty'}")
chk("AC1: line names the offending officer ('OfficerLow')",
    fp_line is not None and "OfficerLow" in fp_line, repr(fp_line))
chk("AC1: line contains '30%' (prec~0.3 → 30%)",
    fp_line is not None and "30%" in fp_line, repr(fp_line))

# Sanity: verify consolidate.filing_precision agrees on the numbers
prec_results = consolidate.filing_precision(cfg1)
chk("AC1: consolidate sees OfficerLow with precision ~0.3",
    len(prec_results) == 1 and abs(prec_results[0]["precision"] - 0.3) < 1e-9,
    str(prec_results))

# =========================================================================== #
# AC2 — No qualifying officer (all above bar or too few outcomes) => NO 📉 line
# =========================================================================== #

tmp2 = Path(tempfile.mkdtemp())

audit2 = tmp2 / "ac2.jsonl"
# OfficerHigh: 9/10 merged = 0.9 — above bar
ac2_events = []
for i in range(9):
    ac2_events.append({"ts": _ts(i + 1), "event": "land_pushed", "ticket_id": f"HI-{i}",
                       "is_autofiled": True, "source_officer": "OfficerHigh"})
for i in range(1):
    ac2_events.append({"ts": _ts(i + 10), "event": "needs_human", "ticket_id": f"HI-err",
                       "is_autofiled": True, "source_officer": "OfficerHigh"})
# OfficerFew: only 3 outcomes (< min_outcomes=5) — should NOT appear even if bad
for i in range(3):
    ac2_events.append({"ts": _ts(i + 1), "event": "needs_human", "ticket_id": f"FE-{i}",
                       "is_autofiled": True, "source_officer": "OfficerFew"})

_seed_audit(audit2, ac2_events)
cfg2 = Config(apps=[], audit_path=str(audit2))

brief2 = dashboard.standup(cfg2)
lines2 = brief2.splitlines()
fp_line2 = _filing_precision_line(lines2)

chk("AC2: filing-precision line absent when all officers qualify (above bar / thin)",
    fp_line2 is None, repr(lines2[-5:] if lines2 else "empty"))
chk("AC2: no false 📉 emission", "📉" not in brief2, "found spurious emoji in brief")

# Also double-check consolidates agrees: no alerts under the bar
alerts = consolidate.filing_precision(cfg2)
officers_below = [r for r in alerts if r["precision"] < 0.5]
chk("AC2: consolidate finds no below-bar officers",
    len(officers_below) == 0, str(alerts))

# =========================================================================== #
# AC3 — Best-effort: if filing_precision raises, standup still returns full brief
# =========================================================================== #

# Temporarily monkey-patch consolidate.filing_precision to raise
_original_fp = consolidate.filing_precision

def _raise_fp(*_a, **_k):
    raise RuntimeError("simulated filing_precision crash")

try:
    consolidate.filing_precision = _raise_fp
    cfg3 = Config(apps=[], audit_path="/tmp/x.jsonl")  # nonexistent path is fine
    brief3 = dashboard.standup(cfg3)
    lines3 = brief3.splitlines()
    has_header = any("Daily stand-up" in l for l in lines3)
    chk("AC3: standup still returns the brief despite filing_precision raising",
        has_header and len(brief3) > 0,
        f"header_found={has_header}, len={len(brief3)}")
    chk("AC3: no exception propagates out",
        True, "No Crash")
finally:
    consolidate.filing_precision = _original_fp

# =========================================================================== #
# AC4 — End-to-end: autofiled terminal audit events carry source_officer
#       through origin_audit_fields → land_pushed/needs_human/no_changes,
#       filing_precision reads them, standup renders the 📉 line.
# =========================================================================== #

# Synthesize exactly what intake.origin_audit_fields would stamp onto a terminal event.
# We simulate: an autofiled ticket from "Scout" whose 5+ outcomes go through land_pushed etc.
# with is_autofiled=True and source_officer stamped by intake.

origin_audit_fields_result = {"is_autofiled": True, "source_officer": "Scout"}

tmp4 = Path(tempfile.mkdtemp())
audit4 = tmp4 / "ac4.jsonl"

ac4_events = []
# 2 landed (good), 2 escalated (bad), 1 unchanged (neutral) => prec = 2/5 = 0.4
ac4_events.append({
    "ts": _ts(1), "event": "land_pushed", "ticket_id": "AUTO-E01",
    **origin_audit_fields_result,
})
ac4_events.append({
    "ts": _ts(2), "event": "land_pushed", "ticket_id": "AUTO-E02",
    **origin_audit_fields_result,
})
ac4_events.append({
    "ts": _ts(3), "event": "needs_human", "ticket_id": "AUTO-E03",
    **origin_audit_fields_result,
})
ac4_events.append({
    "ts": _ts(4), "event": "needs_human", "ticket_id": "AUTO-E04",
    **origin_audit_fields_result,
})
ac4_events.append({
    "ts": _ts(5), "event": "no_changes", "ticket_id": "AUTO-E05",
    **origin_audit_fields_result,
})

_seed_audit(audit4, ac4_events)
cfg4 = Config(apps=[], audit_path=str(audit4))

# Step 1: filing_precision picks up Scout at 0.4
prec_e2e = consolidate.filing_precision(cfg4)
chk("AC4: filing_precision reports Scout with prec 0.4",
    len(prec_e2e) == 1 and prec_e2e[0]["officer"] == "Scout"
    and abs(prec_e2e[0]["precision"] - 0.4) < 1e-9,
    str(prec_e2e))

# Step 2: standup renders the 📉 line with Scout
brief4 = dashboard.standup(cfg4)
fp_line4 = _filing_precision_line(brief4.splitlines())
chk("AC4: 📉 line present in standup for the same officer Scout",
    fp_line4 is not None and "Scout" in fp_line4, repr(fp_line4))
chk("AC4: 📉 line mentions ~40% precision",
    fp_line4 is not None and "40%" in fp_line4, repr(fp_line4))

# Step 3: the pipeline end-to-end connects audit stamp → consolidate → brief
chk("AC4: both paths name the SAME officer (Scout)",
    fp_line4 is not None and "Scout" in fp_line4 and prec_e2e[0]["officer"] == "Scout",
    "officer mismatch between consolidate and brief")

# =========================================================================== #
# Summary
# =========================================================================== #

passed = sum(1 for _, ok, _ in results if ok)
failed = [(lbl, det) for lbl, ok, det in results if not ok]

print("\n========= EU-839 FEEDBACK LOOP INTEGRATION QA =========")
for lbl, ok, det in results:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {lbl}" + (f": {det}" if det and not ok else ""))
print("-------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAILS")
sys.exit(0 if passed == len(results) else 1)
