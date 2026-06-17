# The Elite Unit — Roadmap

The durable plan. The live task list in Cowork mirrors this, but this file is the source of
truth (version-controlled, reviewable on GitHub). Update it as we ship.

Last updated: 2026-06-16.

## Shipped

- **Free-form council + ad-hoc meetings** — the council is now a real **multi-round debate**:
  officers read each other and respond by name (agree / push back / add), can reply `PASS`,
  and the round-table converges early when no one has more to say (`council_rounds`, default 2).
  Any officer can request a focused **MEETING:** on a problem; you (or the General) convene one
  with `general meeting --topic "…" [--officers …]` or the War-Room **Meeting** button — the
  relevant officers debate, the General writes a decision record, and the Scribe folds the
  outcome into Unit Memory. *(Auto-triggering meetings from events is Next — "proactive autonomy".)*

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
4. **Proactive autonomy** — make the unit act without being told: event triggers (after merge → Scout smoke-test; after a security block → Provost+Engineer huddle; after N parks → Drillmaster meeting), auto-convene meetings from officer `MEETING:` requests, and have meeting decisions spawn concrete actions (drill / hire / ticket). Builds on the multi-round council + meetings just shipped.
5. **Finding 1 — superadmin authz** — Provost files it; Roman + the General build the fix together (platform-admin probe + test invariant).

## Notes

- The General self-hosts on Roman's Mac (Claude Max login, no API key). Cowork edits the source; Roman runs it.
- Repo: github.com/roman-berlin/The-General (private), `main` + `dev`.
- Roman runs separate products: Automatixy CRM, SignalDesk, MQL5 EAs — the war room's project switcher serves this.
