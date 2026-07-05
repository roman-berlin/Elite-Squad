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
from .contracts import SecurityArtifact
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


PROVOST_GATE_SYSTEM = """\
You are the Security Engineer security-gating a diff before it merges to the integration branch.
Same doctrine as a full recon but FAST and decisive: hunt secrets, tenant-isolation breaks,
authz/IDOR gaps, injection / unsafe execution, and obviously-vulnerable dependencies in THIS
diff. Read surrounding files only as needed to judge exploitability. You are read-only — flag,
never edit.

Be strict but fair: BLOCK only for a CRITICAL or HIGH issue a competent attacker could exploit.
Medium/low hardening does NOT block.

Before the final verdict line you MUST include the three countersignature sections below, filled
in with your findings or "none" if the diff is clean.  Do NOT leave angle-bracket placeholders.

§1 SECRETS: <diff grep output confirming zero credentials, or masked list of any found>
§2 AUTHZ: <one line per new/changed route: guard function name + middleware position, or "none">
§3 INJECTION: <one line per risky call-site: parameterization mechanism used, or "none">

End your response with EXACTLY one line after the §3 section, nothing after it:
  SECURITY GATE: BLOCK   (if any CRITICAL or HIGH finding)
  SECURITY GATE: PASS    (otherwise)
Above the §1 section, briefly list any findings (severity · where · why · fix)."""


def _gate_passed(report: str) -> bool:
    """Fail-CLOSED verdict parse — the mirror of the Reviewer, never the inverse.

    A diff passes the security gate ONLY when the Security Engineer emits an explicit, unambiguous
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


def _parse_security_sections(report: str) -> tuple[str, str, str]:
    """Extract the mandatory §1/§2/§3 countersignature fields from the Security Engineer's report.

    Scans the report for ``§1 …``, ``§2 …``, ``§3 …`` markers and returns the
    (s1, s2, s3) text triple.  Content runs from the first ``:`` after the
    section marker up to (but not including) the next section marker or the
    ``SECURITY GATE:`` verdict line.

    Returns empty strings for any section that cannot be located — those empty
    strings cause ``SecurityArtifact.is_signed()`` to return False, ensuring
    the gate fails closed on a missing or malformed section.
    """
    sections: dict[str, str] = {}
    for m in re.finditer(
        r"§([123])(.*?)(?=§[123]|SECURITY GATE:|\Z)",
        report,
        re.IGNORECASE | re.DOTALL,
    ):
        num = m.group(1)
        raw = m.group(2)
        # Take the content after the first ":" (strips label like " SECRETS: ")
        colon_idx = raw.find(":")
        content = raw[colon_idx + 1:].strip() if colon_idx != -1 else raw.strip()
        if num not in sections:  # keep first match per section number
            sections[num] = content
    return sections.get("1", ""), sections.get("2", ""), sections.get("3", "")


def _publish_artifact(store, s1: str, s2: str, s3: str, *, signed: bool) -> None:
    """Construct a SecurityArtifact and publish it to *store* if one was provided.

    A no-op when *store* is None so the gate can be called without a store
    (e.g. from tests or standalone CLI use) without raising AttributeError.
    """
    if store is None:
        return
    artifact = SecurityArtifact(s1_secrets=s1, s2_authz=s2, s3_injection=s3, signed=signed)
    store.put(artifact)


async def gate(cfg: Config, app, diff: str, store=None) -> tuple[bool, str]:
    """Security-gate a diff before merge. Returns (passed, report).

    BLOCK on CRITICAL/HIGH findings. Fails CLOSED in the unsafe direction: only an explicit
    ``SECURITY GATE: PASS`` passes; absence / BLOCK / empty / parse-uncertainty all block, and
    ANY exception at the gate also blocks (the caller opens a PR rather than landing on DEV).

    After obtaining the Security Engineer's verdict this function parses the mandatory §1/§2/§3
    countersignature sections from the report, constructs a :class:`~contracts.SecurityArtifact`,
    and (when *store* is supplied) publishes it via ``store.put()``.  The artifact is marked
    ``signed=True`` only when the gate nominally passes AND all three fields are genuinely filled
    in — a PASS verdict with an empty/placeholder section still fails closed via
    ``SecurityArtifact.is_signed()``.  Callers should assert ``store.security.is_signed()`` after
    this function returns to enforce the countersignature gate end-to-end.
    """
    try:
        guard.warn_if_absent("provost-gate")   # EU-47: loud one-liner if the gate runs under bypass with no guard
        # EU-52: the merge-blocking security gate runs through the ladder at high effort — it normally
        # holds the reviewer ceiling (Opus) for a strong gate, conserving only when the budget is tight.
        from . import provider as _provider
        gate_model, gmreason = models.for_officer(cfg, effort="high")
        if getattr(cfg, "auto_model", False):
            print(f"  · provost-gate model: {gmreason}", flush=True)
        options = ClaudeAgentOptions(
            model=gate_model,
            system_prompt=memory.preamble() + PROVOST_GATE_SYSTEM,
            cwd=app.workdir or app.repo_path,
            permission_mode="bypassPermissions",
            allowed_tools=["Read", "Grep", "Glob", "Bash"],
            disallowed_tools=["Write", "Edit", "NotebookEdit"],
            # EU-47: the gate reads an attacker-influenceable diff with Bash allowed, so the hard denylist
            # must guard it too — deny-by-content (cat .env / exfil) is the boundary, not removing Bash.
            hooks=guard.hooks_config(),
            setting_sources=["project"],
            max_turns=18,
            effort="high",
        )
        prompt = "\n".join([
            f"Security-gate this diff before it merges to '{app.base_branch}':", "",
            "```diff", diff[:60000], "```", "", "Issue your gate verdict.",
        ])
        run = await run_agent(prompt, options, tag="provost-gate")
        # EU-123: show actual provider+model in the live feed
        if getattr(cfg, "auto_model", False):
            display = _provider.format_provider_model(run.provider, run.model_version)
            print(f"  · provost-gate · {display}", flush=True)
        # EU-96: provost returns a (bool, str) tuple, not a result object; write token burn
        # directly into the shared store so loop.py can read it from token_burn["provost"].
        if store is not None:
            # getattr-guarded: real RunResult carries these (default 0); test stubs / any
            # bare result object may not — a missing attr must never block the gate (EU-96).
            burn = getattr(run, "input_tokens", 0) + getattr(run, "output_tokens", 0)
            store.token_burn["provost"] = store.token_burn.get("provost", 0) + burn
            # 2026-07-05 telemetry audit: USD mirror of the token write. The (ok, report)
            # contract can't carry cost, so the loop reads the delta from store.stage_costs
            # and adds it to the ticket total — the EU-139 run's run_end missed $0.289 here.
            # isinstance-guarded so a legacy store stub without the field never blocks the gate.
            sc = getattr(store, "stage_costs", None)
            if isinstance(sc, dict):
                sc["provost"] = sc.get("provost", 0.0) + float(getattr(run, "cost_usd", 0.0) or 0.0)
        report = (run.final or run.text or "").strip()
        if not report:
            _publish_artifact(store, "", "", "", signed=False)
            return (False, "SECURITY GATE: BLOCK — Security Engineer returned an empty report; failing closed.")
        passed = _gate_passed(report)
        # Parse and publish the countersignature artifact regardless of verdict so the store
        # always reflects what the Security Engineer actually produced.  signed=True only when
        # the gate passed — the caller must still assert is_signed() to enforce completeness.
        s1, s2, s3 = _parse_security_sections(report)
        _publish_artifact(store, s1, s2, s3, signed=passed)
        return (passed, report)
    except Exception as exc:
        # An exception is never a PASS. Block, report the reason (the caller logs it to audit
        # and opens a PR for human review), and never error the ticket on the unsafe side.
        _publish_artifact(store, "", "", "", signed=False)
        return (False, f"SECURITY GATE: BLOCK — Security Engineer gate raised {type(exc).__name__}: {exc}; "
                       "failing closed (no verdict obtained).")
