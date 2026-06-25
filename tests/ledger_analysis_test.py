"""EU-38 QA — the usage ledger TAGS each build pass with its ticket id (`k`) + pass number (`p`) so
per-pass INPUT tokens are sliceable (AC: 'record input tokens per build pass ... tagged with ticket id
+ pass number'), and the committed ledger-analysis snippet (scripts/ledger_analysis.py) rolls the
ledger up correctly (AC: 'identify the top 3 context contributors' + 'commit a ledger-analysis snippet
under tests/ or scripts/')."""
import json, sys, tempfile, types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")
sys.path.insert(0, "scripts")          # the snippet lives under scripts/ (AC: tests/ or scripts/)

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))

from orchestrator import usage
import ledger_analysis as la

# ============================================================================
# Instrument: a build pass stamps ticket id + pass number into the ledger line.
# ============================================================================
tmp = Path(tempfile.mkdtemp())
usage.configure(str(tmp / "audit.jsonl"))
led = usage._path()

usage.record("claude-sonnet-4-6", 420_000, 3000, 0.0, "builder", ticket_id="EU-38", pass_number=1)
usage.record("claude-opus-4-8", 11_000_000, 5000, 0.0, "builder", ticket_id="AUTO-9", pass_number=4)
usage.record("claude-sonnet-4-6", 50_000, 2000, 0.0, "the-general")   # officer line — NO ticket/pass

lines = [json.loads(x) for x in led.read_text(encoding="utf-8").splitlines() if x.strip()]
builder_lines = [r for r in lines if r.get("g") == "builder"]
check("ledger wrote one line per call", len(lines) == 3, str(len(lines)))
check("build pass carries ticket id (k)", builder_lines[0].get("k") == "EU-38", str(builder_lines[0]))
check("build pass carries pass number (p)", builder_lines[0].get("p") == 1, str(builder_lines[0]))
check("second build pass tagged AUTO-9 / pass 4",
      builder_lines[1].get("k") == "AUTO-9" and builder_lines[1].get("p") == 4, str(builder_lines[1]))
gen = [r for r in lines if r.get("g") == "the-general"][0]
check("officer line stays lean (no k/p keys)", "k" not in gen and "p" not in gen, str(gen))
check("input tokens recorded verbatim on the build pass", builder_lines[0].get("i") == 420_000,
      str(builder_lines[0].get("i")))

# pass_number=0 (e.g. a soldier) must still be written; a junk pass_number must never crash record().
usage.record("claude-sonnet-4-6", 1000, 10, 0.0, "builder", ticket_id="EU-38", pass_number=0)
zero = [json.loads(x) for x in led.read_text(encoding="utf-8").splitlines() if x.strip()][-1]
check("pass_number 0 is still written (not dropped as falsy)", zero.get("p") == 0, str(zero))
try:
    usage.record("m", 1, 1, 0.0, "builder", ticket_id="EU-38", pass_number="oops")
    check("non-int pass_number never crashes record()", True)
except Exception as e:  # noqa: BLE001
    check("non-int pass_number never crashes record()", False, str(e))

# ============================================================================
# Analysis snippet: percentile / median / rollup math (drives the AC 'after' check).
# Asserted on an explicit fixture so the math is deterministic and independent of the
# mutated ledger above; load_rows() is then exercised once against the real file.
# ============================================================================
check("snippet self-test passes (percentile/rollup math)", la._selftest() == 0)
check("median odd", la.median([5, 1, 3]) == 3)
check("median even", la.median([1, 2, 3, 4]) == 2.5)
check("p95 nearest-rank of 1..20 == 19", la.percentile(list(range(1, 21)), 95) == 19)
check("percentile of empty list is 0 (no crash)", la.percentile([], 95) == 0)

fixture = [
    {"i": 11_000_000, "o": 5000, "g": "builder", "k": "AUTO-9", "p": 4},   # the blow-up pass
    {"i": 420_000, "o": 3000, "g": "builder", "k": "EU-38", "p": 1},
    {"i": 800_000, "o": 2000, "g": "builder", "k": "EU-38", "p": 2},
    {"i": 50_000, "o": 4000, "g": "reviewer", "k": "EU-38", "p": 2},
    {"i": 20_000, "o": 1000, "g": "the-general"},
]
split = la.input_output_split(fixture)
check("input dominates the split (>>output)", split["input"] > split["output"] * 50)
top = la.top_contributors(fixture, 3)
check("top contributor is the builder (heaviest input tag)", top[0][0] == "builder", str(top))
passes = la.build_passes(fixture, "builder")
check("build_passes picks up only the builder lines", len(passes) == 3, str(len(passes)))
stats = la.pass_stats(passes)
check("median of the build passes == 800K", stats["median"] == 800_000, str(stats["median"]))
check("the 11M pass trips the p95 target (OVER)", stats["p95_ok"] is False, str(stats))
heaviest = la.analyze(fixture)["heaviest"]
check("heaviest pass surfaced is AUTO-9 (the 11M build)", heaviest[0]["ticket"] == "AUTO-9", str(heaviest[0]))

# the report renders without raising and shows the headline finding + targets
txt = la.report(fixture)
check("report renders the input-share headline", "input share" in txt)
check("report renders the per-pass targets", "median" in txt and "p95" in txt)

# load_rows() reads the real ledger written above end-to-end (proves the file format round-trips)
disk_rows = la.load_rows(led)
check("load_rows reads every ledger line back", len(disk_rows) == len(lines) + 2, str(len(disk_rows)))
check("load_rows preserves the build-pass ticket tag",
      any(r.get("k") == "AUTO-9" and r.get("g") == "builder" for r in disk_rows))

print("\n========= EU-38 LEDGER ANALYSIS / TAGGING QA =========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
