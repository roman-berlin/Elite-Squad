"""Filing precision bucket — deterministic autofile quality scores per officer.

Tests EU-837: ``filing_precision(cfg, *, min_outcomes=5) -> list[dict]`` — reads the
audit log via ``D.audit_lines``, filters autofiled terminal events, buckets by source_officer,
computes ``precision = merged / total``, excludes officers below ``min_outcomes``, and returns
results sorted by precision ascending (worst first).
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

from orchestrator import consolidate
from orchestrator.config import Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())

# Helper: build a distinct autofiled terminal event each iteration
def _line(event, officer, idx):
    return json.dumps({"event": event, "is_autofiled": True,
                       "source_officer": officer, "idx": idx})

# --------------------------------------------------------------------------- #
# AC1: main computation — 6 land_pushed + 2 needs_human + 2 no_changes,
#      all autofiled, same officer => merged=6, escalated=2, closed_unchanged=2,
#      total=10, precision=0.6
# --------------------------------------------------------------------------- #
qae_lines = []
for i in range(6):
    qae_lines.append(_line("land_pushed", "QA-Engineer", i))
for i in range(2):
    qae_lines.append(_line("needs_human", "QA-Engineer", 10 + i))
for i in range(2):
    qae_lines.append(_line("no_changes", "QA-Engineer", 12 + i))

audit1 = tmp / "ac1_audit.jsonl"
audit1.write_text("\n".join(qae_lines) + "\n", encoding="utf-8")
cfg1 = Config(apps=[], audit_path=str(audit1))

res1 = consolidate.filing_precision(cfg1)
chk("AC1: returns exactly one entry", len(res1) == 1)
entry = res1[0] if res1 else {}
chk("AC1: merged=6", entry.get("merged") == 6, str(entry))
chk("AC1: escalated=2", entry.get("escalated") == 2, str(entry))
chk("AC1: closed_unchanged=2", entry.get("closed_unchanged") == 2, str(entry))
chk("AC1: total=10", entry.get("total") == 10, str(entry))
chk("AC1: precision=0.6", abs(entry.get("precision", float("nan")) - 0.6) < 1e-9,
    f"{entry.get('precision')}")

# --------------------------------------------------------------------------- #
# AC2: non-autofiled events excluded — an officer whose ONLY terminal events
#      lack is_autofiled / have is_autofiled=False must NOT appear.
# --------------------------------------------------------------------------- #
audit2 = tmp / "ac2_audit.jsonl"
audit2.write_text(json.dumps({"event": "land_pushed"}) + "\n"          # no is_autofiled
                  + json.dumps({"event": "needs_human", "is_autofiled": False,
                               "source_officer": "Ghost"}) + "\n",
                encoding="utf-8")
cfg2 = Config(apps=[], audit_path=str(audit2))
res2 = consolidate.filing_precision(cfg2)
chk("AC2: absent event (no field) excluded", len(res2) == 0, str(res2))

# --------------------------------------------------------------------------- #
# AC3: min_outcomes filter — officer with only 4 outcomes (< 5) excluded.
# --------------------------------------------------------------------------- #
audit3 = tmp / "ac3_audit.jsonl"
ac3_entries = [_line("land_pushed", "Junior", i) for i in range(4)]
audit3.write_text("\n".join(ac3_entries) + "\n", encoding="utf-8")
cfg3 = Config(apps=[], audit_path=str(audit3))
res3 = consolidate.filing_precision(cfg3)
chk("AC3: officer with 4 outcomes excluded", len(res3) == 0, str(res3))

# --------------------------------------------------------------------------- #
# AC4: sorted ascending by precision — two qualifying officers; worst-first.
# --------------------------------------------------------------------------- #
audit4 = tmp / "ac4_audit.jsonl"
ac4_lines = []
# Officer A: 3 merged + 0 escal + 2 unchanged = 5 total, prec 0.6
for i in range(3):
    ac4_lines.append(_line("land_pushed", "OfficerA", i))
ac4_lines.append(_line("no_changes", "OfficerA", 10))
ac4_lines.append(_line("no_changes", "OfficerA", 11))
# Officer B: 9 merged + 0 escal + 1 unchanged = 10 total, prec 0.9
for i in range(9):
    ac4_lines.append(_line("land_pushed", "OfficerB", 20 + i))
ac4_lines.append(_line("no_changes", "OfficerB", 30))

audit4.write_text("\n".join(ac4_lines) + "\n", encoding="utf-8")
cfg4 = Config(apps=[], audit_path=str(audit4))
res4 = consolidate.filing_precision(cfg4)
chk("AC4: exactly two entries", len(res4) == 2, str(res4))
chk("AC4: worst first — index 0 is OfficerA (prec 0.6)",
    res4[0]["officer"] == "OfficerA", str(res4))
chk("AC4: best last — index 1 is OfficerB (prec 0.9)",
    res4[1]["officer"] == "OfficerB", str(res4))
chk("AC4: OfficerA precision ~0.6",
    abs(res4[0].get("precision", float("nan")) - 0.6) < 1e-9)
chk("AC4: OfficerB precision ~0.9",
    abs(res4[1].get("precision", float("nan")) - 0.9) < 1e-9)

# --------------------------------------------------------------------------- #
# AC5: run() returns "filing_precision" key, value = list, = [] when nothing.
# --------------------------------------------------------------------------- #
audit5 = tmp / "ac5_audit.jsonl"       # empty — no autofiled terminal events
audit5.write_text("", encoding="utf-8")
cfg5 = Config(apps=[], audit_path=str(audit5))
r5 = consolidate.run(cfg5, write=False)
chk("AC5: run() dict has filing_precision key", "filing_precision" in r5, str(r5.keys()))
chk("AC5: filing_precision is a list", isinstance(r5["filing_precision"], list))
chk("AC5: filing_precision == [] with no autofiled terminal events",
    r5["filing_precision"] == [])

# --------------------------------------------------------------------------- #
# Extra: unknown event names ignored (must not crash; unknown events not counted).
# Uses min_outcomes=1 so we can inspect the single real entry.
# --------------------------------------------------------------------------- #
audit_extra = tmp / "extra_audit.jsonl"
audit_extra.write_text(_line("land_pushed", "X", 0) + "\n"
                       + json.dumps({"event": "unknown_thing", "is_autofiled": True,
                                     "source_officer": "X", "idx": 1}) + "\n",
                     encoding="utf-8")
cfg_extra = Config(apps=[], audit_path=str(audit_extra))
res_extra = consolidate.filing_precision(cfg_extra, min_outcomes=1)
chk("extra: unknown event ignored (only 1 counted, not 2)",
    len(res_extra) == 1 and res_extra[0]["total"] == 1, str(res_extra))

print("\n============= FILING PRECISION QA =============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
