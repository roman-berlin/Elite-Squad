"""Squad lanes (the build-delegation path and the domain-gap classifier were removed).

Phase-2 §2 flag-off collapse (2026-07-06): the Dev Team Lead's build-delegation path — the
squad-lead planner, the soldiers, and the ephemeral-specialist synthesis flow — was deleted; the
Builder always builds solo now (see ``builder.build``). What remains here is the standing SQUAD
lane map (the engineer roster the cockpit/roster still render). The read-only domain-gap
classifier (EU-69) was removed in the final Phase-2 collapse slice (EU-326) once its only caller,
the Engineering Manager's advisory roster preview (``adjutant.propose``, EU-85), was deleted. The
recon officers' delegation is a SEPARATE mechanism in ``recon.py`` that shares only the
``delegation_enabled`` flag.
"""
from __future__ import annotations

# Squad roles (from officers/engineer.md). key -> (label, focus line).
SQUAD: dict[str, tuple[str, str]] = {
    "vanguard-fe": ("Frontend Engineer", "Frontend — React/Vite, TypeScript, Tailwind, components, UI state."),
    "ordnance-be": ("Ordnance BE", "Backend — FastAPI/Python: routes, services, repositories, schemas."),
    "logistics-db": ("Logistics DB", "Data — Supabase/Postgres: SQL migrations, RLS policies, types."),
    "devops": ("DevOps", "CI / build / deploy / config — package scripts, env, Docker, Vercel/Turbo."),
    "generalist": ("Software Engineer", "General-purpose — anything outside the specialist lanes, or glue work."),
}
