"""Configuration: YAML for behaviour, environment for secrets.

The pipeline is the PM for *any* app you develop. Each app is its own repo with
its own dev branch, tests, and (optionally) Jira project. `main` is never touched
by the pipeline — you merge that yourself after QA in dev.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

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


def parse_external_chat_ids(raw: Any) -> list[str]:
    """Parse the EU-65 liaison external chat ids from an env value (or any string).

    Accepts a comma / whitespace / newline-separated list (e.g.
    ``TELEGRAM_EXTERNAL_CHAT_IDS="-100123, -100456"``) and returns a de-duped,
    order-preserving list of non-empty chat-id strings. None / blank -> ``[]`` so
    that with nothing configured the liaison channel stays empty and inert. These
    are deliberately kept SEPARATE from the ops ``TELEGRAM_CHAT_ID`` (notify.py):
    the liaison channel is an isolated OUTWARD chat to an allied unit and must
    never reuse the ops chat id.
    """
    if not raw:
        return []
    seen: dict[str, None] = {}
    for tok in str(raw).replace(",", " ").split():
        tok = tok.strip()
        if tok and tok not in seen:
            seen[tok] = None
    return list(seen)


def _env_external_chat_ids() -> list[str]:
    """Default factory: the liaison external chat ids parsed from the environment."""
    return parse_external_chat_ids(os.environ.get("TELEGRAM_EXTERNAL_CHAT_IDS"))


def parse_mention_handles(raw: Any) -> list[str]:
    """Parse the bot @handles that count as an @mention on the liaison channel.

    Same comma/whitespace/newline tokenising as the chat ids, but each token is
    normalised to a bare, lower-case handle (leading ``@`` stripped) so ``@AlliedBot``
    in a message matches the configured ``AlliedBot``. Empty -> ``[]`` which means the
    liaison NEVER auto-replies (it only ever speaks when explicitly addressed)."""
    return [tok.lstrip("@").lower() for tok in parse_external_chat_ids(raw)]


def _env_bot_handles() -> list[str]:
    """Default factory: the liaison mention handles parsed from TELEGRAM_BOT_USERNAME."""
    return parse_mention_handles(os.environ.get("TELEGRAM_BOT_USERNAME"))


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
    gate_timeout_sec: int = 1800
    gate_env: dict[str, str] = field(default_factory=dict)    # extra env for gate cmds (e.g. NODE_OPTIONS, worker caps)
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
    discussion_model: str = "claude-sonnet-4-6"            # council / stand-up / meetings / group / General chat
    smalltalk_model: str = "claude-haiku-4-5-20251001"     # corridor small-talk — cheapest

    # --- effort (thinking depth): low | medium | high | xhigh | max  (xhigh = Opus-only "ultra") ---
    builder_effort: str = "high"            # default / fallback base when sizing is off
    reviewer_effort: str = "high"
    builder_max_turns: int = 60             # base build turn budget; high/max effort scale it up (see builder.turns_for)
    adaptive_effort: bool = True            # size the Builder's effort from the ticket (XS->low … XL->max)
    escalate_effort_on_retry: bool = True   # bump the Builder's effort when a pass is rejected
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
    prebuild_gate_enabled: bool = False     # EU-134: ARMED by default (when False, gate is skipped; flip True only after conservative logic is verified)
    readiness_min_desc: int = 80            # a description shorter than this (and not just the title) counts as "thin"
    postmortem_after: int = 3               # auto-write a post-mortem once a ticket has failed this many times (0 = off); see forensics.py

    # --- EU-38 builder context budget: INPUT tokens (not the model tier) drive cost. The prior_issues
    #     fed back on retry grows every pass and is the biggest contributor, so it's bounded here; the
    #     unit-memory preamble concatenated into the system prompt is bounded too. See builder._cap_feedback. ---
    builder_feedback_max_items: int = 12    # keep at most this many prior-issue lines on retry (NEWEST kept)
    builder_feedback_max_chars: int = 6000  # ...and at most this many total chars of feedback
    builder_preamble_max_chars: int = 4000  # bound the unit-memory preamble fed into the builder system prompt

    # --- squad delegation: ONE switch arms both the Dev Team Lead's build squad AND the recon
    #     officers' read-only squads (QA Engineer / Security Engineer / Release Manager each decide
    #     per-task whether to recruit engineers or run solo). See squad.py (build) and recon.py (recon). ---
    delegation_enabled: bool = False        # OFF by default — flip true to arm all squad delegation
    delegation_min_ac: int = 3              # Dev Team Lead: delegate if >= this many AC (or size L/XL)
    delegation_max_soldiers: int = 4        # cap engineers per ticket (build) / per inspection (recon)

    # --- Architect officer: produces lightweight ADRs for feature/large tickets before the build
    #     — decides whether to produce an ADR (feature/large) or skip (bug/small), and triggers
    #     Scrum Master split when the design exceeds thresholds. ---
    architect_enabled: bool = False         # ARMED: Architect runs before build for feature/large tickets

    # --- Senior PM pre-build triage gate (EU-107). OFF by default (2026-06-29): the gate was over-eager
    #     — it closed [Feature] tickets (EU-118/EU-120) as "answered" instead of building them. Re-enable
    #     only once the triage is conservative (CONTINUE by default; CLOSE only exact dupes; never an
    #     acceptance-criteria ticket) — see the prebuild-gate best-practice fix ticket. ---
    prebuild_gate_enabled: bool = False

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

    # --- autonomy (officers convene themselves between autopilot cycles) ---
    autonomy_enabled: bool = True
    autonomy_cooldown_min: int = 45         # min minutes between auto-convened sessions (anti-spam)
    meeting_on_security_block: bool = True   # a security block -> Security Engineer + Dev Team Lead + Code Reviewer huddle
    parks_meeting_threshold: int = 3         # this many parked tickets -> a "why are we stuck" meeting
    smalltalk_prob: float = 0.15             # chance of corridor small-talk on a quiet cycle
    random_meeting_prob: float = 0.06        # chance of a spontaneous meeting on a quiet cycle
    meeting_autospawn: bool = False          # a meeting may FILE the tickets it proposes (de-duped); drills/hires stay proposal-only
    scout_after_merge: bool = False          # after a live merge, the QA Engineer smoke-tests DEV (extra cost; off by default)

    # --- loop bounds / cost ---
    max_iterations: int = 4
    max_cost_usd: float = 0.0           # 0 = no cap (subscription). Set a number only for API billing.
    max_tickets_per_run: int = 1        # per app, per run

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
    security_gate: bool = False         # the Security Engineer reviews each diff before merge; a CRITICAL/HIGH finding opens a PR instead of landing
    test_gate: bool = True              # ARMED: Test Engineer runs after build, before review — adds happy-path + regression tests and owns the PR coverage artifact
    test_engineer_effort: str = "medium"  # thinking depth for the Test Engineer's coverage pass

    # --- safety ---
    dry_run: bool = False               # default LIVE (build + merge to DEV); pass --dry for a no-changes preview

    # --- notifications ---
    notify_verbose: bool = False        # also Telegram on implemented / verdict / pushed (not just key events)

    # --- EU-65 inter-unit liaison channel (additive foundation; OFF + empty by default) ---
    # An isolated OUTWARD Telegram chat to an ALLIED unit, kept strictly separate from the ops
    # TELEGRAM_CHAT_ID (notify.py). With nothing configured every value below is empty / disabled,
    # so behaviour is byte-identical to today — the channel stays inert until the Commander opts in.
    # Liaison replies run on the CHEAP model under a per-day TOKEN cap so an outward chat can never
    # compete with the unit's own coding spend. Other EU-65 slices import these.
    liaison_enabled: bool = False                  # master flag; even when on, a no-op unless chat ids are set
    # External / social chat ids the liaison may talk to. Parsed from TELEGRAM_EXTERNAL_CHAT_IDS by
    # default (comma/space/newline separated); may also be set explicitly in config.yaml. Empty => inert.
    liaison_external_chat_ids: list[str] = field(default_factory=_env_external_chat_ids)
    liaison_model: str = "claude-haiku-4-5-20251001"   # cheap model for liaison replies (never Opus/Sonnet)
    liaison_effort: str = "low"                    # minimal reasoning depth for an outward small-talk reply
    liaison_daily_token_budget: int = 200_000      # per-day token ceiling for liaison replies; 0 = off (no cap)
    liaison_max_reply_chars: int = 800             # bound a single outward reply (cost + don't over-share)
    # Bot @handles that count as being addressed. The liaison replies ONLY when @mentioned, so with
    # no handle configured it never speaks (safe default). Parsed from TELEGRAM_BOT_USERNAME by default.
    liaison_mention_handles: list[str] = field(default_factory=_env_bot_handles)

    def liaison_active(self) -> bool:
        """True only when the liaison channel is BOTH flagged on AND has at least one external chat
        id configured. Every other slice gates on this so an unconfigured unit behaves exactly as today."""
        return bool(self.liaison_enabled and self.liaison_external_chat_ids)

    def is_liaison_chat(self, chat_id: Any) -> bool:
        """True if ``chat_id`` is one of the configured external liaison chats. Compared as strings so
        an int env/update id and a YAML string id match. Always False for the ops TELEGRAM_CHAT_ID
        unless it was (mistakenly) also listed — callers keep the two channels isolated."""
        if chat_id is None:
            return False
        return str(chat_id) in set(self.liaison_external_chat_ids)

    # --- run logs (EU-106) ---
    # log_folder: root directory for per-run log files written by run_logger.py.
    # Resolved relative to the directory that contains audit_path.
    log_folder: str = "logs/"
    # log_retention_days: auto-purge day-folders older than this many days on each run start.
    # 0 (the default) disables purging.
    log_retention_days: int = 0

    # --- audit ---
    audit_path: str = "./audit.jsonl"   # keep OUTSIDE every target repo

    @staticmethod
    def load(path: str | Path) -> "Config":
        import yaml
        data = yaml.safe_load(Path(path).read_text()) or {}
        apps = [AppConfig(**a) for a in data.pop("apps", [])]
        cfg = Config(apps=apps, **data)
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
