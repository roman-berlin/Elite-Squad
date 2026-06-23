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


async def inspect(cfg: Config, app_name: str, audit=None) -> str:
    app = cfg.app(app_name)
    from . import recon
    # Read-only security recon. With delegation armed, the Provost decides for itself whether to field
    # a squad on a big surface (a soldier per area) and synthesize, else a single solo pass (unchanged).
    return await recon.run_officer(
        officer="provost", label="Security Engineer",
        system=PROVOST_SYSTEM + TICKET_BLOCK_RULE, task=_prompt(app),
        cfg=cfg, cwd=app.repo_path, model=cfg.reviewer_model,
        soldier_tools=["Read", "Grep", "Glob", "Bash"], max_turns=30, effort="high",
        empty="(Security Engineer produced no report.)", audit=audit)


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


def _gate_passed(report: str) -> bool:
    """Fail-CLOSED verdict parse — the mirror of the Reviewer, never the inverse.

    A diff passes the security gate ONLY when the Provost emits an explicit, unambiguous
    `SECURITY GATE: PASS` and does NOT also emit `SECURITY GATE: BLOCK`. Everything else —
    a missing marker, an empty/truncated/garbled reply, or a BLOCK verdict — fails closed
    (returns False) so an unsafe diff is never waved through on silence.
    """
    if not report:
        return False
    up = report.upper()
    has_pass = "SECURITY GATE: PASS" in up
    has_block = "SECURITY GATE: BLOCK" in up
    # Pass requires the explicit PASS marker AND the absence of any BLOCK marker. If both
    # appear (a contradictory reply) we treat it as parse-uncertainty and block.
    return has_pass and not has_block


async def gate(cfg: Config, app, diff: str) -> tuple[bool, str]:
    """Security-gate a diff before merge. Returns (passed, report).

    BLOCK on CRITICAL/HIGH findings. Fails CLOSED in the unsafe direction: only an explicit
    `SECURITY GATE: PASS` passes; absence / BLOCK / empty / parse-uncertainty all block, and
    ANY exception at the gate also blocks (the caller opens a PR rather than landing on DEV).
    """
    try:
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
        report = (run.final or run.text or "").strip()
        if not report:
            return (False, "SECURITY GATE: BLOCK — Security Engineer returned an empty report; failing closed.")
        return (_gate_passed(report), report)
    except Exception as exc:
        # An exception is never a PASS. Block, report the reason (the caller logs it to audit
        # and opens a PR for human review), and never error the ticket on the unsafe side.
        return (False, f"SECURITY GATE: BLOCK — Security Engineer gate raised {type(exc).__name__}: {exc}; "
                       "failing closed (no verdict obtained).")
