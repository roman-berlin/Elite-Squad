# The Elite Unit — Roadmap

The durable plan. The live task list in Cowork mirrors this, but this file is the source of
truth (version-controlled, reviewable on GitHub). Update it as we ship.

Last updated: 2026-06-18.

## Shipped

- **Run controls & guardrails (QA pass)** — three safety/visibility fixes from a War-Room QA
  sweep: (1) **confirm before anything live** — a LIVE free-task run, a LIVE ticket-develop, and
  **Start Autopilot** now pop a confirm (dry-runs never prompt), so one stray click can't spend
  Opus or merge to DEV; (2) a **Stop control** on the active-run panel that cooperatively halts a
  manual run at the next safe checkpoint (before the next build pass / before the merge — **DEV is
  never left half-merged**), wired through `loop.run` via a `stop_event`; (3) **run telemetry** —
  the active run shows **elapsed time + est. cost** alongside the live→DEV chip and heartbeat. Chat
  also auto-scrolls to new messages. Verified by the dashboard QA harness (48/48).

- **War Room chat** — a messaging view (toolbar **💬 Chat**, with a red unread badge) to talk
  with the unit. Pending decisions — the reviewer's product questions, escalations, and Builder
  **halts** — appear as cards you answer inline; below them is a free-form thread with The
  General. Your reply runs the same `route_message` backend as a Telegram reply, so it
  **resolves the decision and resumes the parked ticket** (or gets the General's answer), and
  Telegram + dashboard stay in sync.

- **Smarter run outcomes** — when the Builder *deliberately halts* on a failed precondition
  (e.g. a missing prior migration phase) and makes no edits, that's now surfaced as
  **"needs you" + the Builder's full report** (Telegram + dashboard + parked so Autopilot
  won't retry it forever) instead of a confusing "errored". And auto-sizing **caps at high** —
  `max`/`xhigh` only via an explicit `effort-max`/`effort-ultra` pin (or retry-escalation after
  a real rejection) — so a small ticket can't burn max effort over-exploring.

- **Proactive autonomy (the unit acts on its own)** — between Autopilot cycles the officers
  convene *themselves*, throttled by a cooldown so they never spam: a **security block** pulls
  Provost + Field Engineer + Inspector into a huddle; **repeated parks** trigger a "why are we
  stuck" meeting; on a quiet queue there's a configurable chance of a **spontaneous meeting** or
  **corridor small-talk** (two officers, in character — flavour that sometimes lands a real
  insight). Every outcome logs to Unit Memory. Knobs: `autonomy_enabled`, `autonomy_cooldown_min`,
  `meeting_on_security_block`, `parks_meeting_threshold`, `smalltalk_prob`, `random_meeting_prob`.
  Fires only under Autopilot (manual runs stay quiet); test on demand with `general smalltalk`.

- **Free-form council + ad-hoc meetings** — the council is now a real **multi-round debate**:
  officers read each other and respond by name (agree / push back / add), can reply `PASS`,
  and the round-table converges early when no one has more to say (`council_rounds`, default 2).
  Any officer can request a focused **MEETING:** on a problem; you (or the General) convene one
  with `general meeting --topic "…" [--officers …]` or the War-Room **Meeting** button — the
  relevant officers debate, the General writes a decision record, and the Scribe folds the
  outcome into Unit Memory. Includes a **ship-review** (`general ship-review` / War-Room button):
  the Quartermaster certifies deploy-readiness, QM + Provost + Inspector debate, and the General
  issues a **GO / NO-GO** — but the unit **never promotes to MAIN; that's the Commander's call.**

- **Unit Memory (living protocol)** — `memory/UNIT.md`, the unit's shared company memory:
  Mission, Commander **Standing Orders**, and per-app notes (human-owned), plus a
  Scribe-maintained **Lessons & Decisions** log. Every officer reads it before acting (the
  preamble is injected into all eight officers' prompts). The **Scribe** folds each council's
  lessons in automatically — and on demand via `general scribe` or the War-Room button —
  writing ONLY between protected markers, so your hand-edits are never clobbered; every write
  is backed up and git tracks the evolution. View/curate at `/memory` or `general memory`.

- **Task-adaptive Builder effort** — the Field Engineer now sizes its thinking depth from
  the ticket (XS→low · S/M→medium · L→high · XL→max) using acceptance-criteria count,
  description depth, issue type, labels, and keyword signals (refactor / migration /
  security / schema = heavier; typo / copy / rename / css = lighter). The full SDK ladder is
  honoured — `low · medium · high · xhigh · max` (`xhigh` = "ultra", Opus-only, falls back to
  high off-Opus). A Jira `effort-ultra` / `effort-max` label (or an `[effort:ultra]` marker)
  pins any tier and bypasses sizing — synonyms like "ultra"/"ultracode" normalize to `xhigh`;
  an explicit `--effort` / War-Room pick does the same. A rejected pass still escalates one
  level per retry. Effort ladder is centralized (one source of truth) and the chosen
  effort + reason are logged to the audit. Off-switch: `adaptive_effort` (default on).
  *(Soldier/sub-agent effort awaits the delegation feature — see Next.)*

- **Health-gated cockpit + Autopilot switch** — opening the War Room runs a full health
  check (Claude login, Agent SDK, git, and per-app repo / base-branch / Jira-creds) and shows
  a big green **"System healthy"** banner, or **red** with the exact failing checks. Work is
  gated: **Run and Autopilot are disabled until healthy**. A header **Autopilot ON/OFF switch**
  starts/stops the always-on loop in-process (live, scoped to the selected project,
  interruptible). The `Refresh-General.command` launcher stops the old cockpit, re-runs
  `doctor`, relaunches, and opens it as a **chromeless app window**.

- **War Room cockpit (v1)** — `general serve` now opens the command view: KPI strip
  (merged today, merged total, needs-you, avg passes/ticket, parked, security blocks),
  the **active run** with a Build→Gate→Review→Security→Land phase bar, the **8-officer
  roster** with live/recent/idle status dots, and the unit **activity feed** — all
  scoped by a **project switcher** in the header (multi-project). Auto-refreshes every
  5s. Detailed transcript table moved to `/tasks`. (Live per-event SSE streaming is the
  remaining war-room piece — see Next.)

- **Autonomous pipeline** — build → gate → review → **security gate (Provost)** → land on DEV → QA, on an isolated git worktree; MAIN never touched.
- **Seven officers** — Adjutant (S-1/HR) · Field Engineer (Builder) · Inspector General (Reviewer) · Scout (S-2/QA) · Provost Marshal (Security) · Quartermaster (S-4/DevOps) · Drillmaster (Doctrine).
- **Autopilot** — always-on worker: resume In Progress, else take top To Do (assignee-pinned to you), with a park-guard so it never spins on a stuck ticket; KeepAlive launchd.
- **Daily council** — 10:00 muster, briefing to Telegram, transcript saved, escalates only Commander-level calls.
- **Two-way Telegram** — the General answers you; your replies become standing guidance.
- **Self-improvement** — `drill --apply` writes approved officer/squad edits (backed up).
- **Self-staffing** — Adjutant proposes/executes hires (HR-gated): soldiers + junior officers under each major.
- **Self-filing** — Scout/Provost/Quartermaster file their own findings as Jira tickets (`--file`, de-duped, assigned to you).
- **Find → fix → verify loop** — proven end-to-end (Provost found AUTO-12 → built → re-gated → merged → QA).
- **Builder unattended** — loads no repo settings so nested `ask` rules never block writes; reads CLAUDE.md/.claude/rules for conventions.

## Next — in priority order

1. **War Room — live streaming (SSE)** — the one remaining cockpit piece: stream each
   officer's phases + tool-calls into the active-run panel in real time (needs the loop to
   emit an event stream). Everything else in the War Room shipped in v1.
2. **Soldier / sub-agent delegation** — let the Field Engineer (and other majors) actually
   dispatch subtasks to their soldiers (`.claude/agents` sub-agents) — each soldier sized to
   its subtask. Today soldiers are roster/doctrine only; the Builder runs solo. This makes the
   chain of command execute, and extends task-adaptive effort down to the soldiers.
3. **Scheduled patrols** — Scout/Provost/Quartermaster on a cadence with `--file`, so the unit continuously finds → files → fixes unprompted.
4. **Proactive autonomy — remaining pieces** (event triggers, small-talk, and ship-review shipped): auto-convening a meeting from an officer's `MEETING:` request, after-merge Scout smoke-tests, and **meetings that auto-spawn actions** (a decision files a drill / hire / ticket without you).
5. **Finding 1 — superadmin authz** — Provost files it; Roman + the General build the fix together (platform-admin probe + test invariant).

## Notes

- The General self-hosts on Roman's Mac (Claude Max login, no API key). Cowork edits the source; Roman runs it.
- Repo: github.com/roman-berlin/The-General (private), `main` + `dev`.
- Roman runs separate products: Automatixy CRM, SignalDesk, MQL5 EAs — the war room's project switcher serves this.
