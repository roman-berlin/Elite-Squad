"""Configuration: YAML for behaviour, environment for secrets.

The pipeline is the PM for *any* app you develop. Each app is its own repo with
its own dev branch, tests, and (optionally) Jira project. `main` is never touched
by the pipeline — you merge that yourself after QA in dev.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field, fields as _dc_fields
from pathlib import Path
from typing import Any, Optional


def _known_only(cls, data: dict, *, where: str) -> dict:
    """Drop YAML keys the dataclass no longer declares, warning loudly for each.

    A dataclass ``__init__`` raises ``TypeError`` on an unexpected keyword, so passing a raw
    config dict straight in means ANY retired field still present in a deployed config.yaml
    (the Mac/VPS files are hand-maintained and self-update from main) would BRICK the process at
    startup. The Phase-2 §2 collapse retires several fields (autonomy_*, smalltalk_prob,
    prebuild_gate_enabled, liaison_*), so unknown keys are dropped-with-a-warning instead — a
    typo stays visible in the log, but a since-removed key never stops the unit from booting."""
    known = {f.name for f in _dc_fields(cls)}
    if not isinstance(data, dict):
        return {}
    unknown = [k for k in data if k not in known]
    for k in unknown:
        print(f"  ⚠ config: ignoring unknown {where} key '{k}' (retired or misspelled)", flush=True)
    return {k: v for k, v in data.items() if k in known}

# Re-export the officer name map so the rest of the unit can import the single source of truth
# straight from config (the hub everything already imports). Defined in officers.py.
from .officers import OFFICER_NAMES, display as officer_display  # noqa: F401

# --------------------------------------------------------------------------- #
# Effort (model reasoning depth) — single source of truth.
# The SDK's EffortLevel = Literal["low","medium","high","xhigh","max"].
#   xhigh = extended reasoning, Opus-only (falls back to "high" on other models).
#   max   = maximum, model-agnostic — the top tier the Builder sizes/escalates to.
# We size + escalate on the model-agnostic ladder; xhigh is honoured if explicitly
# chosen (config/label) but never auto-picked, since the Builder runs on Sonnet.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")   # every value the SDK accepts
EFFORT_LADDER = ["low", "medium", "high", "max"]            # sizing + retry-escalation ladder
_EFFORT_ALIASES = {
    "min": "low", "lo": "low", "l": "low",
    "med": "medium", "mid": "medium", "m": "medium", "normal": "medium", "default": "medium",
    "hi": "high", "h": "high",
    "ultra": "xhigh", "ultracode": "xhigh", "ultrathink": "xhigh", "extended": "xhigh",
    "vhigh": "xhigh", "veryhigh": "xhigh", "xh": "xhigh",
    "maximum": "max", "ultramax": "max", "top": "max",
}


def normalize_effort(value: Any, default: str = "high") -> str:
    """Map any human spelling of an effort level to a valid SDK EffortLevel.
    e.g. 'ultra'/'ultracode' -> 'xhigh', 'maximum' -> 'max'. Unknown -> default."""
    if value is None:
        return default
    v = str(value).strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    if v in EFFORT_LEVELS:
        return v
    return _EFFORT_ALIASES.get(v, default)


def effort_step_index(effort: str) -> int:
    """Index of an effort on the escalation ladder. xhigh sits with 'high' (it falls
    back to high off-Opus), so a retry from xhigh climbs toward max."""
    if effort == "xhigh":
        return EFFORT_LADDER.index("high")
    return EFFORT_LADDER.index(effort) if effort in EFFORT_LADDER else EFFORT_LADDER.index("high")


@dataclass
class AppConfig:
    name: str
    repo_path: str
    base_branch: str = "dev"            # features branch off (and merge back to) dev
    protected_branch: str = "main"      # NEVER touched by the pipeline
    branch_prefix: str = "autodev"
    qa_url: Optional[str] = None         # base URL where this app's DEV is testable; shown on merge -> QA
    gate_commands: list[str] = field(default_factory=list)   # tests/lint/typecheck (pre-review gate; repo-wide default)
    # Per-app gate commands for a MONOREPO, keyed by the directory name under apps/ or packages/
    # (e.g. "landing-page", "microsite", "zeltivo-crm"). When a ticket's diff touches one of these
    # components, ONLY the gates for the touched components (plus any apps a changed shared package
    # links to, see gate_shared_packages) run — so an AUTO-9-style landing-page/microsite ticket is
    # typechecked on landing-page + microsite and is NOT blocked by an unrelated zeltivo-crm error.
    # Falls back to the repo-wide `gate_commands` when detection is ambiguous (nothing under
    # apps/ or packages/ changed, or no touched component has a per-app entry). (EU-19)
    gate_commands_by_app: dict[str, list[str]] = field(default_factory=dict)
    # Shared-package -> dependent apps. When a changed path lands under packages/<pkg>/, also run the
    # gates for the apps listed here (they depend on it), so a shared dependency correctly re-gates its
    # consumers. e.g. {"ui": ["zeltivo-crm", "landing-page"]}. (EU-19)
    gate_shared_packages: dict[str, list[str]] = field(default_factory=dict)
    # EU-249: per-app opt-in for the diff-scoped test-collectability + test-run gate (see
    # gate.test_collectability_gate). For every ADDED/RENAMED *.test.*/*.spec.* file in a diff it
    # flags a phantom duplicated-app-root path (apps/<x>/**/apps/<x>/) or one matching none of its
    # owning app's vitest `include` globs, then runs the survivors with a scoped `bunx vitest run`
    # (immune to pre-existing full-suite failures elsewhere in the app). False by default so it
    # never fires for an app with no monorepo/vitest shape (e.g. the EU python gate).
    test_collectability_enabled: bool = False
    gate_timeout_sec: int = 1800
    gate_env: dict[str, str] = field(default_factory=dict)    # extra env for gate cmds (e.g. NODE_OPTIONS, worker caps)
    # Phase-2 §3.3: fast lint/format commands run as a deterministic gate AFTER the test gate and
    # BEFORE any LLM reviewer (e.g. ["ruff check orchestrator/"], ["bun run lint"]). Failures feed
    # the Builder as plain text. Empty = no lint gate for this app.
    lint_commands: list[str] = field(default_factory=list)
    # EU-54 health check: modules the gate's python interpreter MUST be able to import. Checked once
    # before the suite runs (and by the doctor); a missing one fails the gate fast with a clear venv
    # hint instead of a cryptic mid-suite `ModuleNotFoundError`. Empty = no check (e.g. a Bun app).
    gate_preflight: list[str] = field(default_factory=list)
    # EU-54: per-app override of the unit-wide Config.worktree_setup_cmd. Runs ONCE on worktree
    # creation. None = inherit the unit-wide default; "" = explicitly run nothing for this app. Lets the
    # Bun product install deps while the Python EU repo (no package.json) installs nothing.
    worktree_setup_cmd: Optional[str] = None
    postmerge_commands: list[str] = field(default_factory=list)  # SRE's heavier post-merge suite (e2e/integration); empty = skip
    # EU-60: a single fast post-merge SMOKE command run on the landed <base> (e.g. a Playwright
    # auth-redirect smoke). Unlike postmerge_commands it never reverts — on red it FLAGS the merge
    # (Telegram + audit + ticket comment) so the Commander catches a broken DEV at QA. None = skip
    # (the framework stays inert until an app opts a command in). See smoke.py.
    smoke_command: Optional[str] = None
    backlog_backend: str = "jira"       # "jira" | "notion" | "none"
    backlog: dict[str, Any] = field(default_factory=dict)
    # runtime-resolved working dir for officers + gate (the worktree in isolated mode).
    # None -> use repo_path. Set automatically at run start; not normally in YAML.
    workdir: Optional[str] = None

    def validate(self) -> None:
        repo = Path(self.repo_path).expanduser().resolve()
        if not (repo / ".git").exists():
            raise ValueError(f"[{self.name}] repo_path is not a git repo: {repo}")
        if self.base_branch == self.protected_branch:
            raise ValueError(f"[{self.name}] base_branch must differ from protected_branch "
                             f"('{self.protected_branch}'); the pipeline must never build on main")
        if self.backlog_backend not in ("jira", "notion", "none"):
            raise ValueError(f"[{self.name}] unknown backlog_backend: {self.backlog_backend}")


@dataclass
class Config:
    apps: list[AppConfig]

    # --- models (default: all-Opus across the unit) ---
    # builder_model drives the Builder + the council round-table; reviewer_model drives the
    # Reviewer + every verifier/staff officer (QA Engineer, Security Engineer, Release Manager,
    # Engineering Manager, Engineering Coach, the CTO/chair). Both default to Opus. On Opus, the "xhigh"/ultra effort
    # tier is real (it falls back to high only on non-Opus models).
    builder_model: str = "claude-opus-4-8"
    reviewer_model: str = "claude-opus-4-8"

    # --- EU-108/118: multi-provider fallback for plan-limit handling ---
    # When a provider (e.g., Claude Max plan) hits its limit, autopilot can switch to another
    # provider/model that still has capacity (utilization < 1.0). This provides graceful degradation.
    # Empty means disabled; otherwise a list of (model, provider_id) tuples in priority order.
    fallback_providers: list[tuple[str, str]] = field(default_factory=list)

    # The server's MEETINGS and CHAT don't need Opus — only implementation (Builder/Reviewer, which
    # run on the Mac) does. Officer discussions run on Sonnet and corridor small-talk on Haiku, so the
    # always-on box stays light against the Max limit and never competes with your own Opus coding.
    discussion_model: str = "claude-sonnet-5"              # council / stand-up / meetings / group / General chat (upgraded 2026-07-05)
    smalltalk_model: str = "claude-haiku-4-5-20251001"     # corridor small-talk — cheapest

    # --- EU-189: model backend (which provider the officers execute against) ---
    # 'opus' = Anthropic/Claude (default; the Max subscription). 'glm' = Z.ai GLM via the
    # Anthropic-compatible endpoint (needs GLM_AUTH_TOKEN in env; see orchestrator/backends.py).
    # Chosen per-run in the cockpit: the run route sets it on the per-run rcfg (like builder_effort)
    # and loop.run pins it for every officer SDK call. Declared here so _known_only keeps a
    # config.yaml `model_backend:` key (a fleet default) instead of silently dropping it.
    # GOVERNANCE (Phase 1, per the EU-189 security review):
    #   • Setting this to 'glm' in config.yaml makes it the fleet default for ALL ticket/task/bug
    #     runs INCLUDING unattended autopilot + decision-resume — i.e. those runs egress prompts
    #     (code, tickets, diffs) to Z.ai, a third-party sub-processor. The interactive cockpit picker
    #     is the explicit, gated path; a YAML default is a standing opt-in. Meetings/ceremonies
    #     (standup/council/etc.) currently stay on Opus regardless.
    #   • The fleet dollar cap (max_cost_usd) does NOT price GLM (glm-4.6 isn't in the SDK's
    #     Anthropic pricing table → ~$0), so it does not bound GLM runs; per_ticket_token_budget /
    #     per_ticket_time_budget_min still do. Per-provider GLM pricing + an automation opt-in are
    #     tracked for Phase 2.
    model_backend: str = "opus"

    # --- effort (thinking depth): low | medium | high | xhigh | max  (xhigh = Opus-only "ultra") ---
    builder_effort: str = "high"            # default / fallback base when sizing is off
    reviewer_effort: str = "high"
    builder_max_turns: int = 60             # base build turn budget; high/max effort scale it up (see builder.turns_for)
    adaptive_effort: bool = True            # size the Builder's effort from the ticket (XS->low … XL->max)
    escalate_effort_on_retry: bool = False  # OFF by default (2026-07-05 audit): 135/135 round-≥2 reviewer
                                            # objections were textually NEW, so bumping effort on retry (and the
                                            # turn budget with it — turns_for scales off effort) bought context
                                            # bloat, not convergence. Retry keeps pass-1 effort/turns; the
                                            # cheap-first MODEL ladder (Sonnet→Opus, models.for_builder) still
                                            # escalates on retry — that one is intended.
    auto_model: bool = True                 # ON by default: cheapest model that fits each task, escalating to the
                                            # ceiling on retry (<=ceiling, Sonnet floor for code). Fleet-wide econ;
                                            # set false to pin every officer to its configured model. See models.py.
    sentinel_enabled: bool = True           # ARMED by default: the SRE runs an app's postmerge_commands after a
                                            # land and auto-reverts (forward-only) if red. Still a NO-OP for any app
                                            # without a `postmerge_commands:` suite (see sentinel.should_run), so
                                            # arming the framework here costs nothing until an app opts in a suite.
    smoke_enabled: bool = True              # EU-60 ARMED by default: after a land, run an app's single `smoke_command`
                                            # (a fast post-merge canary, e.g. a Playwright auth-redirect smoke) and
                                            # FLAG it (Telegram + audit + ticket comment) if red — never reverts, that's
                                            # the SRE's job. NO-OP for any app without a `smoke_command` (smoke.should_run),
                                            # so arming it costs nothing until an app opts a command in.
    auto_mode: bool = False                 # officers never park for your approval — the PM decides + the unit keeps building (you review/reverse after)
    readiness_gate: bool = False            # hand back an under-specified ticket (no AC + thin desc) BEFORE building — see readiness.py
    readiness_min_desc: int = 80            # a description shorter than this (and not just the title) counts as "thin"
    postmortem_after: int = 3               # auto-write a post-mortem once a ticket has failed this many times (0 = off); see forensics.py

    # --- EU-38 builder context budget: INPUT tokens (not the model tier) drive cost. The prior_issues
    #     fed back on retry grows every pass and is the biggest contributor, so it's bounded here; the
    #     unit-memory preamble concatenated into the system prompt is bounded too. See builder._cap_feedback. ---
    builder_feedback_max_items: int = 12    # keep at most this many prior-issue lines on retry (NEWEST kept)
    builder_feedback_max_chars: int = 6000  # ...and at most this many total chars of feedback
    builder_preamble_max_chars: int = 4000  # bound the unit-memory preamble fed into the builder system prompt

    # --- recon delegation: arms the recon officers' read-only squads. The QA / Security / Release
    #     officers each decide per-task whether to recruit engineers or run solo. The Dev Team Lead's
    #     build-delegation squad was removed (Phase-2 §2); the Builder builds solo. See recon.py. ---
    delegation_enabled: bool = False        # OFF by default — flip true to arm recon delegation
    delegation_max_soldiers: int = 4        # cap engineers per recon inspection

    # --- Architect officer: produces lightweight ADRs for feature/large tickets before the build
    #     — decides whether to produce an ADR (feature/large) or skip (bug/small), and triggers
    #     Scrum Master split when the design exceeds thresholds. ---
    architect_enabled: bool = False         # ARMED: Architect runs before build for feature/large tickets

    # --- Planner (Phase-2 §2 centerpiece): ONE Opus design call/ticket before the build that
    #     absorbs the Architect ADR + squad-lead planning + Scrum split decision. Produces the
    #     design brief + TESTABLE acceptance criteria (the Builder writes tests against them) +
    #     in-scope file list. When on, it runs INSTEAD of the Architect. OFF by default — arm it
    #     once proven live, which then unlocks retiring the separate Test Engineer coverage pass. ---
    planner_enabled: bool = False

    # --- Senior PM pre-build triage gate (EU-107): DELETED in Phase-2 §2 (2026-07-06). Its
    #     ANSWER/CLOSE/REFILE verdicts fold into the Planner's single per-ticket decision, with
    #     the EU-134 conservative overrides (AC / [Feature] / [Bug] ⇒ always build) kept as
    #     deterministic pre-checks there. (The flag was off since 2026-06-29 — the gate closed
    #     [Feature] tickets as "answered" — and the field was accidentally declared twice.) ---

    # --- Product Manager officer: when the Builder halts on a product/IA blocker, consult the PM first
    #     — it either DECIDES (the build resumes with its decision) or ESCALATES one recommendation to
    #     the Commander (parked with a clear comment; the unit moves to the next ticket). ---
    pm_enabled: bool = True                 # False = halts go straight to the Commander (old behaviour)

    # --- out-of-scope build findings (EU-42 / EU-92) ---
    # When the Builder/Reviewer surfaces a finding outside the current ticket's scope, file it into the
    # backlog instead of losing it (see filing.py). EU-92 — the PM owns out-of-scope triage, so the
    # default is AUTO-FILE: worthwhile findings land as de-duped 'out-of-scope'-labeled backlog tickets
    # and the Commander is never paged. Flip to False to PROPOSE-FIRST instead (surface the proposal in
    # 'Needs you' for a manual wave-through); PM-classified out-of-scope findings auto-file either way.
    out_of_scope_autofile: bool = True

    # --- council / meetings ---
    council_rounds: int = 2                 # discussion rounds (1 = report-only; 2+ = officers debate)

    # --- usage governor (server frugality) ---
    usage_cap_per_hour: int = 40            # cap discretionary officer-discussion calls / rolling hour; 0 = off
    daily_token_budget: int = 100_000_000   # tokens/day ceiling; autopilot AUTO-PAUSES new tickets when today's
                                            # ledger burn (input+output, incl. cache reads) hits this. ARMED by
                                            # default as a runaway-loop guard sized to Max-plan headroom — tune in
                                            # config.yaml to your own headroom (0 = off; a heavy legit day may pause
                                            # until midnight). Resumes after midnight or when the ceiling is raised.
    budget_alert_pct: float = 0.8           # Telegram heads-up once today's burn crosses this fraction of the ceiling
    budget_bad_threshold: float = 0.95      # Low-watermark: stop starting new tickets when any provider is at this utilization
    glm_quota_tokens: int = 100_000_000     # GLM (Z.ai) token quota ceiling; 0 = disabled

    # --- EU-122: dual-provider budget monitor (Claude + GLM) ---
    # GLM daily token ceiling (default: 1B tokens). GLM doesn't have a Max-like subscription,
    # so we estimate from ledger using this ceiling. Set based on your GLM API plan.
    glm_daily_token_budget: int = 1_000_000_000
    # Claude low-watermark: stop picking tickets when remaining falls below this.
    # Can be specified as tokens (absolute) or percentage (of daily_token_budget).
    # If both are set, tokens takes precedence. Default: ~5% or 100k tokens, whichever is larger.
    claude_low_watermark_tokens: Optional[int] = None      # e.g. 100_000 for ~1 ticket
    claude_low_watermark_pct: Optional[float] = None         # e.g. 0.05 for 5%
    # GLM low-watermark: same semantics as Claude, but for GLM.
    glm_low_watermark_tokens: Optional[int] = None          # e.g. 100_000 for ~1 ticket
    glm_low_watermark_pct: Optional[float] = None           # e.g. 0.05 for 5%

    # --- autonomy layer (events.py auto-convene): DELETED in Phase-2 §2 (2026-07-06). The
    #     event reactor auto-convened meetings/small-talk between autopilot cycles (<3% of tokens
    #     but ~100% of the org-chart noise, per the audit). Ceremonies are now ON-DEMAND ONLY
    #     (CLI / cockpit / Telegram); the 6 autonomy flags went with it. ---
    meeting_autospawn: bool = False          # a meeting may FILE the tickets it proposes (de-duped); drills/hires stay proposal-only
    scout_after_merge: bool = False          # after a live merge, the QA Engineer smoke-tests DEV (extra cost; off by default)

    # --- loop bounds / cost ---
    # QW3: loop.HARD_MAX_PASSES clamps the effective value to 2 — raising this past 2 has no effect.
    max_iterations: int = 2
    # EU-216: weak (non-Opus, e.g. GLM) backends get one extra review-fix pass — HARD_MAX_PASSES is
    # calibrated for Opus; GLM needed more iterations in 3/14 2026-07-09 "max passes" escalations.
    # loop.HARD_MAX_PASSES_WEAK clamps the effective value to 3 — raising this past 3 has no effect,
    # and it only ever applies when the last review FAIL carries no blocker-severity finding.
    max_iterations_weak: int = 3
    max_cost_usd: float = 0.0           # 0 = no cap (subscription). Set a number only for API billing.
    max_tickets_per_run: int = 1        # per app, per run
    # QW4 (2026-07-05): per-ticket budgets, checked before each pass; breach → BLOCKED + Telegram.
    # 3M (Commander-approved 2026-07-05): above the healthy-merge median (~2.1M tokens, EU-119) so
    # both capped passes stay available, far below the 15.5–24.4M runaway tail that burned the
    # plan. Lower to ~400k for a deliberate one-pass ramp-up throttle. 0 disables a dimension.
    per_ticket_token_budget: int = 3_000_000  # input+output tokens across all officers of one attempt
    per_ticket_time_budget_min: int = 30      # wall-clock minutes per attempt

    # --- merge behaviour (auto-merge to dev if green) ---
    merge_to_dev: bool = True           # merge feature -> dev when review passes & dev stays green
    open_pr_on_block: bool = True       # if it can't merge safely, open a PR into dev instead
    mark_done_on_merge: bool = False    # False = leave In Progress for your manual QA on dev;
                                        # True = move the ticket to Done as soon as it merges to dev

    # --- isolation (so the CTO never fights your manual git checkout) ---
    use_worktree: bool = True           # run each app in a dedicated linked git worktree based on origin/<base>
    worktree_dir: Optional[str] = None  # parent dir for worktrees; default: <repo_parent>/.general-worktrees/<app>
    worktree_setup_cmd: Optional[str] = None  # run ONCE when a worktree is first created (e.g. "bun install")
    sync_base_after_merge: bool = True  # after a live merge, bring <base> in your main checkout up to date (QA-ready)
    # (Phase-2 §2, 2026-07-06: `security_gate` removed with the deleted LLM per-diff security gate —
    #  its secret/dep scan is now deterministic in gate.py; a stale yaml key is dropped harmlessly.)
    # Phase-2 §3.1 (the EU-174 killer, Commander-approved 2026-07-06): before the FIRST build pass,
    # run the gate against the clean base tree; a red base BLOCKS the ticket immediately (Telegram +
    # Needs-you) instead of billing up to HARD_MAX_PASSES max-effort builds for a failure that
    # predates the diff. Cached per base sha (state/red_base_cache.json) so the suite runs once per
    # base commit, not once per ticket. ARMED by default — EU-174 alone burned 15.5M tokens on this.
    red_base_check: bool = True

    # --- safety ---
    dry_run: bool = False               # default LIVE (build + merge to DEV); set dry_run: true in config.yaml for a no-changes preview (there is no --dry CLI flag)

    # --- notifications ---
    notify_verbose: bool = False        # also Telegram on implemented / verdict / pushed (not just key events)

    # --- EU-65/EU-66 inter-unit liaison channel: DELETED in Phase-2 §2 (2026-07-06). It was
    #     dead by default (master flag off + empty chat-id list = doubly inert) and §2's verdict
    #     was DELETE (zero calls ever). notify.incoming_texts now accepts the ops chat only. ---

    # --- run logs (EU-106) ---
    # log_folder: root directory for per-run log files written by run_logger.py.
    # Resolved relative to the directory that contains audit_path.
    log_folder: str = "logs/"
    # log_retention_days: auto-purge day-folders older than this many days on each run start.
    # 0 (the default) disables purging.
    log_retention_days: int = 0

    # --- EU-197: per-officer transcript (full tool inputs + reasoning) ---
    # transcript_enabled: when True, persist a full JSONL transcript per officer run
    # to logs/<app>/<date>/<TICKET>-<HHMMSS>-<officer>.jsonl with secrets redacted.
    # Best-effort (never breaks a run). Disabled by default.
    transcript_enabled: bool = False

    # --- audit ---
    # The audit log's directory is the runtime-state root: every sidecar (blocked_tickets.json,
    # pending_decisions.json, usage_ledger.jsonl, council/, report .md files, …) is derived from it
    # via Path(audit_path).with_name(). Quick Win 2 (2026-07-05): default under state/ so runtime
    # state never mingles with source. Keep OUTSIDE every target repo.
    audit_path: str = "./state/audit.jsonl"

    # EU-185 (Wave 0): single-Telegram-poller election. Telegram getUpdates+offset is
    # single-consumer, so two hosts polling one bot token split/lose the Commander's messages. Only
    # the host whose sync host id (GENERAL_HOST_ID, else hostname) matches this value runs the
    # poller; every other host runs cockpit-only. Override per-host with GENERAL_TELEGRAM_POLLER=1/0.
    telegram_poller_host: str = "server"

    @staticmethod
    def load(path: str | Path) -> "Config":
        import yaml
        data = yaml.safe_load(Path(path).read_text()) or {}
        apps = [AppConfig(**_known_only(AppConfig, a, where="apps[]"))
                for a in data.pop("apps", [])]
        cfg = Config(apps=apps, **_known_only(Config, data, where="config"))
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.apps:
            raise ValueError("config must define at least one app under 'apps:'")
        names = [a.name for a in self.apps]
        if len(names) != len(set(names)):
            raise ValueError("app names must be unique")
        for app in self.apps:
            app.validate()

    def app(self, name: str) -> AppConfig:
        for a in self.apps:
            if a.name == name:
                return a
        raise KeyError(f"no app named '{name}' in config (have: {[a.name for a in self.apps]})")

    # Auth comes from the environment / Claude Code login, never the YAML file.
    # The officers run on Claude Code, which accepts EITHER a Max/Pro subscription
    # (via `claude` /login or a `claude setup-token` OAuth token) OR an API key.
    def detected_auth(self) -> str | None:
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "ANTHROPIC_API_KEY (per-token API billing)"
        if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            return "CLAUDE_CODE_OAUTH_TOKEN (subscription / Max plan)"
        if _claude_login_present():
            return "claude login (subscription / Max plan)"
        return None


def _claude_login_present() -> bool:
    """Best-effort: is there a `claude` subscription login the agent SDK can use even
    with no auth env var? Checks the Claude Code config/credentials files and the macOS
    Keychain. Never reads a secret (so it never triggers a prompt) and never raises."""
    home = Path.home()
    # credentials file (headless / Linux installs)
    for p in (home / ".claude" / ".credentials.json",
              home / ".config" / "claude" / ".credentials.json"):
        try:
            if p.is_file() and p.stat().st_size > 0:
                return True
        except OSError:
            pass
    # Claude Code's config records the logged-in account
    try:
        cj = home / ".claude.json"
        if cj.is_file() and "oauthAccount" in cj.read_text(encoding="utf-8", errors="ignore"):
            return True
    except OSError:
        pass
    # macOS keeps the OAuth credentials in the login Keychain
    if sys.platform == "darwin":
        for service in ("Claude Code-credentials", "Claude Code"):
            try:
                r = subprocess.run(["security", "find-generic-password", "-s", service],
                                   capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    return True
            except Exception:  # noqa: BLE001 - detection must never break doctor
                pass
    return False
