# The General — an unattended dev unit for your SaaS

You give an order. The **General** (the orchestrator) commands two officers — the
**Builder** (Claude Code) and the **Reviewer** (a second, read-only Claude) — to
implement it on a feature branch, test it, review it, and land it on your app's
`dev` branch only if `dev` stays green. You remain the only one who merges
`dev → main`, after your QA.

It works across every app in your unit: automatixy, signaldesk, superadmin —
each its own repo, branch, tests, and (optional) Jira project.

See `ARCHITECTURE.md` for the design. This file is how to run it.

## Give an order — three ways

```bash
# 1) A bug or feature in plain words (no Jira needed) — your scrollbar example:
./general task automatixy "Fix missing scrollbar on the dashboard table" \
    --ac "Scrollbar appears when content overflows" \
    --ac "No regression on window resize"

# 2) An existing Jira ticket, or several:
./general ticket automatixy AUTO-123 AUTO-130

# 3) Drain the backlog — every ticket labelled `autodev`:
./general drain automatixy          # one app
./general drain                     # every app that has a tracker
```

Everything runs **dry-run by default**: the General executes the whole loop —
including a *trial* merge into dev to preview whether dev would stay green — but
pushes nothing, merges nothing, and writes nothing to Jira. Add `--live` when ready:

```bash
./general --live task automatixy "Fix missing scrollbar on the dashboard table" --ac "..."
```

## What happens on each order

For each ticket the General:

1. cuts a feature branch off `dev` (never `main`),
2. orders the **Builder** to implement it,
3. runs that app's `gate_commands` (your tests/lint) on the branch — cheap filter,
4. hands the **diff** to the **Reviewer**, which judges spec conformance *and*
   quality and returns a strict verdict (its required-changes become the Builder's
   next prompt — the loop closes with no copy-paste from you),
5. on PASS: merges the branch into `dev`, re-runs the gate **on dev**, and
   - pushes dev if it's green  →  ticket moves to *In Review* (your QA queue), or
   - reverts the merge and opens a **PR into dev** if it would break or conflict.
6. on repeated FAIL: stops after `max_iterations` and escalates the ticket to you.

The General never touches `main`. dev is your QA buffer; you merge to main.

## Setup (10 minutes, once)

```bash
cd claude-pipeline
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
chmod +x general

cp .env.example .env                 # fill in ANTHROPIC_API_KEY and JIRA_* ; then: source .env
cp config.example.yaml config.yaml   # add your apps (repo_path, dev branch, gate_commands, Jira)

./general doctor                     # verifies config, keys, repos, branches, tooling
```

`doctor` tells you exactly what's missing before you run anything real.

### Auth — using your Max plan (no API key)

The officers run on Claude Code, so they use the same login. On a **Max/Pro plan**:

```bash
claude            # then: /login  — sign in with your Max account (one time, in a browser)
```

That's it — the General now runs on your subscription, no `ANTHROPIC_API_KEY`, usage
counts against your plan. For headless/cron runs, generate a 1-year token instead:

```bash
claude setup-token                       # prints sk-ant-oat01-...
export CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-..."
```

Only set `ANTHROPIC_API_KEY` if you deliberately want per-token API billing — if it's
set, it overrides your subscription. (Note: from **June 15, 2026**, Agent-SDK/`claude -p`
usage on subscriptions draws from a separate monthly Agent SDK credit pool.) Because
subscription runs don't produce a per-token invoice, the `max_cost_usd` budget may read
~$0 — so `max_tickets_per_run` and `max_iterations` are your real guardrails there.

### Adding a Jira project

Per app in `config.yaml`:

```yaml
backlog_backend: jira
backlog:
  base_url: https://YOURTEAM.atlassian.net
  project_key: AUTO
  label: autodev                 # the General only picks up tickets with this label
  status_map: { In Progress: "In Progress", In Review: "In Review", Needs Human: "Needs Triage" }
```

Plus a token in `.env` (never in the config):

```bash
export JIRA_EMAIL="you@yourteam.com"
export JIRA_API_TOKEN="..."        # id.atlassian.com → Security → API tokens
```

Then label a ticket `autodev` and `./general drain automatixy` works it; or run a
specific one with `./general ticket automatixy AUTO-123`. Give each ticket clear
**acceptance criteria** (a custom field, or an "Acceptance Criteria" section in the
description) — the Reviewer judges against exactly those.

## Recommended workflow for building your SaaS

1. `./general task <app> "..." --ac "..."` in **dry-run**; read the summary + `audit.jsonl`.
2. When the trial says *would merge to dev (dev stays green)*, re-run with `--live`.
3. Do your QA on `dev` (with Cowork — k6, e2e, a visual check of that scrollbar).
4. You merge `dev → main`.

Scale up by raising `max_tickets_per_run` and using `drain` once you trust it.

### Run it on a schedule (optional)

```cron
# weekdays 07:00 — drain up to 3 autodev tickets per app, live
0 7 * * 1-5  cd /path/to/claude-pipeline && ./general --live --max-tickets 3 drain >> run.log 2>&1
```

## Model backend — Opus (Claude) or GLM (Z.ai)

By default every run uses **Opus** (your Claude Max subscription). You can point the unit at
**GLM** (Z.ai's Anthropic-compatible endpoint) instead — a per-machine choice, not a code change.

Enable GLM by adding to `.env` (keep `ANTHROPIC_*` commented so Opus stays the default):

```bash
export GLM_BASE_URL="https://api.z.ai/api/anthropic"
export GLM_AUTH_TOKEN="..."                 # your Z.ai key (sent as the bearer token)
export GLM_MODEL="glm-4.6"                   # optional
export GLM_SMALL_FAST_MODEL="glm-4.5-air"    # optional
```

Then choose the backend — it **persists** and applies to every subsequent run:

- **Cockpit:** the *Model* selector in the top control bar (the GLM option appears once the key is set).
- **CLI:** `./general model glm` (or `opus`) to set it, `./general model` to show it, or `--model glm`
  to override a single run — `./general --model glm task <app> "..."`.

Selecting GLM sends the run's prompts (your code, tickets, diffs) to Z.ai, a third-party provider. If
GLM is selected but the key isn't configured, runs are **blocked** with a clear message — no silent
fallback. Opus is never affected and stays the default.

## Safety model (read before `--live`)

- **Dry-run is the default** and previews dev health without side effects.
- **`main` is never touched** — the General is code-blocked from it; you merge dev→main.
- **dev is kept green**: a merge that fails dev's gate is auto-reverted and turned into a PR.
- **Reviewer is read-only** at the permission layer — it cannot edit code.
- **Bounded**: `max_iterations` per ticket and a `max_cost_usd` budget per run.
- **Auditable**: `audit.jsonl` records every build, gate, review, verdict, cost, and diff hash.

## Layout

```
general                 wrapper script (./general ...)
config.yaml             your apps (from config.example.yaml)
orchestrator/
  main.py        CLI: task / ticket / drain / doctor
  loop.py        build -> gate -> review -> land-on-dev / retry / escalate
  builder.py     Builder officer (Agent SDK, full tools)
  reviewer.py    Reviewer officer (Agent SDK, read-only, JSON verdict)
  agent.py       shared Agent SDK runner
  gate.py        runs your tests/lint (branch, then dev)
  git_ops.py     branch / diff / keep-green merge / PR ; main is protected
  intake.py      task / ticket / drain -> worklist
  contracts.py   the hand-off data contracts
  config.py      apps + global settings (+ env secrets)
  audit.py       JSONL audit log
  backlog/       Jira (primary), Notion (stub), none
```
