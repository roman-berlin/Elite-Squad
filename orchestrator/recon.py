"""Recon delegation — any READ-ONLY recon officer (Scout, Provost, Quartermaster) can field a SQUAD
when the surface is big enough to warrant it, then synthesize one report. The read-only sibling of
the Field Engineer's squad.py, with the same discipline (Roman's reflexes):

- **Solo is the default.** With `delegation_enabled` off, this is byte-for-byte the old single-agent
  run — no extra cost, no behaviour change.
- **The officer decides for itself.** When armed, a cheap read-only PLANNING pass lets the officer
  return `SOLO` for a small / single-focus surface, or a set of non-overlapping slices for a big one.
  Fewer than 2 slices ⇒ solo. So an officer recruits soldiers only when the task actually needs them.
- **Fail-safe.** Any planning or soldier hiccup falls back to (or degrades toward) the solo report —
  delegation never breaks a patrol.
- **Soldiers are read-only and sequential**, one slice each. The lead synthesizes the final report in
  the officer's OWN format — the officer's system prompt carries the verdict format through planning,
  the soldiers, and the synthesis, so the output is identical in shape to a solo run.

One master switch (`delegation_enabled`) arms both the Field Engineer's squad and these recon squads.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from claude_agent_sdk import ClaudeAgentOptions

from . import memory
from .agent import run_agent
from .config import Config


@dataclass
class Slice:
    area: str
    detail: str


def parse_slices(text: str | None, cap: int = 4) -> list[Slice]:
    """Parse a planner reply. `SOLO` (or no JSON array) → [] (= do it solo). Otherwise a JSON array of
    {area, detail}. Pure + deterministic, so it's unit-testable without an agent."""
    if not text or text.strip().upper().startswith("SOLO"):
        return []
    m = re.search(r"\[\s*\{.*}\s*\]", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out: list[Slice] = []
    for item in (data if isinstance(data, list) else []):
        if not isinstance(item, dict):
            continue
        detail = str(item.get("detail", "")).strip()
        if not detail:
            continue
        area = str(item.get("area", "")).strip()[:80] or detail[:50]
        out.append(Slice(area=area, detail=detail))
    return out[: max(1, cap)]


_PLAN = """\

--- SQUAD PLANNING (read-only) ---
You may recruit SOLDIERS — read-only sub-inspectors — to cover a large surface faster, each owning ONE
non-overlapping slice. Decide for yourself: if this inspection is small or single-focus, do it
yourself — reply with EXACTLY `SOLO`. Otherwise reply with ONLY a JSON array (2-{cap} items), no prose:
[{{"area":"<short focus>","detail":"<exactly what this soldier inspects + where>"}}]
The slices must not overlap and together must cover the whole surface."""

_SOLDIER = """\

--- YOU ARE A SOLDIER ({i}/{n}) ---
Inspect ONLY your assigned slice and report its findings in your normal format — do NOT inspect other
slices and do NOT write the final verdict (the {label} synthesizes that). Read only what you need.
YOUR SLICE — {area}: {detail}"""

_SYNTH = """\

--- SYNTHESIS ---
Your soldiers inspected the surface in slices; their findings are below. Produce your SINGLE final
report now, in your standard format (verdict + findings), de-duplicating and reconciling overlaps.
Synthesize from their reports — Read a file only to resolve a conflict, do not re-inspect everything.

SOLDIER FINDINGS:
{findings}"""


def _opts(system: str, cwd: str, model: str, tools: list[str], turns: int, effort: str,
          *, planning: bool = False) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=model,
        system_prompt=memory.preamble() + system,
        cwd=cwd,
        permission_mode="bypassPermissions",
        allowed_tools=(["Read", "Grep", "Glob"] if planning else tools),
        disallowed_tools=["Write", "Edit", "NotebookEdit"],   # recon is read-only — flag, never edit
        setting_sources=["project"],
        max_turns=turns,
        effort=effort,
    )


async def _solo(system, task, cwd, model, tools, turns, effort, empty, tag):
    run = await run_agent(task, _opts(system, cwd, model, tools, turns, effort), tag=tag)
    return run.final or run.text or empty


async def run_officer(*, officer: str, label: str, system: str, task: str, cfg: Config, cwd: str,
                      model: str, soldier_tools: list[str], max_turns: int, effort: str,
                      empty: str = "(no report)", audit=None) -> str:
    """Run a recon officer. Solo by default; when delegation is armed the officer may field a squad.
    Always returns the report string (same contract as a solo run)."""
    if not getattr(cfg, "delegation_enabled", False):
        return await _solo(system, task, cwd, model, soldier_tools, max_turns, effort, empty, officer)

    cap = max(2, int(getattr(cfg, "delegation_max_soldiers", 4)))
    # 1) PLAN — the officer decides SOLO vs a split (read-only, cheap model/effort).
    try:
        plan = await run_agent(
            task + "\n" + _PLAN.format(cap=cap),
            _opts(system, cwd, model, soldier_tools, 12, "medium", planning=True),
            tag=f"{officer}-lead")
        slices = parse_slices(plan.final or plan.text, cap)
    except Exception:  # noqa: BLE001 - planning hiccup => solo
        slices = []
    if len(slices) < 2:
        return await _solo(system, task, cwd, model, soldier_tools, max_turns, effort, empty, officer)

    print(f"  {officer} · squad of {len(slices)}: " + ", ".join(s.area for s in slices), flush=True)
    if audit is not None:
        audit.record("recon_delegation", officer=officer, slices=len(slices),
                     areas=[s.area for s in slices])

    # 2) SOLDIERS — read-only, sequential, one slice each.
    findings: list[str] = []
    for i, sl in enumerate(slices, 1):
        sys_i = system + _SOLDIER.format(i=i, n=len(slices), label=label, area=sl.area, detail=sl.detail)
        try:
            r = await run_agent(task, _opts(sys_i, cwd, model, soldier_tools, max_turns, effort),
                                tag=f"soldier·{officer}{i}")
            findings.append(f"[Soldier {i} — {sl.area}]\n{(r.final or r.text or '(no findings)').strip()}")
            if audit is not None:
                audit.record("soldier_recon", officer=officer, area=sl.area, ok=not r.is_error)
        except Exception as e:  # noqa: BLE001 - a soldier failing must not abort the squad
            findings.append(f"[Soldier {i} — {sl.area}] inspection failed: {e}")

    # 3) SYNTHESIS — the lead merges into one report, in the officer's own format.
    try:
        syn = await run_agent(
            task + _SYNTH.format(findings="\n\n".join(findings)),
            _opts(system, cwd, model, soldier_tools, max_turns, effort),
            tag=f"{officer}-synth")
        return syn.final or syn.text or empty
    except Exception:  # noqa: BLE001 - fail-safe: hand back raw findings rather than nothing
        return f"{label} squad findings (synthesis unavailable):\n\n" + "\n\n".join(findings)
