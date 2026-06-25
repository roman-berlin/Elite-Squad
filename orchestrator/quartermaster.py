"""Release Manager — S-4, the Elite Unit's logistics / deploy-readiness officer.

Before the Commander promotes DEV -> MAIN, the Release Manager certifies the unit can actually
ship: the build compiles, types pass, DB migrations apply cleanly, dependencies install from a
lockfile, required env/config is present (not placeholder), and the deploy config is sane. It
runs the cheap checks read-only and reports READY / NOT-READY — it certifies, it never changes
code. (Promotion to MAIN stays the Commander's call; the Release Manager just tells you if it's safe.)

  general quartermaster automatixy
"""
from __future__ import annotations

from .config import Config
from .filing import TICKET_BLOCK_RULE

QUARTERMASTER_SYSTEM = """\
You are the Release Manager (S-4) — logistics and deploy-readiness officer of an elite autonomous
software unit, reporting to THE CTO. Methodical, conservative, supply-chain-minded. Your
charge: certify that the integration branch (DEV) can be promoted to MAIN and shipped without a
failed deploy or a broken environment. You are READ-ONLY — you certify, you never change code.

Check, and run the cheap verifications where present (memory-safe — cap workers, never a full
suite at default concurrency):
1. **Build** — does it compile? (`bun run build` / app build).
2. **Types** — typecheck clean? (`tsc --noEmit`).
3. **Migrations** — do DB migrations apply cleanly and in order; anything destructive or
   irreversible without a guard? (Supabase / SQL under `supabase/migrations`).
4. **Dependencies** — installs from a lockfile; no unpinned/missing critical deps; no obvious
   vulnerable pins.
5. **Environment & config** — required env vars declared (`.env.example`), nothing placeholder
   or missing that the app needs at boot; deploy config (Vercel/Docker/CI) coherent.
6. **Release hygiene** — CHANGELOG / status updated if the repo expects it.

Report a clear **READY / NOT-READY** verdict, then each blocker: what · where · impact on the
deploy · fix. Distinguish hard blockers (won't deploy / will break prod) from warnings. If it's
ready, say so plainly. Do not invent risk."""


def _prompt(app) -> str:
    return "\n".join([
        f"Deploy-readiness check for app '{app.name}' on its integration branch '{app.base_branch}'.",
        f"Repo: {app.repo_path}",
        "",
        "Inspect the branch's current state and the most recent changes. Run the cheap "
        "verifications that exist (build, typecheck, migration lint) memory-safe, and inspect "
        "migrations, lockfiles, .env.example, and deploy/CI config. Then issue your readiness "
        "report: READY/NOT-READY + blockers (what, where, deploy impact, fix). MAIN promotion is "
        "the Commander's call — you certify whether it's safe.",
    ])


async def inspect(cfg: Config, app_name: str, audit=None) -> str:
    app = cfg.app(app_name)
    from . import recon
    # Read-only deploy-readiness certification. With delegation armed, the Release Manager decides whether
    # to field a squad (a soldier per readiness area) on a big surface and synthesize, else solo (unchanged).
    return await recon.run_officer(
        officer="quartermaster", label="Release Manager",
        system=QUARTERMASTER_SYSTEM + TICKET_BLOCK_RULE, task=_prompt(app),
        cfg=cfg, cwd=app.repo_path, model=cfg.reviewer_model,
        soldier_tools=["Read", "Grep", "Glob", "Bash"], max_turns=30, effort="high",
        empty="(Release Manager produced no report.)", audit=audit)
