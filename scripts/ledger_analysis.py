#!/usr/bin/env python3
"""EU-38 ledger-analysis snippet — find what is actually burning INPUT tokens.

~98.5% of the unit's token burn is INPUT, not output, and a handful of heavy Builder passes dominate
it (single passes ingested 4–11M input tokens; a normal build context should be ~100–500K). This reads
the `usage_ledger.jsonl` written by orchestrator/usage.py (one JSON object per agent call:
``{t,m,i,o,c,g}`` plus, for a build/soldier pass, ``k`` = ticket id and ``p`` = pass number) and reports:

  • the input-vs-output split (confirms input is the lever, not output),
  • the top context contributors by tag `g` (builder / reviewer / soldier / …),
  • the per-build-pass INPUT distribution — median / p95 / max — against the EU-38 targets
    (median < 1M, p95 < 3M), keyed by ticket id + pass number,
  • the single heaviest passes, so you can see exactly which ticket+pass blew the budget.

Pure stdlib, read-only — it never imports the orchestrator and never writes. Importable (the functions
below are unit-tested in tests/ledger_analysis_test.py) or runnable from the CLI:

    python3 scripts/ledger_analysis.py [path/to/usage_ledger.jsonl] [--tag builder] [--top N]
    python3 scripts/ledger_analysis.py --selftest      # verify the percentile / rollup math

Default ledger path matches usage.configure(): <audit dir>/usage_ledger.jsonl (here, ./memory/usage_ledger.jsonl).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# EU-38 acceptance targets for a build pass (input tokens), used to flag the distribution.
MEDIAN_TARGET = 1_000_000
P95_TARGET = 3_000_000
DEFAULT_LEDGER = "memory/usage_ledger.jsonl"


def load_rows(path: str | Path) -> list[dict]:
    """Read a usage_ledger.jsonl into a list of row dicts, skipping any unparseable line."""
    rows: list[dict] = []
    text = Path(path).read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def median(values) -> float:
    """Median of a numeric iterable; 0 for an empty input."""
    xs = sorted(values)
    n = len(xs)
    if n == 0:
        return 0
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2


def percentile(values, pct: float) -> float:
    """Nearest-rank percentile (pct in [0,100]); 0 for an empty input. p100 == max, p0 == min."""
    xs = sorted(values)
    if not xs:
        return 0
    if pct <= 0:
        return xs[0]
    rank = -(-int(pct) * len(xs) // 100)        # ceil(pct/100 * n), 1-based
    return xs[min(max(rank, 1), len(xs)) - 1]


def input_output_split(rows: list[dict]) -> dict:
    """Total input vs output tokens across all rows, and input's share (the 98.5% finding)."""
    ti = sum(int(r.get("i", 0)) for r in rows)
    to = sum(int(r.get("o", 0)) for r in rows)
    total = ti + to
    return {"input": ti, "output": to, "total": total,
            "input_pct": (ti / total) if total else 0.0}


def top_contributors(rows: list[dict], n: int = 3) -> list[tuple[str, int]]:
    """The top-n context contributors: tags (`g`) ranked by total INPUT tokens. AC: 'identify the
    top 3 context contributors'."""
    by: dict[str, int] = {}
    for r in rows:
        g = str(r.get("g", "") or "?")
        by[g] = by.get(g, 0) + int(r.get("i", 0))
    return sorted(by.items(), key=lambda kv: kv[1], reverse=True)[: max(0, n)]


def build_passes(rows: list[dict], tag: str = "builder") -> list[dict]:
    """Per-pass input tokens for rows whose tag starts with `tag` (build + its soldiers), each
    labelled with its ticket id + pass number when the line carries them."""
    out: list[dict] = []
    for r in rows:
        if str(r.get("g", "")).startswith(tag):
            out.append({"ticket": r.get("k", "?"), "pass": r.get("p"),
                        "input": int(r.get("i", 0)), "tag": r.get("g", ""), "model": r.get("m", "?")})
    return out


def pass_stats(passes: list[dict]) -> dict:
    """median / p95 / max of per-pass input tokens, with the EU-38 target pass/fail flags."""
    vals = [p["input"] for p in passes]
    med, p95 = median(vals), percentile(vals, 95)
    return {"count": len(vals), "median": med, "p95": p95, "max": max(vals) if vals else 0,
            "median_ok": med < MEDIAN_TARGET, "p95_ok": p95 < P95_TARGET}


def analyze(rows: list[dict], tag: str = "builder", top: int = 3) -> dict:
    """One structured rollup of the whole ledger for the EU-38 acceptance check."""
    passes = build_passes(rows, tag)
    return {"split": input_output_split(rows),
            "top_contributors": top_contributors(rows, top),
            "passes": pass_stats(passes),
            "heaviest": sorted(passes, key=lambda p: p["input"], reverse=True)[: max(0, top)]}


def _fmt(n: float) -> str:
    n = float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:.0f}"


def report(rows: list[dict], tag: str = "builder", top: int = 3) -> str:
    """A human-readable report (what the CLI prints)."""
    a = analyze(rows, tag, top)
    s, ps = a["split"], a["passes"]
    out = ["EU-38 ledger analysis", "=" * 50,
           f"calls: {len(rows)}   input: {_fmt(s['input'])}   output: {_fmt(s['output'])}"
           f"   input share: {s['input_pct'] * 100:.1f}%",
           "",
           f"Top {top} context contributors (by input tokens):"]
    for i, (g, toks) in enumerate(a["top_contributors"], 1):
        out.append(f"  {i}. {g or '?':<22} {_fmt(toks)}")
    out += ["",
            f"Build passes (tag '{tag}'): {ps['count']}",
            f"  median {_fmt(ps['median'])}  (target <{_fmt(MEDIAN_TARGET)}: "
            f"{'OK' if ps['median_ok'] else 'OVER'})",
            f"  p95    {_fmt(ps['p95'])}  (target <{_fmt(P95_TARGET)}: "
            f"{'OK' if ps['p95_ok'] else 'OVER'})",
            f"  max    {_fmt(ps['max'])}",
            "",
            "Heaviest passes:"]
    for p in a["heaviest"]:
        pno = "?" if p["pass"] is None else p["pass"]
        out.append(f"  {str(p['ticket']):<14} pass {pno!s:<3} {_fmt(p['input']):<8} [{p['model']}]")
    return "\n".join(out)


def _selftest() -> int:
    """Verify the percentile / rollup math without any ledger file or model call."""
    fail = []

    def ck(name, cond):
        if not cond:
            fail.append(name)

    ck("median odd", median([3, 1, 2]) == 2)
    ck("median even", median([1, 2, 3, 4]) == 2.5)
    ck("median empty", median([]) == 0)
    ck("p95 of 1..20 == 19", percentile(list(range(1, 21)), 95) == 19)
    ck("p100 == max", percentile([5, 1, 9], 100) == 9)
    ck("p0 == min", percentile([5, 1, 9], 0) == 1)
    ck("percentile empty", percentile([], 95) == 0)

    rows = [
        {"i": 11_000_000, "o": 5000, "g": "builder", "k": "AUTO-9", "p": 1},
        {"i": 400_000, "o": 3000, "g": "builder", "k": "EU-38", "p": 1},
        {"i": 800_000, "o": 2000, "g": "builder", "k": "EU-38", "p": 2},
        {"i": 50_000, "o": 4000, "g": "reviewer", "k": "EU-38", "p": 2},
        {"i": 20_000, "o": 1000, "g": "the-general"},
    ]
    a = analyze(rows)
    ck("input share computed", round(a["split"]["input_pct"], 4) == round(12_270_000 / 12_285_000, 4))
    ck("top contributor is builder", a["top_contributors"][0][0] == "builder")
    ck("3 build passes counted", a["passes"]["count"] == 3)
    ck("median of [11M,400K,800K] == 800K", a["passes"]["median"] == 800_000)
    ck("heaviest pass is the 11M AUTO-9 pass", a["heaviest"][0]["ticket"] == "AUTO-9")
    ck("11M pass flagged over the p95 target", not a["passes"]["p95_ok"])

    if fail:
        print("SELFTEST FAIL: " + ", ".join(fail))
        return 1
    print(f"SELFTEST OK — {0} failures, percentile/rollup math verified")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return _selftest()
    tag, top, path = "builder", 3, None
    i = 0
    args = argv[1:]
    while i < len(args):
        a = args[i]
        if a == "--tag" and i + 1 < len(args):
            tag = args[i + 1]; i += 2
        elif a == "--top" and i + 1 < len(args):
            top = int(args[i + 1]); i += 2
        else:
            path = a; i += 1
    path = path or DEFAULT_LEDGER
    if not Path(path).exists():
        print(f"no ledger at {path} (pass a path, or run from the repo root)")
        return 1
    print(report(load_rows(path), tag=tag, top=top))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
