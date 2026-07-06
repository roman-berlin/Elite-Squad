"""Security Engineer — the Elite Unit's security officer.

A read-only gate: inspects the recent changes on DEV for the things that get a SaaS breached
— hardcoded secrets, injection, broken authn/authz, **tenant-isolation** violations (the
zero-trust core of a multi-tenant CRM), dangerous patterns, and known-vulnerable dependencies.
It flags with severity and the fix; it never edits code — the Dev Team Lead remediates.

  general provost automatixy        # security recon of the latest changes on DEV
"""
from __future__ import annotations

import re

from claude_agent_sdk import ClaudeAgentOptions

from . import guard, memory, models
from .agent import run_agent
from .config import Config
from .filing import TICKET_BLOCK_RULE

PROVOST_SYSTEM = """\
You are the Security Engineer — the Elite Unit's security officer, reporting to THE CTO.
Disciplined, precise, adversarial in the right way. You gate code before it reaches the
Commander. You are READ-ONLY: you flag, you never edit — the Dev Team Lead remediates.

Inspect the most recent changes for, in priority order:
1. **Secrets** — hardcoded API keys, tokens, passwords, private keys, connection strings.
2. **Tenant isolation** (zero-trust core) — cross-tenant data access, missing `business_id`
   scoping, a hardcoded client key instead of a per-request lookup, Supabase RLS gaps. Ground
   on the repo's `.claude/rules/tenant-isolation.md` and `prompts/rules.md`.
3. **AuthN/AuthZ** — missing or weak verification (JWT/JWKS), unprotected endpoints, privilege
   gaps, IDOR.
4. **Injection & unsafe execution** — SQL/command/XSS injection, `eval`, unsafe deserialization.
5. **Dependencies** — known-vulnerable packages (run a cheap audit if available).
6. **Exposure** — secrets/config leaking to the client, verbose errors, permissive CORS.

Rules: never print a real secret — mask it (`sk-…abcd`). Concentrate on what actually changed.
Output a clear **PASS / FAIL** verdict, then each finding as: severity (CRITICAL / HIGH /
MEDIUM / LOW) · where (file:line) · why it's exploitable · the fix. End with the single most
important control to add. If the changes are clean, say so plainly — do not invent risk."""


def _prompt(app) -> str:
    return "\n".join([
        f"Security recon of app '{app.name}' on its integration branch '{app.base_branch}'.",
        f"Repo: {app.repo_path}",
        "",
        "Inspect the most recent changes on the base branch (use git log/diff to see what landed "
        "recently) and concentrate your audit there. Read the repo's security rules under "
        "`.claude/rules/` for the project's zero-trust / tenant-isolation standards. Then issue "
        "your security report: PASS/FAIL + findings with severity, location, impact, and fix.",
    ])


async def inspect(cfg: Config, app_name: str, audit=None) -> str:
    app = cfg.app(app_name)
    from . import recon, models
    # Read-only security recon. With delegation armed, the Security Engineer decides for itself whether to field
    # a squad on a big surface (a soldier per area) and synthesize, else a single solo pass (unchanged).
    # EU-52: route through the ladder — high-effort security recon holds the reviewer ceiling normally
    # and only steps down when the day's budget is tight; auto_model off keeps the configured model.
    model, mreason = models.for_officer(cfg, effort="high")
    if getattr(cfg, "auto_model", False):
        print(f"  · provost model: {mreason}", flush=True)
    return await recon.run_officer(
        officer="provost", label="Security Engineer",
        system=PROVOST_SYSTEM + TICKET_BLOCK_RULE, task=_prompt(app),
        cfg=cfg, cwd=app.repo_path, model=model,
        soldier_tools=["Read", "Grep", "Glob", "Bash"], max_turns=30, effort="high",
        empty="(Security Engineer produced no report.)", audit=audit)


# Phase-2 §2 (2026-07-06): the per-diff security GATE (gate() + its countersignature
# helpers + PROVOST_GATE_SYSTEM) was DELETED. It was security_gate=false by default (never
# armed in prod) and §2 folds it into a deterministic secret/dep scan (gate.py) + one
# Reviewer checklist section. inspect() above STAYS — the scheduled security recon
# (patrol / `general provost` / recon) keeps its full value.
