"""EU-340 QA — the per-turn transcript economics analyzer recovers what the ledger's collapsed
`i` field hides: per-turn input growth and cache-hit ratio (scripts/transcript_analysis.py),
modeled on tests/ledger_analysis_test.py. Pure stdlib, no network, no SDK — drives the analyzer's
pure functions against inline fixtures (no dependency on a real ~/.claude/projects transcript)."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")          # the analyzer lives under scripts/ (same convention as EU-38)

results = []


def check(n, c, d=""):
    results.append((n, bool(c), d))


import transcript_analysis as ta

# ============================================================================
# AC1 — parse_usage/turn_usages: 3 usage-bearing messages + non-usage lines -> exactly 3 turns,
# each carrying all four token fields; a line missing message.usage is skipped, not crashed on.
# ============================================================================
FIXTURE_LINES = [
    json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": 100_000, "output_tokens": 500,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}),
    "",                                                              # blank line
    "not json at all {{{",                                          # malformed JSON
    json.dumps({"type": "summary", "summary": "no message key"}),   # no message key
    json.dumps({"type": "assistant", "message": {"role": "assistant"}}),  # message, no usage key
    json.dumps({"type": "user", "message": {"usage": "not-a-dict"}}),     # usage wrong shape
    json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": 300_000, "output_tokens": 800,
        "cache_creation_input_tokens": 10_000, "cache_read_input_tokens": 40_000}}}),
    json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": 10_000, "output_tokens": 200,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 90_000}}}),
]

rows = ta.parse_usage(FIXTURE_LINES)
turns = ta.turn_usages(rows)
check("turn_usages returns exactly 3 usage-bearing turns", len(turns) == 3, str(len(turns)))
FOUR_FIELDS = {"input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"}
check("every turn carries all four token fields",
      all(FOUR_FIELDS <= set(t.keys()) for t in turns), str(turns))
check("no crash / silent skip on lines missing message.usage (blank/malformed lines dropped at parse)",
      len(rows) == 6, str(len(rows)))     # 6 parseable JSON objects out of 8 raw lines
check("of those 6 parsed rows, the 3 without a usable message.usage are skipped by turn_usages",
      len(rows) - len(turns) == 3, str(len(rows) - len(turns)))

# ============================================================================
# AC2 — cache-hit ratio: cache_read / (input+cache_creation+cache_read); no div-by-zero.
# ============================================================================
heavy_cache = {"input_tokens": 5_000, "output_tokens": 10,
               "cache_creation_input_tokens": 5_000, "cache_read_input_tokens": 90_000}
check("cache-hit ratio computes to 0.9", round(ta.cache_hit_ratio(heavy_cache), 4) == 0.9,
      str(ta.cache_hit_ratio(heavy_cache)))

no_cache = {"input_tokens": 1_000, "output_tokens": 10,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
check("cache-hit ratio is 0.0 with no cached input (no crash)", ta.cache_hit_ratio(no_cache) == 0.0)

zero_turn = {"input_tokens": 0, "output_tokens": 0,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
check("cache-hit ratio on an all-zero turn is 0.0 (no ZeroDivisionError)",
      ta.cache_hit_ratio(zero_turn) == 0.0)

# ============================================================================
# AC3 — analyze_pass: turn count + per-turn input-growth curve in message order; heaviest turn.
# ============================================================================
growth_turns = [
    {"input_tokens": v, "output_tokens": 0,
     "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    for v in (100_000, 300_000, 900_000)
]
a = ta.analyze_pass(growth_turns)
check("analyze_pass turn count", a["turn_count"] == 3, str(a["turn_count"]))
check("growth curve preserves ascending message order",
      a["growth_curve"] == [100_000, 300_000, 900_000], str(a["growth_curve"]))
check("heaviest turn identified as the 900000 turn",
      a["heaviest"]["turn"] == 2 and a["heaviest"]["input_total"] == 900_000, str(a["heaviest"]))

# ============================================================================
# AC4 — totals reconciliation: sum(cache_read+cache_creation+input) across turns vs ledger row's
# single `i` (what agent.py:432-436 collapses); reports a match/delta either way.
# ============================================================================
matched = ta.analyze_pass(growth_turns, ledger_row={"i": 1_300_000})
check("reconciliation recovers the exact sum agent.py collapsed into `i`",
      matched["reconciliation"]["recovered"] == 1_300_000, str(matched["reconciliation"]))
check("reconciliation reports a match when recovered == ledger i",
      matched["reconciliation"]["match"] is True)

mismatched = ta.analyze_pass(growth_turns, ledger_row={"i": 1_000_000})
check("reconciliation reports the delta on a mismatch",
      mismatched["reconciliation"]["delta"] == 300_000, str(mismatched["reconciliation"]))
check("reconciliation flags the mismatch", mismatched["reconciliation"]["match"] is False)

# report() renders without raising and surfaces the headline figures
txt = ta.report(growth_turns, ledger_row={"i": 1_300_000})
check("report renders the cache-hit headline", "cache-hit ratio" in txt)
check("report renders the reconciliation line", "MATCH" in txt)

# ============================================================================
# AC5 — encode_cwd reproduces Claude Code's `/`->`-` project-dir encoding; select_transcript
# picks the .jsonl whose mtime lies within a ledger row's [t, t+d] window and ignores one outside.
# ============================================================================
check("encode_cwd encodes a plain path",
      ta.encode_cwd("/Users/x/Projects/General") == "-Users-x-Projects-General",
      ta.encode_cwd("/Users/x/Projects/General"))
check("encode_cwd also encodes dots (observed Claude Code behaviour)",
      ta.encode_cwd("/Users/x/Projects/.wt/Foo") == "-Users-x-Projects--wt-Foo",
      ta.encode_cwd("/Users/x/Projects/.wt/Foo"))

tmp = Path(tempfile.mkdtemp())
in_window = tmp / "in-window.jsonl"
out_window = tmp / "out-window.jsonl"
in_window.write_text("{}\n", encoding="utf-8")
out_window.write_text("{}\n", encoding="utf-8")

now = time.time()
row = {"t": now, "d": 60}
os.utime(in_window, (now + 10, now + 10))          # inside [t, t+d]
os.utime(out_window, (now + 1000, now + 1000))     # well outside the window

picked = ta.select_transcript([in_window, out_window], row)
check("select_transcript picks the .jsonl whose mtime is inside the window",
      picked == in_window, str(picked))
check("select_transcript ignores the .jsonl outside the window (not the out-of-window file)",
      picked != out_window)

no_row_match = ta.select_transcript([out_window], row)
check("select_transcript returns None when nothing falls in the window", no_row_match is None)

# ============================================================================
# AC6 — the module's own self-test (percentile-free here, but same pattern as ledger_analysis) and
# CLI entrypoint both stay green; run_all.py picks this harness up automatically (tests/*_test.py).
# ============================================================================
check("module self-test passes (parsing/analysis math)", ta._selftest() == 0)
check("CLI --selftest returns 0", ta.main(["transcript_analysis.py", "--selftest"]) == 0)

print("\n========= EU-340 TRANSCRIPT ANALYSIS QA =========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results) - passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
