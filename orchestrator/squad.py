"""Squad lanes + domain-gap classification (the build-delegation path was removed).

Phase-2 §2 flag-off collapse (2026-07-06): the Dev Team Lead's build-delegation path — the
squad-lead planner, the soldiers, and the ephemeral-specialist synthesis flow — was deleted; the
Builder always builds solo now (see ``builder.build``). What remains here is the standing SQUAD
lane map (the engineer roster the cockpit/roster still render) and the read-only domain-gap
classifier (EU-69) still used by the Engineering Manager's advisory roster preview
(``adjutant.propose``). The recon officers' delegation is a SEPARATE mechanism in ``recon.py``
that shares only the ``delegation_enabled`` flag.
"""
from __future__ import annotations

import json
import re

from .agent import run_agent

# Squad roles (from officers/engineer.md). key -> (label, focus line).
SQUAD: dict[str, tuple[str, str]] = {
    "vanguard-fe": ("Frontend Engineer", "Frontend — React/Vite, TypeScript, Tailwind, components, UI state."),
    "ordnance-be": ("Ordnance BE", "Backend — FastAPI/Python: routes, services, repositories, schemas."),
    "logistics-db": ("Logistics DB", "Data — Supabase/Postgres: SQL migrations, RLS policies, types."),
    "devops": ("DevOps", "CI / build / deploy / config — package scripts, env, Docker, Vercel/Turbo."),
    "generalist": ("Software Engineer", "General-purpose — anything outside the specialist lanes, or glue work."),
}


_GAP_SYSTEM = """\
You are a domain classifier. Given a software ticket and a list of engineering squad lanes, \
decide whether the ticket's primary technical domain is covered by one of those lanes.

Reply with ONLY valid JSON — no prose, no fences.
If covered by a lane: {"covered": true, "domain": "<lane_key>"}
If outside all lanes: {"covered": false, "domain": "<short domain label, e.g. mql5, rust, ml>"}"""


async def detect_domain_gap(ticket_text: str, squad: dict,
                            ticket_id: str | None = None) -> tuple[bool, str | None, dict]:
    """Classify the ticket's primary domain against the known squad lanes using a Haiku call.

    Returns ``(True, '<domain>', burn)`` when no lane covers the ticket (e.g. ``(True, 'mql5', …)``),
    or ``(False, None, burn)`` when a lane already handles it. ``burn`` is the classifier call's
    own spend — ``{"cost_usd", "num_turns", "input_tokens", "output_tokens"}`` — or ``{}`` when the
    call never ran. The 2026-07-05 EU-139-run telemetry audit found this cost was dropped from
    every accounting surface, so callers must fold ``burn`` into their own totals.

    Fail-safe: any error (network, parse, SDK) returns ``(False, None, burn-so-far)`` so it never
    blocks the caller's flow.

    Args:
        ticket_text: Combined summary + description + acceptance criteria of the ticket.
        squad: The squad lane mapping — same shape as the module-level ``SQUAD`` dict.
        ticket_id: Ticket key when running inside a ticket flow — stamps the usage-ledger line
            (``k``) so per-ticket burn slicing counts this call. None outside a ticket context.
    """
    from . import models as _models
    lane_desc = "\n".join(f"  {key}: {val[1]}" for key, val in squad.items())
    prompt = "\n".join([
        "SQUAD LANES:",
        lane_desc,
        "",
        "TICKET:",
        (ticket_text or "").strip()[:2000],
        "",
        'Classify now — JSON only, e.g. {"covered": false, "domain": "mql5"}',
    ])
    burn: dict = {}
    try:
        from claude_agent_sdk import ClaudeAgentOptions as _Opts
        options = _Opts(
            model=_models.HAIKU,
            system_prompt=_GAP_SYSTEM,
            permission_mode="bypassPermissions",
            allowed_tools=[],
            setting_sources=[],
            max_turns=3,
            effort="low",
        )
        run = await run_agent(prompt, options, tag="gap-detect", ticket_id=ticket_id)
        burn = {"cost_usd": float(getattr(run, "cost_usd", 0.0) or 0.0),
                "num_turns": int(getattr(run, "num_turns", 0) or 0),
                "input_tokens": int(getattr(run, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(run, "output_tokens", 0) or 0)}
        text = (run.final or run.text or "").strip()
        # Tolerate prose wrapping; pull the first {...} block.
        m = re.search(r"\{[^}]+\}", text)
        if not m:
            return False, None, burn
        data = json.loads(m.group(0))
        covered = bool(data.get("covered", True))
        domain = str(data.get("domain", "")).strip().lower() or None
        if covered:
            return False, None, burn
        return True, domain, burn
    except Exception:  # noqa: BLE001 — fail-safe: gap detection must never break a run
        return False, None, burn
