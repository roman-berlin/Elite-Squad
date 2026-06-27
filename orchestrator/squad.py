"""Squad delegation — the Dev Team Lead's chain of command executes.

For a big ticket, the Dev Team Lead (squad lead) decomposes the work into a few non-overlapping
subtasks, each owned by the right SOLDIER (frontend / backend / DB / DevOps / generalist) and
SIZED to its slice (task-adaptive effort, all the way down to the soldier). The soldiers run
sequentially on the SAME isolated branch; the orchestrator's gate + review + keep-DEV-green merge
still validate the combined result, and MAIN is never touched.

Design notes (Roman's reflexes):
- Fail-safe: any planning hiccup (no/!invalid plan, <2 subtasks, an exception) falls back to the
  normal SOLO build — delegation never breaks a run.
- Off by default (`delegation_enabled`), and only ever engages on iteration 1 of a sized-big
  ticket; a rejected retry goes back to a single focused builder pass.
- Sequential, not parallel: soldiers share one worktree, so concurrent edits would conflict (and
  the dev machine has limited RAM). One soldier at a time.
- Domain-gap detection (EU-69): before planning, a lightweight Haiku call checks whether the
  ticket's primary domain falls outside the fixed SQUAD lanes.  A gap routes to the synthesis
  flow instead of the normal soldier split.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field

from claude_agent_sdk import ClaudeAgentOptions

from . import guard, memory, models
from .agent import run_agent
from .config import AppConfig, Config
from .contracts import BuildRequest, BuildResult

# Squad roles (from officers/engineer.md). key -> (label, focus line for the soldier's system).
SQUAD: dict[str, tuple[str, str]] = {
    "vanguard-fe": ("Frontend Engineer", "Frontend — React/Vite, TypeScript, Tailwind, components, UI state."),
    "ordnance-be": ("Ordnance BE", "Backend — FastAPI/Python: routes, services, repositories, schemas."),
    "logistics-db": ("Logistics DB", "Data — Supabase/Postgres: SQL migrations, RLS policies, types."),
    "devops": ("DevOps", "CI / build / deploy / config — package scripts, env, Docker, Vercel/Turbo."),
    "generalist": ("Software Engineer", "General-purpose — anything outside the specialist lanes, or glue work."),
}

# subtask size -> builder effort (task-adaptive effort, extended to the soldiers; capped at high).
_SIZE_EFFORT = {"XS": "low", "S": "medium", "M": "medium", "L": "high", "XL": "high"}


@dataclass
class Subtask:
    role: str          # a SQUAD key
    title: str
    detail: str
    size: str = "M"    # XS | S | M | L | XL
    def effort(self) -> str:
        return _SIZE_EFFORT.get((self.size or "M").upper(), "medium")


def _extract_json_array(text: str | None):
    """Pull the first JSON array of objects out of an agent reply (tolerant of prose / fences)."""
    if not text:
        return None
    m = re.search(r"\[\s*\{.*}\s*\]", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, list) else None
    except json.JSONDecodeError:
        return None


def parse_subtasks(text: str | None) -> list[Subtask]:
    """Validate the planner's JSON into Subtasks: unknown roles -> generalist, bad size -> M,
    empty detail dropped. Pure + deterministic, so it's unit-testable without an agent."""
    out: list[Subtask] = []
    for item in (_extract_json_array(text) or []):
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip().lower()
        if role not in SQUAD:
            role = "generalist"
        detail = str(item.get("detail", "")).strip()
        if not detail:
            continue
        title = str(item.get("title", "")).strip()[:120] or detail[:60]
        size = str(item.get("size", "M")).strip().upper()
        if size not in _SIZE_EFFORT:
            size = "M"
        out.append(Subtask(role=role, title=title, detail=detail, size=size))
    return out


_GAP_SYSTEM = """\
You are a domain classifier. Given a software ticket and a list of engineering squad lanes, \
decide whether the ticket's primary technical domain is covered by one of those lanes.

Reply with ONLY valid JSON — no prose, no fences.
If covered by a lane: {"covered": true, "domain": "<lane_key>"}
If outside all lanes: {"covered": false, "domain": "<short domain label, e.g. mql5, rust, ml>"}"""


async def detect_domain_gap(ticket_text: str, squad: dict) -> tuple[bool, str | None]:
    """Classify the ticket's primary domain against the known squad lanes using a Haiku call.

    Returns ``(True, '<domain>')`` when no lane covers the ticket (e.g. ``(True, 'mql5')``),
    or ``(False, None)`` when a lane already handles it.

    Fail-safe: any error (network, parse, SDK) returns ``(False, None)`` so it never blocks the
    normal delegation / solo-build path.

    Args:
        ticket_text: Combined summary + description + acceptance criteria of the ticket.
        squad: The squad lane mapping — same shape as the module-level ``SQUAD`` dict.
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
        run = await run_agent(prompt, options, tag="gap-detect")
        text = (run.final or run.text or "").strip()
        # Tolerate prose wrapping; pull the first {...} block.
        m = re.search(r"\{[^}]+\}", text)
        if not m:
            return False, None
        data = json.loads(m.group(0))
        covered = bool(data.get("covered", True))
        domain = str(data.get("domain", "")).strip().lower() or None
        if covered:
            return False, None
        return True, domain
    except Exception:  # noqa: BLE001 — fail-safe: gap detection must never break a run
        return False, None


def _ticket_in_backlog(ticket_id: str, app: AppConfig, cfg: Config) -> bool:
    """Check whether *ticket_id* exists in the Jira backlog.

    Fail-**open**: returns ``True`` on any error except a clear HTTP 404 (ticket genuinely
    absent) so a transient network blip never blocks specialist provisioning.  Returns
    ``True`` unconditionally for apps with ``backlog_backend: none`` (no Jira to check)."""
    try:
        from .backlog.base import make_backlog, NoneBacklog
        bl = make_backlog(app)
        if isinstance(bl, NoneBacklog):
            return True   # no backlog — allow provisioning
        bl.get_task(ticket_id)
        return True
    except Exception as exc:  # noqa: BLE001
        # Fail-closed only on a definitive "not found" HTTP 404.
        try:
            import requests
            if isinstance(exc, requests.exceptions.HTTPError):
                if getattr(exc.response, "status_code", None) == 404:
                    return False
        except ImportError:
            pass
        return True   # network error / misconfigured backlog: fail-open


async def _run_gate(gate_cmd: str | None, cwd: str) -> tuple[str, str]:
    """Execute the specialist's domain_gate command in the worktree; return ``(status, report)``.

    ``status`` is an explicit TRI-STATE (EU-69 iteration-3). A bare bool conflated two very different
    things — "the gate ran and proved the code wrong" and "the gate could not be run at all" — and the
    latter must NOT fail the build. An MQL5/Solidity charter whose gate is a prose description, or one
    that names a tool not installed in this worktree, is a MANUAL-QA item, not a verification failure:
      • ``'pass'``   — the gate ran and exited 0. Verified.
      • ``'fail'``   — the gate ran and exited non-zero (a real, EXECUTED verification failure). This is
                       the ONLY status the caller turns into ``ok=False`` (→ Outcome.ERRORED).
      • ``'manual'`` — the gate could not be executed AS a verification: missing/empty, prose that isn't
                       a runnable command (shell exit 126/127 = not-executable / command-not-found), a
                       guard-blocked command, a timeout, or an OS error. The work still LANDS, carrying a
                       manual-QA note for the Commander/review rather than erroring the whole ticket.

    The command is model-authored (UNTRUSTED) and is checked against the same guard denylist as every
    other Bash call before execution. The subprocess is capped at 120 s so a runaway compile or test
    suite can't stall the build loop (a timeout is reported as 'manual', not 'fail').

    Args:
        gate_cmd: The ``domain_gate`` string from the specialist charter — a shell command or a prose
            description. A prose gate runs as a shell line whose first word isn't a command, so it exits
            127 and is reported as 'manual'.
        cwd: Worktree directory to run the command in (same cwd the soldier used).
    """
    if not (gate_cmd and gate_cmd.strip()):
        return "manual", "⚠️  No domain_gate provided by the specialist — manual QA required."

    # Zero-trust: reject anything the denylist catches (same rules as every Bash agent call). A blocked
    # gate never executed, so it is NOT a verification failure — surface it loudly as a manual-QA item.
    blocked, reason = guard.is_dangerous("Bash", {"command": gate_cmd})
    if blocked:
        return "manual", f"⚠️  domain_gate BLOCKED by the hard guardrail — {reason}; manual QA required."

    try:
        proc = await asyncio.create_subprocess_shell(
            gate_cmd, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return "manual", f"⚠️  domain_gate timed out (120 s) — manual QA required: {gate_cmd[:80]}"
        output = stdout.decode("utf-8", errors="replace").strip()
        rc = proc.returncode
        # Shell convention: 127 = command not found, 126 = found-but-not-executable. A model-authored
        # PROSE gate ("backtest drawdown < 20%") runs as a shell line whose first word isn't a command,
        # so it exits 127 — that is "this wasn't a runnable gate", NOT a verification failure. A real
        # tool (cargo / forge) that isn't installed in this worktree lands here too. Both are MANUAL.
        if rc in (126, 127):
            return "manual", (f"⚠️  domain_gate not executable (exit {rc} — command not found / "
                              f"prose-only gate) — manual QA required: {gate_cmd[:80]}\n{output}")
        if rc == 0:
            return "pass", f"domain_gate [PASS] (exit 0): {gate_cmd[:80]}\n{output}"
        return "fail", f"domain_gate [FAIL] (exit {rc}): {gate_cmd[:80]}\n{output}"
    except Exception as exc:  # noqa: BLE001 — gate failure must not crash the build loop
        # Command not found, cwd missing, permission error, or unparseable prose — surface as a
        # manual QA note rather than a hard crash, so the BuildResult carries the warning.
        return "manual", f"⚠️  domain_gate could not run ({exc!r}): {gate_cmd[:80]} — manual QA required."


async def _run_synthesis(gap_domain: str, req: BuildRequest, app: AppConfig, cfg: Config):
    """Provision ephemeral specialists via HR and dispatch each as a soldier (EU-69/EU-88).

    EU-88 ask-once / park discipline:
      • Non-automode + real (non-ephemeral) ticket: the approval request is posted to Telegram
        ONCE, a "pending" state is written to ``pending_specialist_approvals.json``, and the
        function returns ``None`` (solo-build fallback) until the Commander approves.  On every
        subsequent call for the same (ticket, domain) pair the "pending" guard fires BEFORE the
        model call — no re-post, no wasted tokens.
      • A "declined" state also short-circuits without a model call.
      • An "approved" state (set by ``decisions.handle_reply`` → ``hr.resolve_specialist_approval_reply``)
        bypasses the gate and proceeds directly to specialist dispatch.
      • EU-88 Jira existence check: a roster is NEVER proposed for a ticket that doesn't exist in
        the backlog — provisioning for a phantom ticket is skipped with a Telegram warning.
      • automode: proceeds automatically as before (no Telegram gate, no state tracking).

    Returns ``None`` (signalling solo fallback) when HR returns no usable charters or when
    the gate parks the ticket for Commander approval.

    Args:
        gap_domain: Short domain label from ``detect_domain_gap`` (e.g. ``'mql5'``).
        req: The current build request.
        app: App configuration (determines the worktree directory).
        cfg: Global orchestrator configuration.
    """
    from . import hr as _hr

    is_auto = bool(getattr(cfg, "auto_mode", False))
    # tid is None for ephemeral (trackerless) tickets — they skip the Telegram gate entirely.
    tid = req.ticket.id if not getattr(req.ticket, "ephemeral", False) else None

    # Current approval status (non-automode + real ticket only); None everywhere else.
    status = _hr.get_spec_approval(cfg, tid, gap_domain) if (not is_auto and tid) else None

    # ── EU-88 ask-once dedup: check state BEFORE any model call ────────────
    if status == "pending":
        print(
            f"  squad · specialist approval already pending for {tid} [{gap_domain}] — "
            f"skipping (dedup, no re-post).", flush=True,
        )
        return None
    if status == "declined":
        print(
            f"  squad · specialist provisioning was declined for {tid} [{gap_domain}] — "
            f"skipped.", flush=True,
        )
        return None

    # ── EU-88: on an APPROVED re-run, reuse the EXACT roster the Commander approved ──
    # The charters were pinned to the approval entry when the request was posted, so the
    # specialists (and their domain_gate commands) we provision are precisely the ones approved —
    # NOT a freshly re-synthesized roster that could differ. If the entry has no pinned charters
    # (e.g. an 'approved' state set directly), fall back to synthesis below.
    approved_charters = (
        _hr.get_spec_charters(cfg, tid, gap_domain) if status == "approved" else None
    )

    if approved_charters:
        charters = approved_charters
        print(
            f"  squad · provisioning the approved roster for {tid} [{gap_domain}] "
            f"({len(charters)} specialist(s)) — reusing the pinned charters.", flush=True,
        )
    else:
        # ── EU-88 Jira existence gate: never propose a roster for a phantom ticket ──
        if tid and not getattr(cfg, "dry_run", False):
            if not _ticket_in_backlog(tid, app, cfg):
                try:
                    from . import notify as _notify
                    _notify.send(
                        f"⚠️ HR: cannot provision specialists for {tid} — "
                        f"ticket does not exist in Jira. Create the ticket first."
                    )
                except Exception:  # noqa: BLE001
                    pass
                print(
                    f"  squad · domain gap '{gap_domain}' skipped — {tid} not found in Jira.",
                    flush=True,
                )
                return None

        ticket_text = "\n".join(filter(None, [
            req.ticket.summary or "",
            req.ticket.description or "",
            *list(req.ticket.acceptance_criteria or []),
        ]))

        # When the Telegram gate manages the real approval (non-automode + real ticket), inject an
        # auto-approving callback so synthesize_specialists returns charters without blocking on
        # stdin. The Telegram gate below is then the only approval surface for this path.
        _telegram_gate = not is_auto and tid is not None
        _synth_approver = (lambda _: "y") if _telegram_gate else _hr._tty_input

        charters = await _hr.synthesize_specialists(
            gap_domain, ticket_text, cfg, approver=_synth_approver
        )
        if not charters:
            return None  # no usable specialists — caller falls through to solo build

        # ── EU-88 non-automode Telegram gate (post ONCE, park, await Commander) ──
        # status is None here (pending/declined returned above; approved+pinned used the roster
        # above). Pin the synthesized charters so the approved re-run provisions exactly these.
        if _telegram_gate and status != "approved":
            roster_lines = "\n".join(
                f"  {i}. [{c['lane_key']}] {c['name']}"
                for i, c in enumerate(charters, 1)
            )
            msg = (
                f"🎖 HR — Specialist roster for domain '{gap_domain}' (ticket {tid}):\n"
                f"{roster_lines}\n"
                f"⏸ Awaiting Commander approval before provisioning these specialists.\n"
                f"Reply  {tid}: approve  to provision, or  {tid}: decline  to skip."
            )
            _hr.set_spec_approval(
                cfg, tid, gap_domain, "pending",
                charters=charters,
                ticket_summary=req.ticket.summary or "",
                ticket_description=req.ticket.description or "",
                ticket_ac=list(req.ticket.acceptance_criteria or []),
                app_name=app.name,
            )
            try:
                from . import notify as _notify
                _notify.send(msg)
            except Exception:  # noqa: BLE001 — Telegram failure must not crash the build
                pass
            print(
                f"  squad · specialist approval request posted for {tid} [{gap_domain}] — "
                f"awaiting Commander (ticket parked).", flush=True,
            )
            return None  # park — do not provision until the Commander approves

    # Key by lane_key for O(1) lookup in _soldier() and _run_gate().
    specialists: dict[str, dict] = {c["lane_key"]: c for c in charters}

    # One subtask per specialist: implement the full ticket in their domain.
    subtasks = [
        Subtask(
            role=c["lane_key"],
            title=c["name"],
            detail=(req.ticket.description or req.ticket.summary or "").strip()[:2000],
            size="M",
        )
        for c in charters
    ]

    cwd = app.workdir or app.repo_path
    cost, turns, tools, summaries = 0.0, 0, [], []
    ok = True
    manual_notes: list[str] = []   # gates that couldn't run AS a verification (prose / missing / blocked / timeout)

    print(
        f"  squad · ephemeral delegation — {len(subtasks)} specialist(s): "
        + ", ".join(c["name"] for c in charters),
        flush=True,
    )

    for i, st in enumerate(subtasks, 1):
        specialist = specialists[st.role]
        print(
            f"  specialist {i}/{len(subtasks)} · {specialist['name']} "
            f"(effort {st.effort()}) — {st.title}",
            flush=True,
        )
        run, _ = await _soldier(st, req, app, cfg, i, len(subtasks), specialists=specialists)
        cost += run.cost_usd
        turns += run.num_turns
        tools += run.tools
        if run.is_error:
            ok = False

        # Run the specialist's own domain gate after each soldier finishes. The gate is a TRI-STATE
        # (EU-69 iter-3): only a REAL executed non-zero exit ('fail') fails the build. A gate that can't
        # be executed AS a verification ('manual' — prose, tool-not-installed, guard-blocked, timeout)
        # lands the work with a manual-QA note instead of routing the whole ticket to Outcome.ERRORED.
        gate_cmd = (specialist.get("domain_gate") or "").strip()
        if gate_cmd:
            gate_status, gate_report = await _run_gate(gate_cmd, cwd)
        else:
            # parse_specialists makes domain_gate mandatory, so an empty one means a malformed charter
            # slipped through — treat as manual QA, never a hard fail.
            gate_status = "manual"
            gate_report = f"⚠️  No domain_gate for {specialist['name']} — manual QA required."

        gate_label = {"pass": "PASS", "fail": "FAIL", "manual": "MANUAL"}[gate_status]
        print(f"  domain_gate [{gate_label}] {specialist['name']}: {gate_cmd[:80]}", flush=True)
        if gate_status == "fail":
            ok = False                       # the ONLY path that fails the build → Outcome.ERRORED
        elif gate_status == "manual":
            manual_notes.append(f"{specialist['name']} (lane {st.role}): {gate_report}")

        summaries.append(
            f"[{specialist['name']} · {st.size} · gate:{gate_label}] {st.title}\n"
            f"{(run.final or '').strip()}\n{gate_report}"
        )

    header = (f"Ephemeral-specialist delegation — {len(subtasks)} specialist(s) dispatched "
              f"[domain: {gap_domain}]:")
    if manual_notes:
        # Carry the manual-QA note into the BuildResult summary so a non-executable gate reaches
        # review/landing as a MANUAL-QA item (the build stays ok=True) instead of being mislabelled a
        # builder failure at loop.py's `if not build.ok`.
        header += (
            "\n\n⚠️  MANUAL QA REQUIRED — one or more specialist domain gates could not be executed as "
            "an automated verification (prose-only description / tool not installed / blocked / timed "
            "out). This change LANDS, but a human must verify the domain output before release:\n  - "
            + "\n  - ".join(manual_notes)
        )
    summary = header + "\n\n" + "\n\n".join(summaries)

    # ── EU-88: an approved re-run has now provisioned — expire the approval entry so a FUTURE run
    # of the same (ticket, domain) re-asks the Commander rather than silently re-provisioning from
    # the stale 'approved' state. Best-effort: a state-write hiccup must not lose the BuildResult.
    if status == "approved" and tid:
        try:
            _hr.clear_spec_approval(cfg, tid, gap_domain)
        except Exception:  # noqa: BLE001
            pass

    return BuildResult(ok=ok, summary=summary, cost_usd=cost, num_turns=turns,
                       raw=summary, tools=tools)


_PLANNER_SYSTEM = """\
You are the Dev Team Lead (squad lead) planning how to split ONE ticket across your squad so
specialists each own their slice. Read what you need (Grep/Glob/Read only — do NOT edit). Decide
the SMALLEST set of subtasks (1-5) that fully covers the acceptance criteria, each owned by the
right engineer. If the ticket is genuinely atomic (one concern, one area), return a SINGLE subtask.

Roles (use the exact key):
- vanguard-fe   : frontend (React/Vite/TS/Tailwind)
- ordnance-be   : backend (FastAPI/Python)
- logistics-db  : database (Supabase/SQL/migrations/RLS)
- devops        : CI/build/deploy/config
- generalist    : anything else / glue

Reply with ONLY a JSON array, no prose, each item exactly:
{"role":"<key>","title":"<short>","detail":"<what to implement + which files/area>","size":"XS|S|M|L"}
Order them so dependencies come first (e.g. a DB migration before the backend that uses it). Keep
slices NON-OVERLAPPING — two engineers must never edit the same lines."""

_SOLDIER_SYSTEM = """\
You are an ENGINEER of the Dev Team Lead's squad — {label}: {focus}
You implement ONE subtask of a larger ticket, on the current git branch, surgically.

HOUSE RULES: before editing, read this repo's conventions and follow them strictly — CLAUDE.md at
the repo root and the relevant .claude/rules/ files (Bun-only, never npm; tenant-isolation /
zero-trust; TypeScript conventions). Stay strictly inside YOUR subtask — do NOT implement other
engineers' slices, and do NOT refactor unrelated code. Match existing conventions. Add or adjust
ONLY the tests for what you changed.

Resource safety (limited RAM): never run the whole suite; run ONLY targeted tests with bounded
workers, e.g. `npx vitest run <path> --pool=forks --poolOptions.forks.maxForks=2`; prefer
`tsc --noEmit` / eslint on changed files. Never start watch mode or dev servers.

Git: the orchestrator owns git and has already placed you on the correct branch (cwd = repo root).
Do NOT run git at all. Spend your turns on the code.

Finish with a short plain-text summary: what you changed and which file(s)."""

# System prompt for EPHEMERAL (HR-synthesized) specialists. The charter text (identity / knowledge /
# skills / constraints) is injected below the standard house-rules block so the specialist retains
# its domain expertise while obeying the same zero-trust / git-safety invariants as every fixed lane.
_EPHEMERAL_SOLDIER_SYSTEM = """\
You are an ENGINEER of the Dev Team Lead's squad — {name} (ephemeral, task-scoped specialist).
You implement ONE subtask of a larger ticket, on the current git branch, surgically.

HOUSE RULES: before editing, read this repo's conventions and follow them strictly — CLAUDE.md at
the repo root and the relevant .claude/rules/ files (Bun-only, never npm; tenant-isolation /
zero-trust; TypeScript conventions). Stay strictly inside YOUR subtask — do NOT implement other
engineers' slices, and do NOT refactor unrelated code. Match existing conventions. Add or adjust
ONLY the tests for what you changed.

Resource safety (limited RAM): never run the whole suite; run ONLY targeted tests with bounded
workers, e.g. `npx vitest run <path> --pool=forks --poolOptions.forks.maxForks=2`; prefer
`tsc --noEmit` / eslint on changed files. Never start watch mode or dev servers.

Git: the orchestrator owns git and has already placed you on the correct branch (cwd = repo root).
Do NOT run git at all. Spend your turns on the code.

Finish with a short plain-text summary: what you changed and which file(s).

---
YOUR SPECIALIST CHARTER (domain-specific SOP):
## Who you are
{identity}

## Knowledge base
{knowledge}

## Skills (SOP)
{skills}

## Constraints (hard)
{constraints}"""


def _planner_prompt(req: BuildRequest) -> str:
    ac = "\n".join(f"  - {c}" for c in req.ticket.acceptance_criteria) or "  (none specified)"
    return "\n".join([
        f"TICKET {req.ticket.id}: {req.ticket.summary}", "",
        "DESCRIPTION:", req.ticket.description or "(none)", "",
        "ACCEPTANCE CRITERIA:", ac, "",
        "Plan the squad split now — JSON array only.",
    ])


def _soldier_prompt(st: Subtask, req: BuildRequest, idx: int, total: int,
                    specialists: dict | None = None) -> str:
    """Build the user-turn prompt for a soldier.

    When `specialists` is provided and the subtask's role matches an ephemeral lane_key, the
    specialist's human-readable name is used as the role label; otherwise falls back to the fixed
    SQUAD lookup (or 'generalist' for any unknown role).
    """
    ac = "\n".join(f"  - {c}" for c in req.ticket.acceptance_criteria) or "  (none)"
    specialist = (specialists or {}).get(st.role)
    label = specialist["name"] if specialist else SQUAD.get(st.role, SQUAD["generalist"])[0]
    parts = [
        f"Parent ticket {req.ticket.id}: {req.ticket.summary}",
        f"(You are engineer {idx} of {total}. Other engineers handle the rest — stay in your slice.)",
    ]
    # EU-69 (iter-3): synthesized specialists share ONE worktree and run SEQUENTIALLY, so >1 of them
    # editing the same files would clobber each other (the fixed-lane planner avoids this by handing out
    # non-overlapping slices). Name the OTHER lanes explicitly so each specialist stays off their files —
    # this enforces the non-overlap that each charter's Constraints already declare.
    co_specialists = [f"{s['name']} (lane: {k})" for k, s in (specialists or {}).items() if k != st.role]
    if specialist and co_specialists:
        parts.append(
            "CO-SPECIALISTS share this branch and own the OTHER lanes below — edit ONLY the files your "
            "own charter's Constraints claim, NEVER theirs:\n  - " + "\n  - ".join(co_specialists))
    parts += [
        "", "FULL ACCEPTANCE CRITERIA (context — you own only your slice):", ac, "",
        f"YOUR SUBTASK [{label}] — {st.title}:", st.detail, "",
    ]
    if idx > 1:
        parts.append("Earlier engineers already changed this branch; build on their work, don't redo it.")
    parts.append("Implement your subtask now.")
    return "\n".join(parts)


async def _plan(req: BuildRequest, app: AppConfig, cfg: Config):
    """Read-only planning pass. Returns (subtasks, cost, turns, tools, gap_domain).

    ``gap_domain`` is ``None`` for tickets that fit the fixed squad lanes, or a short domain
    label (e.g. ``'mql5'``) when ``detect_domain_gap`` finds no covering lane.  When a gap is
    detected, the planner LLM call is skipped — the caller routes to the synthesis flow instead.
    """
    # EU-69: domain-gap check runs before the expensive planner LLM call so we never waste
    # tokens planning a squad split for a domain no soldier can handle.
    ticket_text = "\n".join(filter(None, [
        req.ticket.summary or "",
        req.ticket.description or "",
        *list(req.ticket.acceptance_criteria or []),
    ]))
    gap, gap_domain = await detect_domain_gap(ticket_text, SQUAD)
    if gap:
        return [], 0.0, 0, [], gap_domain

    cwd = app.workdir or app.repo_path
    # EU-52: the read-only squad-planning pass runs through the ladder under the Builder's ceiling — a
    # medium-effort plan sizes down a tier and conserves under a tight budget; auto_model off = ceiling.
    model, mreason = models.for_officer(cfg, effort="medium", ceiling_model=cfg.builder_model)
    if getattr(cfg, "auto_model", False):
        print(f"  · squad-plan model: {mreason}", flush=True)
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=memory.preamble() + _PLANNER_SYSTEM,
        cwd=cwd, permission_mode="bypassPermissions",   # read-only plan; unattended — never dead-stop
        allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Write", "Edit", "Bash", "NotebookEdit"],
        setting_sources=[], max_turns=14, effort="medium")
    run = await run_agent(_planner_prompt(req), options, tag="squad-lead")
    return parse_subtasks(run.final or run.text), run.cost_usd, run.num_turns, run.tools, None


async def _soldier(st: Subtask, req: BuildRequest, app: AppConfig, cfg: Config, idx: int, total: int,
                   specialists: dict[str, dict] | None = None,
                   iteration: int = 1) -> tuple:
    """Dispatch one soldier (fixed SQUAD lane OR ephemeral specialist); return ``(AgentRun, mreason)``.

    When `specialists` is provided and `st.role` matches a lane_key, the ephemeral charter text is
    injected as the system prompt instead of loading from ``~/.claude/agents/``. The fixed-lane
    path is strictly unchanged — this function is additive only.

    Args:
        st: The subtask this soldier owns.
        req: The parent build request.
        app: App configuration (determines the working directory).
        cfg: Global orchestrator configuration.
        idx: 1-based position of this soldier in the overall squad.
        total: Total number of soldiers dispatched for this ticket.
        specialists: Optional dict keyed by lane_key → ephemeral charter dict from
            ``hr.synthesize_specialists()``. When None (or when ``st.role`` is not a key),
            the fixed SQUAD lookup is used unchanged.
        iteration: Pass number from the parent BuildRequest (1 = first attempt). Drives the
            cheap-first escalation ladder in ``for_soldier_build`` — a rejected cheap pass
            re-runs on a stronger model automatically.
    """
    from .builder import turns_for
    guard.warn_if_absent(f"soldier·{st.role}")   # EU-2 F7: loud one-liner if guard is absent under bypass
    cwd = app.workdir or app.repo_path

    # Ephemeral path: the charter drives the system prompt; fixed path: SQUAD dict unchanged.
    specialist = (specialists or {}).get(st.role)
    if specialist:
        skills_text = "\n".join(
            f"  {i}. {s}" for i, s in enumerate(specialist.get("skills") or [], 1)
        ) or "  (none)"
        system_prompt = memory.preamble() + _EPHEMERAL_SOLDIER_SYSTEM.format(
            name=specialist["name"],
            identity=specialist.get("identity", "") or "(none)",
            knowledge=specialist.get("knowledge", "") or "(none)",
            skills=skills_text,
            constraints=specialist.get("constraints", "") or "(none)",
        )
    else:
        # Fixed SQUAD lane — existing behaviour, additive only.
        label, focus = SQUAD.get(st.role, SQUAD["generalist"])
        system_prompt = memory.preamble() + _SOLDIER_SYSTEM.format(label=label, focus=focus)

    # EU-91: all SQUAD roles emit code — use for_soldier_build (same cheap-first escalation as the
    # Builder) rather than for_officer's size-once approach. Pass the current iteration so that a
    # rejected cheap pass automatically escalates to a stronger model on retry. Floor = Sonnet;
    # ceiling = cfg.builder_model.
    model, mreason = models.for_soldier_build(cfg, effort=st.effort(), iteration=iteration)
    if getattr(cfg, "auto_model", False):
        print(f"  · soldier·{st.role} model: {mreason}", flush=True)
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system_prompt,
        cwd=cwd, permission_mode="bypassPermissions",
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        hooks=guard.hooks_config(),    # same hard denylist as the builder
        setting_sources=[], max_turns=turns_for(cfg, st.effort()), effort=st.effort())
    run = await run_agent(
        _soldier_prompt(st, req, idx, total, specialists=specialists),
        options, tag=f"soldier·{st.role}",
    )
    return run, mreason


def should_delegate(cfg: Config, req: BuildRequest) -> bool:
    """Delegate only on the first pass of a sized-big ticket (L/XL or many acceptance criteria).
    Retries are targeted fixes -> a single focused builder pass, never a re-split."""
    if not getattr(cfg, "delegation_enabled", False):
        return False
    if req.iteration != 1:
        return False
    from .builder import size_ticket
    size, _eff, _why = size_ticket(req.ticket)
    if size in ("L", "XL"):
        return True
    return len(req.ticket.acceptance_criteria or []) >= int(getattr(cfg, "delegation_min_ac", 3))


async def build_delegated(req: BuildRequest, app: AppConfig, cfg: Config, audit=None):
    """Plan -> dispatch soldiers -> aggregate into one BuildResult. Returns (result, n_subtasks).

    n_subtasks meanings:
      0   — thin plan (<2 subtasks), or a domain gap whose HR synthesis yielded no usable charter →
            caller does the solo build.
      1   — the ephemeral-synthesis flow handled the ticket (n=1 signals it, not a squad split): a
            BuildResult is returned even when its domain gate is a manual-QA item (ok=True) so the
            ticket reaches review/landing rather than ERRORING.
      ≥2  — normal squad delegation with that many soldiers.

    Returns (None, 0) when the plan has <2 subtasks, signalling the caller to do a solo build.
    Returns (None, 0) when a domain gap is detected but HR provisioned no usable specialist.
    """
    subtasks, p_cost, p_turns, p_tools, gap_domain = await _plan(req, app, cfg)

    # EU-69: a detected domain gap skips the soldier loop entirely and hands off to synthesis.
    if gap_domain is not None:
        print(f"  squad · domain gap '{gap_domain}' — routing to synthesis flow", flush=True)
        synthesis = await _run_synthesis(gap_domain, req, app, cfg)
        # None → fall through to solo build; a BuildResult means the synthesis flow handled it.
        # EU-69 (iter-3) lifecycle: only a SUCCESSFUL synthesis (gate not failed → ok=True) accrues
        # auto-promotion credit. A failed-gate domain (ok=False) must NOT count toward promoting the lane
        # to a permanent officer — we don't enshrine a domain we couldn't actually verify. Both calls are
        # best-effort: a tracking hiccup must never abort the result.
        if synthesis is not None and synthesis.ok:
            try:
                from . import hr as _hr
                _hr.record_domain_use(gap_domain, cfg)
                _hr.check_promote(gap_domain, cfg)
            except Exception:  # noqa: BLE001 — tracking hiccup must not lose the BuildResult
                pass
        return synthesis, (1 if synthesis is not None else 0)

    cap = max(1, int(getattr(cfg, "delegation_max_soldiers", 4)))
    subtasks = subtasks[:cap]
    if len(subtasks) < 2:
        return None, 0
    print("  squad · split into " + str(len(subtasks)) + ": "
          + ", ".join(f"{SQUAD[s.role][0]}({s.size})" for s in subtasks), flush=True)
    if audit is not None:
        audit.record("delegation", ticket_id=req.ticket.id, subtasks=len(subtasks),
                     roles=[s.role for s in subtasks])

    cost, turns, tools, summaries, ok = p_cost, p_turns, list(p_tools), [], True
    for i, st in enumerate(subtasks, 1):
        print(f"  engineer {i}/{len(subtasks)} · {SQUAD[st.role][0]} (effort {st.effort()}) — {st.title}",
              flush=True)
        run, mreason = await _soldier(st, req, app, cfg, i, len(subtasks), iteration=req.iteration)
        cost += run.cost_usd
        turns += run.num_turns
        tools += run.tools
        summaries.append(f"[{SQUAD[st.role][0]} · {st.size}] {st.title}\n{(run.final or '').strip()}")
        if run.is_error:
            ok = False
        if audit is not None:
            audit.record("soldier_build", ticket_id=req.ticket.id, role=st.role, size=st.size,
                         effort=st.effort(), ok=not run.is_error, cost_usd=run.cost_usd,
                         turns=run.num_turns, tools=run.tools, mreason=mreason)

    summary = (f"Squad delegation — {len(subtasks)} subtasks dispatched:\n\n" + "\n\n".join(summaries))
    return BuildResult(ok=ok, summary=summary, cost_usd=cost, num_turns=turns,
                       raw=summary, tools=tools), len(subtasks)
