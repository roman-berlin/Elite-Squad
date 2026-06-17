"""Provost Marshal — the Elite Unit's security officer.

A read-only gate: inspects the recent changes on DEV for the things that get a SaaS breached
— hardcoded secrets, injection, broken authn/authz, **tenant-isolation** violations (the
zero-trust core of a multi-tenant CRM), dangerous patterns, and known-vulnerable dependencies.
It flags with severity and the fix; it never edits code — the Field Engineer remediates.

  general provost automatixy        # security recon of the latest changes on DEV
"""
from __future__ import annotations

from claude_agent_sdk import ClaudeAgentOptions

from . import memory
from .agent import run_agent
from .config import Config
from .filing import TICKET_BLOCK_RULE

PROVOST_SYSTEM = """\
You are the Provost Marshal — the Elite Unit's security officer, reporting to THE GENERAL.
Disciplined, precise, adversarial in the right way. You gate code before it reaches the
Commander. You are READ-ONLY: you flag, you never edit — the Field Engineer remediates.

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


async def inspect(cfg: Config, app_name: str) -> str:
    app = cfg.app(app_name)
    options = ClaudeAgentOptions(
        model=cfg.reviewer_model,            # security judgment — use the strong model
        system_prompt=memory.preamble() + PROVOST_SYSTEM + TICKET_BLOCK_RULE,
        cwd=app.repo_path,
        # Unattended so it never stalls on the repo's Bash ask-gate. Still read-only: Write/Edit
        # are disallowed outright, and the repo's deny rules (rm -rf, force-push) still hold.
        permission_mode="bypassPermissions",
        allowed_tools=["Read", "Grep", "Glob", "Bash"],
        disallowed_tools=["Write", "Edit", "NotebookEdit"],   # flag, never edit
        setting_sources=["project"],
        max_turns=30,
        effort="high",
    )
    run = await run_agent(_prompt(app), options, tag="provost")
    return run.final or "(Provost produced no report.)"


PROVOST_GATE_SYSTEM = """\
You are the Provost Marshal security-gating a diff before it merges to the integration branch.
Same doctrine as a full recon but FAST and decisive: hunt secrets, tenant-isolation breaks,
authz/IDOR gaps, injection / unsafe execution, and obviously-vulnerable dependencies in THIS
diff. Read surrounding files only as needed to judge exploitability. You are read-only — flag,
never edit.

Be strict but fair: BLOCK only for a CRITICAL or HIGH issue a competent attacker could exploit.
Medium/low hardening does NOT block. End your response with EXACTLY one line, nothing after it:
  SECURITY GATE: BLOCK   (if any CRITICAL or HIGH finding)
  SECURITY GATE: PASS    (otherwise)
Above that line, briefly list any findings (severity · where · why · fix)."""


async def gate(cfg: Config, app, diff: str) -> tuple[bool, str]:
    """Security-gate a diff before merge. Returns (passed, report). BLOCK only on CRITICAL/HIGH."""
    options = ClaudeAgentOptions(
        model=cfg.reviewer_model,
        system_prompt=memory.preamble() + PROVOST_GATE_SYSTEM,
        cwd=app.workdir or app.repo_path,
        permission_mode="bypassPermissions",
        allowed_tools=["Read", "Grep", "Glob", "Bash"],
        disallowed_tools=["Write", "Edit", "NotebookEdit"],
        setting_sources=["project"],
        max_turns=18,
        effort="high",
    )
    prompt = "\n".join([
        f"Security-gate this diff before it merges to '{app.base_branch}':", "",
        "```diff", diff[:60000], "```", "", "Issue your gate verdict.",
    ])
    run = await run_agent(prompt, options, tag="provost-gate")
    report = (run.final or run.text or "(no report)").strip()
    blocked = "SECURITY GATE: BLOCK" in report.upper()
    return (not blocked, report)
