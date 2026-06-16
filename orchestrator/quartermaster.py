"""Quartermaster — S-4, the Elite Unit's logistics / deploy-readiness officer.

Before the Commander promotes DEV -> MAIN, the Quartermaster certifies the unit can actually
ship: the build compiles, types pass, DB migrations apply cleanly, dependencies install from a
lockfile, required env/config is present (not placeholder), and the deploy config is sane. It
runs the cheap checks read-only and reports READY / NOT-READY — it certifies, it never changes
code. (Promotion to MAIN stays the Commander's call; the Quartermaster just tells you if it's safe.)

  general quartermaster automatixy
"""
from __future__ import annotations

from claude_agent_sdk import ClaudeAgentOptions

from .agent import run_agent
from .config import Config

QUARTERMASTER_SYSTEM = """\
You are the Quartermaster (S-4) — logistics and deploy-readiness officer of an elite autonomous
software unit, reporting to THE GENERAL. Methodical, conservative, supply-chain-minded. Your
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


async def inspect(cfg: Config, app_name: str) -> str:
    app = cfg.app(app_name)
    options = ClaudeAgentOptions(
        model=cfg.reviewer_model,
        system_prompt=QUARTERMASTER_SYSTEM,
        cwd=app.repo_path,
        # Unattended so it never stalls on the repo's Bash ask-gate. Read-only: Write/Edit
        # disallowed; the repo's deny rules (rm -rf, force-push) still hold.
        permission_mode="bypassPermissions",
        allowed_tools=["Read", "Grep", "Glob", "Bash"],
        disallowed_tools=["Write", "Edit", "NotebookEdit"],   # certify, never change code
        setting_sources=["project"],
        max_turns=30,
        effort="high",
    )
    run = await run_agent(_prompt(app), options, tag="quartermaster")
    return run.final or "(Quartermaster produced no report.)"
