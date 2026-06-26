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

    # --- squad delegation: ONE switch arms both the Dev Team Lead's build squad AND the recon
    #     officers' read-only squads (QA Engineer / Security Engineer / Release Manager each decide
    #     per-task whether to recruit engineers or run solo). See squad.py (build) and recon.py (recon). ---
    delegation_enabled: bool = False        # OFF by default — flip true to arm all squad delegation
    delegation_min_ac: int = 3              # Dev Team Lead: delegate if >= this many AC (or size L/XL)
    delegation_max_soldiers: int = 4        # cap engineers per ticket (build) / per inspection (recon)

    # --- Product Manager officer: when the Builder halts on a product/IA blocker, consult the PM first
    #     — it either DECIDES (the build resumes with its decision) or ESCALATES one recommendation to
    #     the Commander (parked with a clear comment; the unit moves to the next ticket). ---
    pm_enabled: bool = True                 # False = halts go straight to the Commander (old behaviour)

    # --- out-of-scope build findings (EU-42) ---
    # When the Builder surfaces a finding outside the current ticket's scope, file it into the backlog
    # instead of losing it (see filing.py). Default False = PROPOSE-FIRST: the finding is proposed (a
    # comment / de-duped suggestion) but a backlog ticket is only opened when this is flipped on.
    out_of_scope_autofile: bool = False

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
