#!/usr/bin/env python3
"""EU-340 transcript-economics analyzer — see WITHIN a build pass, not just its ledger total.

`orchestrator/usage.py` writes one ledger line PER AGENT CALL (`usage_ledger.jsonl`), and
`orchestrator/agent.py:432-436` collapses ``cache_read_input_tokens + cache_creation_input_tokens +
input_tokens`` into that single ``i`` field before recording. That is enough to see a pass blew its
budget (see ``scripts/ledger_analysis.py``, EU-38) but NOT enough to see how — whether the growth was
one heavy turn or a slow climb, or whether the pass was cache-starved (low ``cache_read`` share, i.e.
paying full price every turn) or cache-healthy. ``orchestrator/transcript.py`` (EU-197) captures no
usage fields either, so that per-turn detail is only recoverable from Claude Code's OWN conversation
transcript on disk.

Claude Code writes one ``.jsonl`` per conversation under ``~/.claude/projects/<encoded-cwd>/``, where
``<encoded-cwd>`` is the run's working directory with ``/`` and ``.`` characters replaced by ``-``
(observed behaviour, e.g. ``/Users/x/Projects/.general-worktrees/Elite-Unit-s1`` encodes to
``-Users-x-Projects--general-worktrees-Elite-Unit-s1``). Each line is a JSON object; the ones that
matter here carry a ``message`` dict with a ``usage`` dict holding (up to) four token fields:
``input_tokens``, ``output_tokens``, ``cache_creation_input_tokens``, ``cache_read_input_tokens``.

**This is an UNDOCUMENTED Claude Code schema.** It is not a public API and can drift or disappear
across Claude Code versions with no notice. Every field access below defaults to 0 and every
unparseable/shape-mismatched line is skipped, never raised — this is a best-effort diagnostic tool,
not something anything else in the unit depends on.

Read-only, stdlib-only. Never imports the orchestrator, never writes, never touches the network.
Importable (unit-tested in tests/transcript_analysis_test.py) or runnable from the CLI:

    python3 scripts/transcript_analysis.py <usage_ledger.jsonl> <worktree_cwd> [--ticket K] [--pass P]
    python3 scripts/transcript_analysis.py --selftest      # verify the pure math, no files needed
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DEFAULT_LEDGER = "state/usage_ledger.jsonl"


def encode_cwd(path: str) -> str:
    """Reproduce Claude Code's `~/.claude/projects/<encoded>/` directory-name encoding: every `/`
    and `.` in the absolute cwd becomes `-`. Undocumented behaviour (see module docstring) —
    isolated here as one function so a future encoding change only needs one fix."""
    return re.sub(r"[/.]", "-", str(path))


def select_transcript(jsonl_paths, row: dict) -> Path | None:
    """Pick the conversation .jsonl (from `jsonl_paths`) whose mtime falls inside the ledger
    row's [t, t+d] window (t=timestamp, d=duration seconds); None if none match or the row has no
    timestamp. Degrades gracefully: a path that vanishes mid-stat is skipped, not raised on."""
    t = row.get("t")
    if t is None:
        return None
    try:
        t = float(t)
        d = float(row.get("d") or 0)
    except (TypeError, ValueError):
        return None
    lo, hi = t, t + d
    for p in jsonl_paths:
        p = Path(p)
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if lo <= mtime <= hi:
            return p
    return None


def parse_usage(lines) -> list[dict]:
    """Parse a transcript's raw lines into JSON row dicts, skipping any blank/unparseable line —
    same contract as ledger_analysis.load_rows. Does NOT require a `message.usage` shape yet;
    that filtering happens in turn_usages() so a schema-drifted line here is just skipped, never
    crashed on."""
    rows: list[dict] = []
    for line in lines:
        line = line.strip() if isinstance(line, str) else line
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def turn_usages(rows: list[dict]) -> list[dict]:
    """Extract the usage-bearing turns from parsed transcript rows: each returned record carries
    all four token fields (defaulted to 0 on a missing/malformed sub-field). A row with no
    `message.usage` dict at all is skipped — it's not a model turn (a tool_result line, a summary
    line, etc.), not a schema failure."""
    turns: list[dict] = []
    for row in rows:
        msg = row.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue

        def _n(key: str) -> int:
            try:
                return int(usage.get(key, 0) or 0)
            except (TypeError, ValueError):
                return 0

        turns.append({
            "input_tokens": _n("input_tokens"),
            "output_tokens": _n("output_tokens"),
            "cache_creation_input_tokens": _n("cache_creation_input_tokens"),
            "cache_read_input_tokens": _n("cache_read_input_tokens"),
        })
    return turns


def total_input(turn: dict) -> int:
    """The full input cost of one turn — input + cache_creation + cache_read — i.e. exactly the
    three fields agent.py:432-436 collapses into the ledger's single `i`."""
    return (int(turn.get("input_tokens", 0) or 0)
            + int(turn.get("cache_creation_input_tokens", 0) or 0)
            + int(turn.get("cache_read_input_tokens", 0) or 0))


def cache_hit_ratio(turn: dict) -> float:
    """cache_read / total_input for one turn; 0.0 (never a division error) when the turn paid no
    input at all."""
    ti = total_input(turn)
    if not ti:
        return 0.0
    return int(turn.get("cache_read_input_tokens", 0) or 0) / ti


def analyze_pass(turns: list[dict], ledger_row: dict | None = None) -> dict:
    """One structured rollup of a build pass's turns: turn count, the per-turn input-growth curve
    (in message order — the sequence IS the signal), the heaviest turn, the pass-wide cache-hit
    ratio, and — when a ledger row is supplied — the totals reconciliation that recovers what
    agent.py's collapse hides (sum of input+cache_creation+cache_read across every turn vs the
    ledger row's single `i`)."""
    curve = [total_input(t) for t in turns]
    heaviest_idx = max(range(len(curve)), key=lambda i: curve[i]) if curve else None
    total_read = sum(int(t.get("cache_read_input_tokens", 0) or 0) for t in turns)
    total_creation = sum(int(t.get("cache_creation_input_tokens", 0) or 0) for t in turns)
    total_in = sum(int(t.get("input_tokens", 0) or 0) for t in turns)
    total_out = sum(int(t.get("output_tokens", 0) or 0) for t in turns)
    recovered = total_in + total_creation + total_read

    out = {
        "turn_count": len(turns),
        "growth_curve": curve,
        "heaviest": ({"turn": heaviest_idx, "input_total": curve[heaviest_idx]}
                     if heaviest_idx is not None else None),
        "cache_hit_ratio": (total_read / recovered) if recovered else 0.0,
        "totals": {"input": total_in, "output": total_out,
                   "cache_creation": total_creation, "cache_read": total_read,
                   "recovered": recovered},
    }
    if ledger_row is not None:
        try:
            ledger_i = int(ledger_row.get("i", 0) or 0)
        except (TypeError, ValueError):
            ledger_i = 0
        out["reconciliation"] = {"ledger_i": ledger_i, "recovered": recovered,
                                  "delta": recovered - ledger_i,
                                  "match": recovered == ledger_i}
    return out


def _fmt(n: float) -> str:
    n = float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:.0f}"


def report(turns: list[dict], ledger_row: dict | None = None) -> str:
    """A human-readable report (what the CLI prints)."""
    a = analyze_pass(turns, ledger_row)
    out = ["EU-340 transcript analysis", "=" * 50,
           f"turns: {a['turn_count']}   cache-hit ratio: {a['cache_hit_ratio'] * 100:.1f}%",
           "", "Per-turn input growth:"]
    for i, v in enumerate(a["growth_curve"]):
        out.append(f"  turn {i:<4} {_fmt(v)}")
    if a["heaviest"] is not None:
        out.append(f"heaviest turn: {a['heaviest']['turn']} ({_fmt(a['heaviest']['input_total'])})")
    t = a["totals"]
    out += ["", f"totals: input {_fmt(t['input'])}  cache_creation {_fmt(t['cache_creation'])}  "
                f"cache_read {_fmt(t['cache_read'])}  recovered {_fmt(t['recovered'])}"]
    if "reconciliation" in a:
        r = a["reconciliation"]
        out.append(f"reconciled vs ledger 'i' {_fmt(r['ledger_i'])}: "
                    f"delta {_fmt(r['delta'])} ({'MATCH' if r['match'] else 'MISMATCH'})")
    return "\n".join(out)


def _selftest() -> int:
    """Verify the pure parsing/analysis math without any real transcript or ledger file."""
    fail = []

    def ck(name, cond, detail=""):
        if not cond:
            fail.append(f"{name} ({detail})" if detail else name)

    ck("encode_cwd replaces / and .",
       encode_cwd("/Users/x/Projects/.general-worktrees/Elite-Unit-s1")
       == "-Users-x-Projects--general-worktrees-Elite-Unit-s1")

    lines = [
        json.dumps({"type": "assistant", "message": {"usage": {
            "input_tokens": 1000, "output_tokens": 50,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}),
        "",
        "not json {{{",
        json.dumps({"type": "summary", "summary": "no message key here"}),
        json.dumps({"type": "assistant", "message": {"no_usage_key": True}}),
        json.dumps({"type": "assistant", "message": {"usage": {
            "input_tokens": 5000, "output_tokens": 20,
            "cache_creation_input_tokens": 5000, "cache_read_input_tokens": 90000}}}),
    ]
    rows = parse_usage(lines)
    ck("parse_usage skips blank/malformed lines", len(rows) == 4, str(len(rows)))
    turns = turn_usages(rows)
    ck("turn_usages extracts only usage-bearing turns", len(turns) == 2, str(len(turns)))
    ck("cache_hit_ratio computed 0.9",
       round(cache_hit_ratio(turns[1]), 4) == 0.9, str(cache_hit_ratio(turns[1])))
    ck("cache_hit_ratio 0.0 with no cached input (no crash)",
       cache_hit_ratio(turns[0]) == 0.0)

    growth_turns = [{"input_tokens": v, "output_tokens": 0,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
                     for v in (100_000, 300_000, 900_000)]
    a = analyze_pass(growth_turns, ledger_row={"i": 1_300_000})
    ck("analyze_pass turn count", a["turn_count"] == 3)
    ck("growth curve preserves order", a["growth_curve"] == [100_000, 300_000, 900_000])
    ck("heaviest turn identified", a["heaviest"]["turn"] == 2 and a["heaviest"]["input_total"] == 900_000)
    ck("reconciliation matches ledger i", a["reconciliation"]["match"] is True)

    mismatched = analyze_pass(growth_turns, ledger_row={"i": 1_000_000})
    ck("reconciliation reports delta on mismatch", mismatched["reconciliation"]["delta"] == 300_000)
    ck("reconciliation flags mismatch", mismatched["reconciliation"]["match"] is False)

    if fail:
        print("SELFTEST FAIL: " + ", ".join(fail))
        return 1
    print("SELFTEST OK — parsing/analysis math verified")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return _selftest()
    args = argv[1:]
    ticket, pass_no, ledger_path, cwd = None, None, None, None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--ticket" and i + 1 < len(args):
            ticket = args[i + 1]; i += 2
        elif a == "--pass" and i + 1 < len(args):
            pass_no = int(args[i + 1]); i += 2
        elif ledger_path is None:
            ledger_path = a; i += 1
        else:
            cwd = a; i += 1
    ledger_path = ledger_path or DEFAULT_LEDGER
    if not cwd or not Path(ledger_path).exists():
        print("usage: transcript_analysis.py <usage_ledger.jsonl> <worktree_cwd> "
              "[--ticket K] [--pass P]")
        return 1

    rows = parse_usage(Path(ledger_path).read_text(encoding="utf-8").splitlines())
    candidates = [r for r in rows if str(r.get("g", "")).startswith("builder")]
    if ticket:
        candidates = [r for r in candidates if r.get("k") == ticket]
    if pass_no is not None:
        candidates = [r for r in candidates if r.get("p") == pass_no]
    if not candidates:
        print("no matching ledger row for the given ticket/pass")
        return 1
    row = candidates[-1]

    proj_dir = Path.home() / ".claude" / "projects" / encode_cwd(cwd)
    jsonl_paths = sorted(proj_dir.glob("*.jsonl")) if proj_dir.exists() else []
    transcript = select_transcript(jsonl_paths, row)
    if transcript is None:
        print(f"no transcript found under {proj_dir} within the pass's time window")
        return 1

    turns = turn_usages(parse_usage(transcript.read_text(encoding="utf-8").splitlines()))
    print(report(turns, ledger_row=row))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
