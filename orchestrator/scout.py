"""Scout (S-2) — the Elite Unit's reconnaissance / QA officer.

After work lands on DEV, the Scout smoke-tests the *running* app the way a user would —
key flows and accessibility — catching the runtime/UX regressions that diff-level review
(Inspector) and unit tests miss. It is an independent verifier: it RUNS the app's e2e /
Playwright suite (or a focused smoke) and reports PASS or concrete defects. It never changes
application source — it verifies, it does not build.

  general scout automatixy                  # smoke-test DEV via the local dev server
  general scout automatixy --url https://…  # smoke-test a deployed DEV URL
"""
from __future__ import annotations

from claude_agent_sdk import ClaudeAgentOptions

from .agent import run_agent
from .config import Config
from .filing import TICKET_BLOCK_RULE

SCOUT_SYSTEM = """\
You are the Scout (S-2) — the Elite Unit's reconnaissance / QA officer, reporting to THE
GENERAL. Disciplined, concise, evidence-driven. Your lens is what actually breaks in the
RUNNING app on DEV: critical user flows and accessibility — the defects that unit tests and
diff review miss.

Doctrine:
- You VERIFY; you do not build. Never modify application source.
- Recon the most recent changes first (inspect the base branch's latest commits/diff) and
  concentrate your smoke test there.
- If the app has an e2e / Playwright suite, run it — memory-safe (cap workers; never the full
  suite at default concurrency). If it also has an axe-core / a11y check, run that on the
  changed screens.
- If there is NO browser/e2e coverage, do not fabricate a pass: report that the live-QA blind
  spot exists and recommend the single smallest smoke test worth standing up first.
- Report in disciplined tone: a clear PASS/FAIL verdict, then each defect with where it is,
  what you saw vs. expected, and how to reproduce. Keep it tight."""


def _prompt(app, url: str | None) -> str:
    target = f"the deployed DEV URL: {url}" if url else "the app's local dev server on DEV"
    return "\n".join([
        f"Recon the app '{app.name}' on its integration branch '{app.base_branch}'.",
        f"Repo: {app.repo_path}",
        f"Target: {target}.",
        "",
        "Inspect the latest changes on the base branch, then smoke-test the running app there "
        "(key flows + accessibility). Run the e2e/Playwright suite if one exists (memory-safe), "
        "or a focused smoke if not. Then give your recon report: PASS/FAIL + concrete defects.",
    ])


async def recon(cfg: Config, app_name: str, url: str | None = None) -> str:
    app = cfg.app(app_name)
    options = ClaudeAgentOptions(
        model=cfg.reviewer_model,            # an independent verifier — use the strong model
        system_prompt=SCOUT_SYSTEM + TICKET_BLOCK_RULE,
        cwd=app.repo_path,                   # the checkout with deps installed (can run the app)
        # Unattended so it never stalls on the repo's Bash ask-gate. Still read-only: Write/Edit
        # are disallowed outright, and the repo's deny rules (rm -rf, force-push) still hold.
        permission_mode="bypassPermissions",
        allowed_tools=["Read", "Grep", "Glob", "Bash"],
        disallowed_tools=["Write", "Edit", "NotebookEdit"],   # verify, never change app source
        setting_sources=["project"],
        max_turns=30,
        effort="high",
    )
    run = await run_agent(_prompt(app, url), options, tag="scout")
    return run.final or "(Scout produced no report.)"
