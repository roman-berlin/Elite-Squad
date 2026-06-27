"""HR — ephemeral, task-scoped specialist synthesis (EU-69).

The web-stack officer corps (Vanguard / Security Engineer / …) is great when the ticket *is* the web
stack. But a ticket can land in ANY domain — an MQL5 expert advisor, a Rust CLI, a Solidity contract, a
data-pipeline DAG — where the standing corps has no specialist. Rather than hand-author a permanent
officer file for every domain the unit ever touches, HR **synthesizes the specialists the task needs, on
the spot**, by asking Claude (Sonnet) to write N charters in the `officers/_TEMPLATE.md` shape.

These charters are **EPHEMERAL**: they live in memory for the duration of one task and are **never**
written to `officers/`. The standing corps is curated doctrine; these are throwaway, task-scoped hires
the Dev Team Lead can dispatch to and then discard. (If a synthesized lane proves recurrent, the
Engineering Manager can later promote it to a committed officer file — see the AUTO-PROMOTE FIXME below.)

Each charter is a dict matching the template's Identity / Knowledge / Skills (+ Constraints) shape, plus
two EU-69 fields:
  • ``lane_key``    — a stable slug for the lane, e.g. ``'mql5-algo'`` (used to key/dedupe the hire).
  • ``domain_gate`` — a self-authored verification step the specialist runs to prove its own output,
                      e.g. ``'mql5 compile + Strategy Tester run'`` (a shell command OR a description).

In NON-automode the proposed charters are printed and the Commander must approve them ('y') before they
are used; in automode the unit proceeds autonomously (the Commander reviews after, as everywhere).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from . import guard, memory, models
from .agent import run_agent
from .config import Config


def _tty_input(prompt: str) -> str:
    """Default approver for the human-in-the-loop gates below — interactive ONLY when safe.

    Prompt the Commander on stdin **only when a real terminal is attached**. The unit runs
    UNATTENDED (the server / autopilot / CI — no TTY, and ``auto_mode`` is False by default), so there
    is no one at a keyboard to answer; in that case we raise ``EOFError`` and let the caller take its
    fail-safe DECLINE path instead of blocking forever on a dead stdin.

    The bare builtin ``input()`` must NEVER be the default approver here: a ``synthesize_specialists`` /
    ``check_promote`` call reached from the autonomous build loop with ``approver=input`` is exactly the
    EU-69 iteration-1 hang that wedged ``tests/run_all.py`` for the full 1800 s timeout (the build loop
    can't catch a blocking read — it is not an exception). Interactive approval is still fully
    available: inject an explicit ``approver`` (a CLI / cockpit hook, or a test stub)."""
    if sys.stdin is None or not sys.stdin.isatty():
        raise EOFError("no interactive terminal — declining (unattended)")
    return input(prompt)

# --------------------------------------------------------------------------- #
# FIXME (EU-69 design decision — AUTO-PROMOTE THRESHOLD): an ephemeral charter that the unit
# synthesizes over and over for the same `lane_key` is really a missing PERMANENT officer. The intended
# contract is: once HR has synthesized the same lane_key >= AUTO_PROMOTE_THRESHOLD distinct times, the
# Engineering Manager (adjutant.py) should PROPOSE promoting it to a committed `officers/<lane_key>.md`
# (propose-only, Commander-approved — never auto-write, same discipline as every other doctrine change).
# The counter/promotion pipeline is a SEPARATE slice; this module only flags the intent so the threshold
# has a single home. Until then every hire stays ephemeral. Default chosen conservatively (3) to mirror
# `Config.postmortem_after` — three repeats is "a pattern, not a fluke".
AUTO_PROMOTE_THRESHOLD = 3

# FIXME (EU-69 design decision — GATE-SUPPLY CONTRACT): `domain_gate` is the specialist's self-authored
# proof-of-correctness for its OWN domain (the web corps has the test/lint/typecheck gate; an MQL5 lane
# does not). The contract downstream is:
#   1. The specialist AUTHORS the gate here (a shell command when one exists, else a prose description of
#      the manual check) — HR never invents or executes it.
#   2. The Dev Team Lead / gate runner SUPPLIES the gate at dispatch time: a runnable command is executed
#      in the worktree (subject to the SAME guard.hooks_config denylist as every other Bash call — an
#      ephemeral, model-authored command is UNTRUSTED and must never bypass the zero-trust guard); a
#      prose-only gate is surfaced to the Commander as a manual QA step, NOT silently skipped.
#   3. A charter with no usable gate is still returned (so the lane isn't lost) but is flagged so the
#      caller can decide whether to require a manual gate. Wiring the executor is a separate slice; this
#      module guarantees the FIELD exists and is non-empty so that contract has something to bind to.
# --------------------------------------------------------------------------- #

_SYSTEM = """\
You are HR for an elite autonomous software unit. The unit's standing officer corps specialises in the
web stack (React/Vite + FastAPI + Supabase). A ticket has arrived in a DIFFERENT domain, and you must
synthesize the task-scoped SPECIALIST(S) needed to build it well — there is no permanent officer for
this domain, so you write their charters from scratch.

Produce between 1 and {max_n} specialists. Fewer is better: synthesize ONLY the distinct expertises this
specific ticket genuinely needs (a one-language algo ticket needs ONE specialist; a ticket spanning,
say, a smart contract + its off-chain indexer might need two). Do NOT pad the roster.

If you synthesize MORE THAN ONE specialist, their file ownership MUST be NON-OVERLAPPING: the unit runs
them SEQUENTIALLY on a SINGLE shared branch/worktree (never in parallel), so two specialists editing the
same files would clobber each other's work. Give each charter's `constraints` an explicit, DISJOINT set
of files/directories it owns, and forbid it from touching any other specialist's files.

Each specialist follows the unit's officer-charter shape (Identity / Knowledge / Skills / Constraints),
PLUS two extra fields:
  - lane_key:    a short stable kebab-case slug for this specialist's lane (e.g. "mql5-algo",
                 "solidity-contract", "rust-cli"). Lowercase, hyphenated, no spaces.
  - domain_gate: HOW this specialist verifies its OWN output in this domain. STRONGLY prefer a runnable
                 shell command (e.g. "mql5 compile via MetaEditor + Strategy Tester smoke run", "forge
                 test") — the unit EXECUTES it automatically and a non-zero exit fails the build. Only
                 when no command can exist, give a precise prose description of the manual check; a
                 prose-only gate becomes a MANUAL-QA step (the work still lands) rather than an automated
                 gate. This is the specialist's equivalent of the web corps' test/lint gate; it MUST be
                 concrete and non-empty.

Reply with ONLY a JSON array, no prose before or after it, in EXACTLY this shape:
[
  {{
    "name": "<role title, e.g. 'MQL5 Algorithmic Trading Developer'>",
    "lane_key": "<kebab-case slug>",
    "identity": "<one sharp paragraph: who they are, their bias, what they own — a NARROW specialist>",
    "knowledge": "<what they ground on: the ticket, the language/platform docs, the conventions, the files>",
    "skills": ["<numbered SOP step 1>", "<step 2>", "<self-check step>"],
    "constraints": "<hard non-negotiables: resource limits, the SPECIFIC files/directories this specialist OWNS and stays within, what NOT to touch (esp. OTHER specialists' files — you share one branch), output format>",
    "domain_gate": "<the self-authored verification command (preferred) or manual-check description>"
  }}
]"""


def _prompt(domain: str, ticket_text: str) -> str:
    """Build the synthesis task. Pure; kept separate so it's unit-testable without an agent."""
    return "\n".join([
        f"DOMAIN: {(domain or '').strip() or '(infer from the ticket)'}",
        "",
        "TICKET:",
        (ticket_text or "").strip()[:6000] or "(no ticket text given — infer from the domain)",
        "",
        "Synthesize the specialist charter(s) this ticket needs. Reply with ONLY the JSON array.",
    ])


def _coerce_skills(value) -> list[str]:
    """Normalise the `skills` field to a clean list of step strings (the template's numbered SOP).
    Accepts a JSON list or a newline/numbered string so a slightly-off model reply still parses."""
    if isinstance(value, list):
        steps = [str(s).strip() for s in value]
    else:
        steps = [ln.strip(" \t-*0123456789.)") for ln in str(value or "").splitlines()]
    return [s for s in (st.strip() for st in steps) if s]


def parse_specialists(text: str | None, *, max_n: int = 4) -> list[dict]:
    """Parse the model's reply into a list of validated charter dicts. Pure + deterministic, so it's
    unit-testable without an agent (mirrors recon.parse_slices).

    A charter is kept ONLY if it has a non-empty name, lane_key, identity AND domain_gate — the two
    EU-69 fields are mandatory (a specialist with no way to verify its own output is useless; see the
    GATE-SUPPLY contract above). Malformed / missing-field entries are dropped rather than guessed at.
    lane_key is slugified and de-duped so two charters never collide on the same lane."""
    if not text:
        return []
    m = re.search(r"\[\s*\{.*}\s*\]", text, re.S)   # first JSON array of objects
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out: list[dict] = []
    seen_lanes: set[str] = set()
    for item in (data if isinstance(data, list) else []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        lane = _slug(item.get("lane_key", ""))
        identity = str(item.get("identity", "")).strip()
        gate = str(item.get("domain_gate", "")).strip()
        if not (name and lane and identity and gate):   # mandatory fields — fail fast, don't fabricate
            continue
        if lane in seen_lanes:                            # one charter per lane
            continue
        seen_lanes.add(lane)
        out.append({
            "name": name,
            "lane_key": lane,
            "identity": identity,
            "knowledge": str(item.get("knowledge", "")).strip(),
            "skills": _coerce_skills(item.get("skills")),
            "constraints": str(item.get("constraints", "")).strip(),
            "domain_gate": gate,
            "ephemeral": True,   # explicit marker: NEVER written to officers/ (see module docstring)
        })
        if len(out) >= max(1, max_n):
            break
    return out


def _slug(value) -> str:
    """kebab-case a lane key: lowercase, non-alphanumerics → single hyphens, trimmed."""
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower())).strip("-")


def render_charter(c: dict) -> str:
    """Render one charter in the human-readable officer-template shape (for the approval print-out)."""
    skills = "\n".join(f"  {i}. {s}" for i, s in enumerate(c.get("skills") or [], 1)) or "  (none)"
    return "\n".join([
        f"# {c.get('name', '?')}  ·  lane: {c.get('lane_key', '?')}  ·  [EPHEMERAL]",
        "## Identity",
        c.get("identity", "") or "(none)",
        "## Knowledge",
        c.get("knowledge", "") or "(none)",
        "## Skills (SOP)",
        skills,
        "## Constraints (hard)",
        c.get("constraints", "") or "(none)",
        f"## Domain gate (self-authored): {c.get('domain_gate', '') or '(none)'}",
    ])


def _request_approval(charters: list[dict], approver) -> bool:
    """NON-automode gate: print the proposed charters and require the Commander to type 'y'. Any other
    answer (incl. EOF / a non-interactive stdin) is treated as NO — fail-safe: an unconfirmed ephemeral
    hire is discarded rather than dispatched. `approver` is injectable so this is testable without a TTY."""
    print(f"\n  HR proposes {len(charters)} ephemeral specialist(s) for this task:\n", flush=True)
    for c in charters:
        print(render_charter(c), flush=True)
        print("", flush=True)
    try:
        ans = approver("  Approve these ephemeral specialists? [y/N] ")
    except (EOFError, KeyboardInterrupt):   # no interactive Commander available → don't proceed
        return False
    return str(ans).strip().lower() in ("y", "yes")


def _opts(cfg: Config, model: str) -> ClaudeAgentOptions:
    """Options for the synthesis call. Read-only (HR writes charters, never code): the only repo access
    it needs is to ground on the template/conventions, so Write/Edit are denied and the zero-trust guard
    is attached exactly as every other officer."""
    return ClaudeAgentOptions(
        model=model,
        system_prompt=memory.preamble(),
        cwd=getattr(cfg.apps[0], "repo_path", ".") if getattr(cfg, "apps", None) else ".",
        permission_mode="bypassPermissions",
        allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "NotebookEdit"],
        hooks=guard.hooks_config(),
        setting_sources=["project"],
        max_turns=8,
        effort="medium",
    )


async def synthesize_specialists(domain: str, ticket_text: str, cfg, *, approver=_tty_input) -> list[dict]:
    """Synthesize the task-scoped specialist charter(s) for `domain`/`ticket_text` by calling Claude
    (Sonnet). Returns a list of EPHEMERAL charter dicts (template shape + lane_key + domain_gate); the
    charters are NEVER written to `officers/` — the caller holds them in memory for the task only.

    Approval gating:
      • automode (cfg.auto_mode True)  → auto-proceed: the synthesized charters are returned as-is and
        the unit keeps building (the Commander reviews/reverses afterward, as everywhere on DEV).
      • non-automode                   → the proposed charters are PRINTED and the Commander must type
        'y'; on anything else an EMPTY list is returned (the hire is discarded, fail-safe).

    `approver` is injected (defaults to builtin input) so the gate is unit-testable without a TTY. A
    synthesis / SDK failure degrades to an empty list with a warning — fail-safe over crashing the run."""
    max_n = max(1, int(getattr(cfg, "delegation_max_soldiers", 4)))
    # Pin Sonnet per the EU-69 spec: HR is a synthesis task, not code — route through the model ladder
    # with a Sonnet CEILING so auto_model can still conserve under a tight budget but never escalate to
    # Opus. With auto_model off this returns exactly Sonnet.
    model, mreason = models.for_officer(cfg, effort="medium", ceiling_model=models.SONNET)
    if getattr(cfg, "auto_model", False):
        print(f"  · hr model: {mreason}", flush=True)

    system = _SYSTEM.format(max_n=max_n)
    opts = _opts(cfg, model)
    opts.system_prompt = (memory.preamble() + system)
    try:
        run = await run_agent(_prompt(domain, ticket_text), opts, tag="hr")
    except Exception as exc:  # noqa: BLE001 — a synthesis hiccup must not crash the build loop
        print(f"  HR synthesis failed ({exc}); no ephemeral specialists provisioned.", flush=True)
        return []

    charters = parse_specialists(run.final or run.text, max_n=max_n)
    if not charters:
        print("  HR returned no usable specialist charters.", flush=True)
        return []

    if bool(getattr(cfg, "auto_mode", False)):
        print(f"  HR (automode): provisioning {len(charters)} ephemeral specialist(s) — "
              f"{', '.join(c['lane_key'] for c in charters)}.", flush=True)
        return charters

    if not _request_approval(charters, approver):
        print("  HR: Commander did not approve the ephemeral specialists — discarded.", flush=True)
        return []
    return charters


# ──────────────────────────────────────────────────────────────────────────────
# Usage tracking and auto-promote pipeline (EU-69 ephemeral lifecycle)
# ──────────────────────────────────────────────────────────────────────────────

def record_domain_use(domain: str, cfg) -> None:
    """Append one ``domain_use`` event for *domain* to the audit log.

    Called by ``build_delegated()`` after an ephemeral specialist synthesis whose
    ``BuildResult`` was ``ok`` — i.e. no domain gate FAILED (a 'pass' or a manual-QA
    'manual' gate both count; only a real EXECUTED non-zero gate does not). A
    failed-gate domain deliberately does NOT accrue auto-promotion credit, so the
    unit never enshrines a permanent officer for a domain it couldn't verify. The
    accumulated count drives ``check_promote``'s threshold logic; the entry is
    structurally identical to every other audit row so the cockpit and forensics
    tooling can read it without modification."""
    from .audit import AuditLog
    AuditLog(cfg.audit_path).record("domain_use", domain=domain)


def _charter_body(domain: str) -> str:
    """Return a minimal permanent-charter stub for a promoted domain specialist.

    The stub follows the unit's Identity / Knowledge / Skills template shape and
    is intentionally lean — the Commander or Engineering Manager should enrich it
    once the specialist earns a permanent post.  It is written verbatim to
    ``officers/<domain>-specialist.md`` by ``adjutant.hire()``."""
    title = domain.replace("-", " ").title() + " Specialist"
    return "\n".join([
        f"# {title}",
        "",
        "## Identity",
        f"Permanent specialist for the {domain} domain. Auto-promoted after recurring ephemeral "
        f"use (threshold reached). Refine this charter to reflect the unit's actual SOP for this "
        f"domain before assigning mission-critical work.",
        "",
        "## Knowledge",
        f"Ground on: the {domain} platform conventions and toolchain, the repo's CLAUDE.md and "
        f".claude/rules/ files, and any relevant ticket descriptions.",
        "",
        "## Skills (SOP)",
        "1. Read existing code and conventions before writing anything.",
        "2. Implement the ticket's scope only — do NOT refactor unrelated code.",
        "3. Verify output using the domain's own gate (compile / test / lint) before finishing.",
        "4. Surface any gate failure explicitly rather than silently proceeding.",
        "",
        "## Constraints (hard)",
        "- Stay within the ticket's stated scope.",
        "- Do NOT modify CI workflow files (`.github/workflows/`).",
        "- Never touch MAIN; land changes on DEV only.",
        "- Follow the zero-trust guard (no dangerous shell commands).",
    ])


def check_promote(domain: str, cfg, threshold: int = AUTO_PROMOTE_THRESHOLD,
                  *, approver=_tty_input) -> bool:
    """Count how many times *domain* has been used as an ephemeral specialist and,
    if the count meets *threshold*, propose writing a permanent charter via
    ``adjutant.hire()``.

    Promotion gating mirrors ``synthesize_specialists``:
      • automode  → proceeds automatically; the Commander reviews after the fact.
      • non-automode → the Commander is prompted; declining returns ``False`` (fail-safe).

    Returns ``True`` when a charter was written, ``False`` otherwise (below threshold,
    Commander declined, file already exists, or the audit log is unreadable).

    ``approver`` is injectable for unit tests (avoids a real TTY).

    Idempotent: a ``FileExistsError`` from ``adjutant.hire()`` is silently swallowed —
    the specialist is already in post."""
    audit_path = Path(cfg.audit_path)
    if not audit_path.exists():
        return False

    # Count domain_use events for this exact domain — fail-safe on malformed lines.
    count = 0
    try:
        for raw in audit_path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if entry.get("event") == "domain_use" and entry.get("domain") == domain:
                count += 1
    except OSError:
        return False

    if count < threshold:
        return False

    # Threshold reached — build the permanent charter and route through the gate.
    from . import adjutant
    name = f"{domain}-specialist"
    body = _charter_body(domain)

    if bool(getattr(cfg, "auto_mode", False)):
        print(
            f"  HR promote (automode): domain '{domain}' used {count}× (≥{threshold}) — "
            f"writing permanent charter officers/{name}.md",
            flush=True,
        )
        try:
            adjutant.hire(cfg, name, body)
        except FileExistsError:
            pass  # already in post — idempotent
        return True

    # Non-automode: present the proposal and require the Commander's approval.
    print(
        f"\n  HR: domain '{domain}' has been used {count} times (threshold: {threshold}).",
        flush=True,
    )
    print(f"  Proposed permanent charter: officers/{name}.md", flush=True)
    try:
        ans = approver(f"  Promote '{domain}' to a permanent specialist? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return False
    if str(ans).strip().lower() not in ("y", "yes"):
        return False

    try:
        adjutant.hire(cfg, name, body)
    except FileExistsError:
        pass  # already in post — idempotent
    return True
