# The Elite Unit — Roadmap

The durable plan. The live task list in Cowork mirrors this, but this file is the source of
truth (version-controlled, reviewable on GitHub). Update it as we ship.

Last updated: 2026-06-19.

## Shipped

- **Mac ↔ server state sync (`general sync`)** — the server's cockpit + councils now reflect what the
  Mac ships, and vice-versa. Each machine publishes its `audit.jsonl` to a single-writer
  `shared/<host>.jsonl` on a dedicated **orphan `unit-state` branch** (a separate gitignored
  `.unit-state/` clone — runtime state never pollutes `main`/`dev`, so it can't fight the server's
  `git reset --hard origin/main` promotion). `dashboard.audit_lines` merges the local audit with every
  peer's file (exact-dup lines collapsed), so `load_tasks` / cockpit KPIs / `collect_signals` all see
  the unified record; a `↔ synced: mac, server` badge shows under the KPI row. Single-writer files = no
  merge driver, no append race; best-effort (a git hiccup just no-ops). Pull-only suffices for the
  server to see the Mac. Tests: real bare-origin round-trip + dedup + fail-soft (**12/12**); full
  General harness sweep **164/164**, no regression. Schedule: Mac launchd every 15 min + VPS `*/15`
  cron (set `GENERAL_HOST_ID`); runbook updated.

- **Finding 1 — superadmin authz hardened (defense in depth)** — the platform-admin surface
  (`/api/v1/admin/*`) is now gated at the **router** by construction —
  `APIRouter(..., dependencies=[Depends(require_superadmin)])` — so a future admin route can no longer
  be silently exposed by forgetting the per-handler guard (a one-char omission that the old per-route
  pattern allowed). Plus a **probe-test invariant** (`backend/tests/api/routes/test_admin_authz.py`)
  that enumerates *every* route on the admin router and asserts **401 (anon) + 403 (non-admin)** — so
  adding an unguarded admin route turns the suite red before it ships. Guard logic untouched (still
  5/5); full backend suite green (**115/115**).

- **Turn-budget scaling + "too big" surfacing** — the build turn budget now scales with effort
  (`builder_max_turns` base 60 → high ~96 → max ~144, configurable) so a deep ticket doesn't error
  out mid-implementation; and if a build does hit the cap, it's surfaced as an actionable
  **🛑 needs you — "ticket too big: split it or raise the budget"** (a Chat decision), not a
  confusing "errored." Soldiers get the same scaling. (From the AUTO-13 E2E test, which is a
  6-phase epic that blew the old fixed 60-turn cap.) Verified 12/12.

- **Proactive autonomy — the unit acts on its own deliberations** — three pieces close the
  autonomy loop: (1) **auto-convened meetings** — when an officer ends a council turn with a
  `MEETING:` request, the event reactor convenes that huddle on the next cycle (once, then marks
  it actioned), so the unit follows up on its own calls; (2) **meetings auto-spawn tickets** —
  with `meeting_autospawn` on, a meeting's decision FILES the Jira tickets it proposes (de-duped,
  assigned to you) — drills and hires deliberately stay proposal-only (your approval); (3)
  **after-merge Scout** — with `scout_after_merge` on, the Scout smoke-tests the running DEV app
  right after each live merge and files any runtime/UX/a11y regression. All gated + fail-safe (an
  autonomy hiccup never breaks the autopilot). Verified by a dedicated harness (17/17); dashboard
  66/66, patrol + squad still green.

- **Scheduled patrols (find → file, unprompted)** — `general patrol <app>` sends the three recon
  officers — **Scout** (runtime/UX/a11y), **Provost** (security), **Quartermaster** (deploy
  readiness) — to sweep DEV and **file their ticket-worthy findings as Jira tickets** (de-duped,
  assigned to you, To Do), so with Autopilot armed the unit continuously finds → files → fixes.
  One officer failing never aborts the patrol; `--no-file` makes it propose-only, `--officers`
  picks a subset. Trigger on demand from the War Room (**Unit → Run patrol**, with a confirm since
  it files tickets), or on a cadence via the included launchd plist (weekly Mon 09:00 —
  `scripts/com.roman.general.patrol.plist` + `run-patrol.sh`, not auto-loaded; you `launchctl load`
  it when ready). Verified by a dedicated harness (13/13); dashboard suite 66/66.

- **Squad delegation (the chain of command executes)** — for a sized-big ticket (L/XL or many
  acceptance criteria), the **Field Engineer now splits the work** into a few non-overlapping
  subtasks and dispatches each to the right **soldier** — Vanguard FE (frontend) · Ordnance BE
  (backend) · Logistics DB (migrations/RLS) · DevOps · Sapper (generalist) — **each sized to its
  own slice** (task-adaptive effort, now extended down to the soldiers). Soldiers run sequentially
  on the same isolated branch; the existing gate + review + keep-DEV-green merge still validate the
  combined result, and MAIN is untouched. With the new SSE feed you watch each soldier work live
  (`· soldier·ordnance-be: Edit …`). **Fail-safe**: a thin plan (<2 subtasks), an atomic ticket, a
  retry pass, or any planning hiccup falls back to the normal solo build. **Off by default** — arm
  with `delegation_enabled: true` (knobs: `delegation_min_ac`, `delegation_max_soldiers`). Verified
  by a dedicated harness (23/23); dashboard suite still green (64/64).

- **War Room — live streaming (SSE)** — the cockpit now updates in **real time**: a
  `/api/stream` Server-Sent-Events endpoint pushes a freshly-rendered board the instant the unit
  prints a step (≤0.5s) instead of the old 5-second poll — so each officer's phases *and*
  per-tool-call lines (`· builder: Edit foo.tsx`) stream into the Live feed and active-run panel
  as they happen. A green **live** dot in the header shows the stream is connected; if it drops,
  the page falls back to the 5s poll and auto-reconnects. (`threaded=True` so the long-lived
  stream never blocks the cockpit.) Verified end-to-end — a printed step reached the browser in
  ~0.5s (dashboard QA 64/64 + a live push test).

- **The unit talks — group room, real stand-up, training charter** — three "live like a real
  unit" additions: (1) a **Group room** in the chat (tab next to your 1:1 General chat) where you
  consult the whole unit / brainstorm — your message goes to every officer, the **relevant ones
  answer in character, others add a short comment, off-lane officers stay quiet (PASS)**, each
  building on the last; the thread persists. (2) A **real officer stand-up** — `/standup` keeps the
  instant snapshot *and* adds a **Hold stand-up** button that has every officer report **Yesterday /
  Today / Blockers** from the actual record and flag **hand-offs** ('need <Officer>: why'), which
  are collected into a cross-officer section (this is *on top of* the council, where officers
  already debate each other by name and can call a `MEETING:`). (3) The **Drillmaster** charter now
  explicitly owns **onboarding** new officers/soldiers and **refresher** drills for existing ones —
  the unit's training officer. Plus the Mac launcher now runs under `caffeinate` so the scheduled
  10:00 council isn't skipped by sleep. Verified by the dashboard QA harness (59/59).

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
  *(Now extended down to the soldiers — see Squad delegation above.)*

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
  scoped by a **project switcher** in the header (multi-project). Updates **live over SSE**
  (≤0.5s, with a 5s-poll fallback). Detailed transcript table moved to `/tasks`.

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

_(Clear. The General self-hosts 24/7 on the Oracle VPS now — surface the next item from a daily
council or a patrol finding.)_

## Notes

- The General self-hosts on Roman's Mac (Claude Max login, no API key). Cowork edits the source; Roman runs it.
- Repo: github.com/roman-berlin/Elite-Unit (private), `main` (prod, the VPS runs it) + `dev` (active dev on the Mac).
- Roman runs separate products: Automatixy CRM, SignalDesk, MQL5 EAs — the war room's project switcher serves this.
