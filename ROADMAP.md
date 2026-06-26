# The Elite Unit — Roadmap

The durable plan. The live task list in Cowork mirrors this, but this file is the source of
truth (version-controlled, reviewable on GitHub). Update it as we ship.

Last updated: 2026-06-20.

## Shipped

- **PM triage on iteration-exhaustion + dependency-discipline doctrine** (2026-06-21) — closed a real gap:
  when a ticket burned all `max_iterations` on repeated Reviewer rejections it escalated **straight to the
  Commander** with no PM consult (the PM only ever fired on a Builder *halt*, `loop.py:299`) — so
  scope-creep blow-ups like AUTO-9 / AUTO-18 parked on Roman even though the core work was done. Now, at
  exhaustion the loop calls **`pm.triage`**: if the deliverable is essentially done and the rejections are
  fixable scope-creep, it returns ONE surgical instruction and the ticket is **re-queued** for a single
  corrective pass (new non-parked `Outcome.REQUEUED`; capped to one triage/ticket via the audit so a truly
  stuck ticket still lands on you); otherwise it escalates just the genuine decision as a readable brief
  (recorded as `needs_human` so Needs-you shows the ask, not an empty "escalated" row). Root cause also
  fixed at source: a **standing doctrine** in `UNIT.md` — *never change dependency versions / the lockfile
  / an out-of-scope app unless the ticket is about that* — which is what produced the `@supabase` +
  `bun.lock` churn that broke the `useLeads`/`useTrips` typecheck across both tickets. Scope-guidance
  comments posted on AUTO-9 + AUTO-18. **14 new tests; suite 53 harnesses / 733 checks, all green.**
- **Brief escalations + reliable Needs-you answer + grounded General chat** (2026-06-21) — three fixes
  from a live drain. (1) The PM now escalates in a tight **BLOCKER/DECISION/OPTIONS/RECOMMENDATION**
  brief, and the Needs-you card leads with that brief (`dashboard.brief`), full message behind a toggle —
  no more wall of text. (2) Answering a parked ticket with no pending decision now **records the answer +
  re-queues + clears the row** (`/api/answer` dismisses it), so the answer visibly "takes." (3) The
  **General chat was hallucinating** (invented repo paths, told the Commander to "paste into Claude
  Code") — `respond_to_commander` now gets the real app config (paths/branches/Jira) + a "the unit builds
  tickets itself" operating block. 18 tests; suite 53 harnesses / ~723 checks.
- **Live phase bar + bigger terminal + collapsible Activity** (2026-06-21) — three cockpit fixes. (1) A
  build started by the answer box / `/unblock` / autopilot now lights the **ACTIVE RUN phase bar**
  (Build → Gate → Review → Security → Land) instead of showing "last run · interrupted" while the builder
  is clearly working — `_run_in_flight` infers a live run from fresh audit activity with no terminal
  outcome (time-bounded so a crash stops reading as live). (2) The **LIVE FEED terminal** is taller
  (380px) and drag-resizable. (3) The **Activity** panel is collapsible with a caret. Collapse + height
  persist in localStorage and survive the SSE re-render. **12 tests; suite 50 harnesses / 701 checks.**
- **Needs-you: answer a parked decision from the cockpit** (2026-06-21) — a parked "awaiting decision"
  ticket (e.g. AUTO-14) could only be "Discuss with the General" (chat) or Dismiss — no way to ship an
  actual answer. Added `/api/answer` + a **Ship-answer** box on every Needs-you card: it resolves the
  pending decision and re-runs the ticket with the answer baked into its spec; if there's no pending
  decision on file, it records the answer as a **Jira comment** (the next build reads all comments) and
  unblocks the ticket for retry. Works without touching Telegram. 10 tests; suite 49 harnesses / 689 checks.
- **/jira recognizes an app's existing config+env Jira** (2026-06-21) — the page wrongly said "No Jira
  connected for automatixy" while the unit was happily pulling AUTO-* tickets. It only looked at the new
  cockpit connection store and ignored the app's `config.yaml` `backlog:` + env-var creds. Now, when no
  quick-connect is assigned, it shows the **config+env Jira as connected** — site URL (`toibis.atlassian.net`),
  project key (`AUTO`), the user (`JIRA_EMAIL`), and token-set status — and only shows "No Jira" for a
  genuinely backlog-less app. 5 tests added; suite 48 harnesses / 679 checks.
- **Deploy progress bar + fresh ahead-count** (2026-06-21) — the "Update unit" / "Ship → production"
  buttons looked stuck (the GitHub push takes 10–30s with no feedback) and the ahead-count badge went
  stale after a merge (showed "13"/"8" when DEV was already fully merged into MAIN — count was really 0).
  Now: a live **progress strip** shows while a promote/ship runs, polls `/api/deploy-status`, and reloads
  the page when it finishes so the count re-renders; the busy flag is set synchronously to avoid a render
  race; and at 0-ahead both buttons show an explicit **"✓ unit current" / "✓ <app> shipped"** instead of
  a stale number. **13/13 tests; suite 48 harnesses / 674 checks.**
- **Runnable discovered repos — click-to-onboard** (2026-06-21) — the "Found nearby" repos (git repos
  beside your configured ones, not yet in config) are now actionable: the **➕ Product** page lists them
  as chips, and clicking one pre-fills the onboard form (name + path + auto-detected base/protected
  branches). One click + confirm turns a discovered repo into a worked project. Reuses the onboarding
  engine + hardened `_insert_under_apps` to create config.yaml if it's somehow missing. **10/10 tests;
  suite 47 harnesses / 661 checks.**
- **Memory consolidation + learning from rejections** (2026-06-21) — closes the learning loop.
  `consolidate.py` dedups + prunes the living Lessons log so it stays tight, and scans the Reviewer's
  FAIL verdicts to cluster recurring rejection themes (an explicit tenant filter, tests-with-the-change,
  complete typing, error/edge handling, validation, security, style, docs). When a theme recurs across
  ≥2 distinct tickets it folds a one-line lesson into the log ("Reviewer repeatedly required X — do Y",
  idempotent) and surfaces a **drill proposal** — so the unit stops repeating the mistakes its own
  Reviewer keeps catching. Deterministic / free; the Scribe runs it after every pass. `general
  consolidate [--dry]` + cockpit /memory ("Consolidate" + a recurring-rejections panel). **23/23 tests;
  suite 46 harnesses / 651 checks.**
- **Failure forensics + auto-post-mortem** (2026-06-21) — turns the "errored / escalated" pile into
  something actionable. `forensics.py` classifies every failed run into a cause taxonomy (under-specified,
  too-big, merge-conflict, gate/review fail, security block, product-decision-needed, infra, transient),
  each with a recommended fix; surfaces **repeat offenders**; and once a ticket fails `postmortem_after`
  (3) times the unit **auto-writes a deterministic post-mortem** (`postmortems/<TICKET>.md`: pattern,
  attempt timeline, dominant cause, what to change) — no model call, runs unattended. Hooked into the run
  loop; cockpit **🧩 Failure forensics** page + `general forensics`. **37/37 tests; suite 45 harnesses /
  628 checks.**
- **One-command new-product onboarding** (2026-06-21) — scaffold a new product into the unit without
  hand-editing YAML. `onboarding.py` validates the repo is a git repo, **auto-detects** its base/protected
  branches (honoring DEV/MAIN vs dev/main casing), refuses duplicate names, **backs up `config.yaml`** and
  does a **surgical insert** under `apps:` so existing comments/order survive, and optionally **attaches a
  saved Jira connection** to the new project. CLI `general onboard <name> <repo> [--jira <id>] [--write]`
  (preview-by-default) + a cockpit **➕ Product** page. So the unit can take on SignalDesk, the MQL5 EAs,
  etc. **33/33 tests; suite 44 harnesses / 591 checks.**
- **Cockpit Jira-connection UI — pick / quick-connect a Jira per project** (2026-06-20) — Roman runs
  products against different Jira accounts (Automatixy on one, the algo-trading robot on another).
  `connections.py` is a gitignored store of named Jira connections + which one each project uses; the
  `/jira` cockpit page picks the Jira for the current project, quick-connects a new one (name + site URL
  + email + API token, **password field, masked everywhere, never committed**), and switches projects
  from the same page. The Jira adapter prefers an assigned connection and falls back to the existing
  `email_env`/`token_env` env vars, so nothing breaks. **37/37 tests; suite 43 harnesses / 558 checks.**
- **Ticket-readiness gate — hand back under-specified tickets before building** (2026-06-20) — the first
  P1 item. A cheap, **deterministic** check (`readiness.py`, no model call) runs before the build: a
  ticket with **no acceptance criteria AND a thin description** (or one that just restates its title) is
  handed back — moved to Needs Human with a comment listing exactly what's missing — instead of the
  Builder guessing the scope and halting mid-way. Anything with acceptance criteria, or a real
  description, sails through. Saves the wasted Opus on a blind build. Opt-in (`readiness_gate`;
  `readiness_min_desc` tunes the threshold); off by default. **16/16** tests; suite now **42 / 521**.

- **The unit's own test suite + CI — the guard that guards the guard** (2026-06-20) — promoted the ~40
  scratch harnesses (which used to vanish each session) into the repo as **`tests/`** with a
  `tests/run_all.py` runner (**41 harnesses / 505 checks, all green**), and a **GitHub Actions** workflow
  that runs the whole suite on every push to `dev`/`main` and every PR — so a regression in the
  orchestrator goes red before it reaches the 24/7 server. Paths made portable (repo-relative), and the
  two long-stale harnesses fixed (`patrol_test` fake-signature drift; `wr_test` missing setup +
  render-signature drift). Closes the last P0 hardening item.

- **Hard tool-call guardrail (denylist) under bypassPermissions** (2026-06-20) — the safety net the
  bypassPermissions change needed. A **PreToolUse SDK hook** (`guard.py`) that BLOCKS in code — no matter
  what an officer decides — writes to `.env*` / secrets / keys / `.github` CI config, and destructive
  shell (`rm -rf` of a root/home path, `git push --force`, push to `main`/`master`, `git reset --hard`,
  `DROP`/`TRUNCATE`, `chmod 777`, fork bombs, `curl … | sh`). Wired into the **builder + soldiers** (the
  write-capable agents). Fails closed only on a clear match; `.env.example` templates and every normal
  build command pass through untouched. **53/53** tests.

- **Ship app→production does a REAL merge of DEV into MAIN** (2026-06-20) — the app Ship button used a
  fast-forward, which can't ship a repo whose MAIN carries its own commits — and Automatixy's MAIN does
  (the "Merge pull request #43–47" bookkeeping commits from shipping via PRs). So ff was rejected and
  nothing shipped. Rewrote `promote_app` to do a **real merge** of DEV into MAIN (MAIN keeps its
  commits, DEV's are added) in a **throwaway git worktree** — the user's dirty checkout is never
  touched, and it works even when the branches have diverged — then push and advance the local ref so
  the cockpit shows 0-ahead. If MAIN is a protected branch the push is rejected with a clear "ship via a
  PR" message. (The General's "Update unit" stays fast-forward — correct for its pure dev→main.) **11/11**.

- **Fix the stuck "Update unit" / "Ship" buttons — promote on the remote, not via checkout** (2026-06-20)
  — the cockpit promote used to `git checkout main → merge → push`, which **chokes on a dirty working
  tree** (the unit constantly writes runtime files like `council/index.jsonl`) and could **hang on
  HTTPS credentials** — the "still thinking" spinner. Now `promote` (dev→main) and `promote_app`
  (DEV→MAIN) push the branch straight onto the target **on the remote** (`git push origin dev:main`),
  fast-forward only, **never touching the working tree**, then advance the local ref. Plus
  `GIT_TERMINAL_PROMPT=0` so a push without cached creds **fails fast instead of hanging**. Manual
  equivalent any time: `git push origin dev:main`. Tests **13/13** (incl. a dirty-tree regression).

- **Ship → production opens a review page (commits + their tickets)** (2026-06-20) — the
  "Ship <app> → production" button no longer ships on a single confirm click. It opens a review page
  listing every commit DEV is ahead of MAIN, **grouped by ticket** (the AUTO-key parsed from each commit
  subject, e.g. AUTO-4 → its commits), with a "tickets going live" summary and the final
  **Ship to production** button there. So you see exactly what's about to deploy before you commit to it.
  `sync.app_promote_commits` + a `/ship-preview` route. Tests **15/15**.

- **Launcher: close the whole War Room terminal pile** (2026-06-20) — the earlier "close the old window"
  fix was tag-based, so it couldn't recognise windows opened before the fix. Now all three launchers
  (`War Room.command`, repo `War-Room.command`, **and the `Elite War Room.app`**) close every other
  cockpit terminal by matching the cockpit URL/banner in each window's scrollback — catching the whole
  pile, idle (no "terminate?" prompt) because the server was just stopped. One relaunch clears them all.

- **Living roster doc + hierarchy chart** (2026-06-20) — a once-a-day, info-only `ROSTER.md`: the
  chain-of-command (Mermaid chart), every officer + role + **duty** + the model it runs on, and the
  soldiers each squad fields — all read straight from the code, so it can never drift from reality. A
  single cheapest-model (Haiku) line adds the day's status ("shipped N, parked M"). Refreshed at the end
  of the daily council; on demand via `general roster`; viewable in the cockpit (Reports → Unit roster).
  Gitignored (runtime). Sentinel was also added to the cockpit roster for consistency. Tests **30/30**.

- **Automode — the unit never stops for your approval** (2026-06-20) — opt-in `auto_mode`. Normally
  when a build hits a product/scope blocker the PM either decides the everyday calls or ESCALATES a
  critical one and **parks to wait for your approve button**. In automode the PM **must decide** — it
  makes the best *reversible* call, the unit keeps building, and the decision is logged on the ticket +
  Telegram for you to review and **reverse** afterward. The safety floors are untouched: everything
  lands on DEV (never MAIN — only you ship to production), **Sentinel** guards DEV and auto-reverts a
  bad land, the Provost still blocks CRITICAL security, and the max-iterations / token-budget limits
  still stop a runaway. Off by default. Tests **14/14**.

- **Cockpit: "Update unit" vs "Ship app" — no more confusing the two repos** (2026-06-20) — the header
  had two `dev→main` buttons for two *different* repos sitting side by side. Now unmistakable: the green
  **⚙ Update unit** promotes **The General's own code** (`~/Projects/General` dev→main → the 24/7 VPS
  self-updates) — its confirm says "the unit's brain, NOT your app"; the purple **🚀 Ship <app> →
  production** promotes the **product** (e.g. Automatixy DEV→MAIN → live) — its confirm says "the <app>
  APP to PRODUCTION." A divider separates them. Same wiring as before (already targeted the right repos
  — lowercase `dev`/`main` for the unit, uppercase `DEV`/`MAIN` for the app); this is the labelling fix.

- **Sentinel — S-3 · Integration & rollback (post-merge guard + auto-revert)** (2026-06-20) — a new
  officer and the unit's last line of defence on DEV. The pre-review gate already validates the exact
  merge on a throwaway branch before DEV is touched, so Sentinel runs the **heavier post-merge suite**
  (`postmerge_commands` — e2e/integration too slow for every build pass) on the *landed* DEV. If it's
  red, Sentinel **reverts the merge forward-only** (`git revert -m 1`, no force-push, no history
  rewrite) and hands the ticket back (Needs Human + comment), so DEV is never left broken. Opt-in
  (`sentinel_enabled` + a per-app `postmerge_commands` list); a no-op when no post-merge suite is
  configured. Now sits on the council roster. Tests **18/18**, including a real `git revert`
  round-trip.

- **Auto model selection — Opus for code, conserve only under budget pressure** (2026-06-20) — opt-in
  `auto_model`. Opus is the better coder, so the **builder and reviewer stay on Opus for all code** and
  drop to Sonnet *only when the day's token budget is tight* (to keep the unit working rather than
  hard-stopping) — never below Sonnet, never above the configured ceiling. The cheaper tiers stay
  confined to the non-coding chatter (council/corridor were already Sonnet/Haiku). Off by default —
  out of the box every officer uses exactly the model it always did. Tests **21/21**.

- **Cost governor v2 + token-usage window** (2026-06-20) — Roman is on the **Max** plan, so the budget
  that matters is token throughput. Every agent call (officer / builder / soldier / council / chat) now
  meters its input+output tokens from a single choke-point (`agent.run_agent` → `usage.py` ledger). The
  cockpit gets a **Token usage** page — *today / last 7d / last 30d*, totals + a per-model breakdown +
  a daily-budget bar. A configurable `daily_token_budget` makes **Autopilot pause itself** when the
  day's burn hits the ceiling (Telegram alert at 80%, pause at 100%, auto-resume next day). Header shows
  today's burn at a glance. Tests **20/20**; full sweep 318.

- **War Room launcher — one click = clean refresh** (2026-06-20) — double-clicking *War Room.command*
  now (1) stops the old cockpit server, (2) **closes the old cockpit Terminal window** (tags its own
  window so each launch leaves exactly one), and (3) closes the stale browser tab + opens one fresh tab.
  No more piling up dead terminals and tabs. (Desktop launcher + repo `scripts/War-Room.command` kept
  in sync; first run asks once for Automation permission.)

- **Officers never stall remotely — bypassPermissions + own-token Jira** (2026-06-20) — every
  read-only officer (the General's chat, the whole council, stand-up, ship-review, group room, plus the
  adjutant / drillmaster / reviewer / squad propose+plan passes — **12 in all**) now runs
  `permission_mode="bypassPermissions"` instead of `default`. On the headless VPS a `default` officer
  **dead-stopped** waiting for a tool-approval prompt no human could answer (the "approve Atlassian in
  Claude" wall over Telegram). Writes stay disallowed, so bypass just means "read without stalling."
  The General also answers ticket questions ("is AUTO-14 ok?") by pulling the ticket through the unit's
  **own** Jira adapter/token and injecting it inline — never reaching for an ambient Atlassian MCP.
  Tests **20/20**; full sweep 278.

- **Needs-you — expandable problem detail + pre-filled General chat** (2026-06-20) — each "runs that
  need you" card now **expands** (native disclosure arrow) to the full story: what halted, the
  Builder's summary, the Reviewer's verdict, and the specific findings. **"Discuss with the General"**
  pre-loads a one-line brief of that problem into the chat composer, so you send (or tweak) instead of
  retyping. One source of truth (`dashboard.needs_detail_html` + `needs_chat_summary`). Tests **20/20**.

- **Cockpit project switcher + Recent projects** (2026-06-20) — a VS-Code-style **Recent** list at the
  top of the header switcher, plus **nearby-repo discovery** (scans the parents of configured repos +
  `$GENERAL_PROJECTS_DIR` / `~/Projects`) so you can see what else is there to onboard. Tests **10/10**.

- **Cockpit "Ship → MAIN" button** (2026-06-20) — promote the **current app's** DEV→MAIN to production
  from the War Room (Mac-only, ff-only, confirm-gated) — the app-level sibling of the unit's own
  Deploy→main. Tests **9/9**.

- **Server→Mac living-log bridge** — the Mac pulls the server's runtime `UNIT.live.md` over SSH so the
  Mac cockpit reflects what the 24/7 server has been doing (gated on `GENERAL_SERVER_SSH`, skipped on
  the read-only server). Tests **7/7**.

- **Unit Memory split — doctrine vs runtime log** — `memory/UNIT.md` (tracked doctrine) vs
  `memory/UNIT.live.md` (gitignored, server-canonical living log), so the server's `git reset --hard`
  self-update can't clobber the officers' running notes. Tests **9/9**.

- **Multi-Jira develop loop** — per-app Jira credentials (`email_env` / `token_env` per backlog) so the
  unit develops tickets across several Jira connections/projects, not just one. Tests **6/6**.

- **Product Manager officer (S-5)** — when a build deliberately halts on a product call, the PM
  **DECIDES** if it's within mandate (the ticket continues with the decision baked in) or **ESCALATES**
  to you with a proposed solution (parks + comments). Can recruit its own soldiers. Tests **11/11 + 8/8**.

- **Worktree lock** — a per-app file lock (`fcntl.flock`) stops two runs of the same app from sharing
  one worktree and clobbering each other (the AUTO-14 data-loss root cause); a busy app is **skipped,
  not corrupted**. Tests **6/6**.

- **Default run = live; dry-run opt-in** — the cockpit/CLI default is now a real build+merge; dry-run
  is an explicit opt-in, and stale "dry" state is cleared at run-end so the UI never lies. Tests **7/7**.

- **Recon squads — officers recruit soldiers autonomously** — the read-only patrol officers (Scout,
  Provost Marshal, Quartermaster) can now do what the Field Engineer already did: when a surface is
  big enough, field a squad of read-only SOLDIERS (one per area) and synthesize a single report in the
  officer's own format. Each officer **decides for itself** — a cheap read-only planning pass returns
  `SOLO` for a small/atomic surface (no soldiers spent) or a non-overlapping split for a big one;
  fewer than 2 slices ⇒ solo. New `recon.py` mirrors `squad.py`'s discipline: **solo is the default**,
  the **same `delegation_enabled` switch** arms both the build squad and the recon squads, **fail-safe**
  degrades to solo on any planning/soldier hiccup, soldiers are read-only + sequential. Per-ticket
  Reviewer/security-gate stay solo (per-ticket cost). Tests: parse + solo + delegated + autonomy +
  fail-safe (**13/13**); full sweep **197/197**.

- **Cockpit "Deploy → main" button + dev-ahead indicator** — promote DEV→main from the War Room instead
  of the terminal. The top bar shows how far DEV is ahead of main (= approved changes not yet on the
  24/7 server) as a count badge; one click (with confirm) fast-forwards main and pushes, and the server
  auto-deploys it. Mac-only by construction (gated on `GENERAL_COCKPIT_PROMOTE=1`, which only the Mac
  launcher sets — the read-only server never shows it); ff-only (reports instead of forcing on
  divergence), always returns the working tree to DEV, blocks while a build is active. Also a header
  machine-name label (`mac` / `server`) so the two cockpits are tellable apart. Tests: gate + ahead-count
  + ff-merge/push + return-to-dev + idempotency (**10/10**); full sweep **177/177**.

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
  it files tickets), or on a cadence via the server crontab that `scripts/install-server-cron.sh`
  installs (its weekly `0 9 * * 1 … ./general patrol` entry — Mon 09:00). Verified by a dedicated
  harness (13/13); dashboard suite 66/66.

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
- **Officers** — Adjutant (S-1/HR) · Field Engineer (Builder) · Inspector General (Reviewer) · Scout (S-2/QA) · Provost Marshal (Security) · Quartermaster (S-4/DevOps) · Sentinel (S-3/Integration & rollback) · Drillmaster (Doctrine) · Product Manager (S-5) — chaired by The General.
- **Autopilot** — always-on worker: resume In Progress, else take top To Do (assignee-pinned to you), with a park-guard so it never spins on a stuck ticket; KeepAlive launchd.
- **Daily council** — 10:00 muster, briefing to Telegram, transcript saved, escalates only Commander-level calls.
- **Two-way Telegram** — the General answers you; your replies become standing guidance.
- **Self-improvement** — `drill --apply` writes approved officer/squad edits (backed up).
- **Self-staffing** — Adjutant proposes/executes hires (HR-gated): soldiers + junior officers under each major.
- **Self-filing** — Scout/Provost/Quartermaster file their own findings as Jira tickets (`--file`, de-duped, assigned to you).
- **Find → fix → verify loop** — proven end-to-end (Provost found AUTO-12 → built → re-gated → merged → QA).
- **Builder unattended** — loads no repo settings so nested `ask` rules never block writes; reads CLAUDE.md/.claude/rules for conventions.

## Next — in priority order

The unit is mature: it self-hosts 24/7, finds → files → fixes, self-improves, and self-staffs. The
next phase is **hardening the autonomy we now have** before widening it. Priority order below.

### P0 — harden what we just loosened (safety mechanisms)

1. ~~**Structural tool-call guardrail (a hard denylist).**~~ ✅ **Shipped 2026-06-20** — `guard.py`
   PreToolUse hook blocks secret/.env/CI writes + destructive shell (`rm -rf`, force-push, push to
   main, `DROP`/`TRUNCATE`, …) on the builder + soldiers, in code; 53/53 tests.
2. ~~**Commit the unit's OWN test suite + CI.**~~ ✅ **Shipped 2026-06-20** — `tests/` (41 harnesses /
   505 checks) + `tests/run_all.py` + a GitHub Actions workflow on every push/PR. (Follow-up still open:
   have the server's `self-update.sh` refuse to deploy a `main` whose CI is red — a 1-line `gh` check.)
3. ~~**Cost governor v2 — rolling budget + auto-pause + cockpit panel.**~~ ✅ **Shipped 2026-06-20**
   (token ledger + daily ceiling + auto-pause + 80% alert + Token-usage window).
4. ~~**Post-merge DEV health gate + auto-revert.**~~ ✅ **Shipped 2026-06-20** as the **Sentinel**
   officer (post-merge suite on landed DEV + forward-only auto-revert if it breaks).

### P1 — capability & throughput

5. ~~**Ticket-readiness gate (PM, pre-build).**~~ ✅ **Shipped 2026-06-20** — `readiness.py` hands back a
   no-AC + thin-description ticket before the build (opt-in `readiness_gate`); 16/16 tests.
6. ~~**Parallel multi-app builds.**~~ ❌ **Dropped 2026-06-20** (Roman's call) — running two products at
   once is unnecessary and invites a mess. The unit works **one project at a time**; you switch projects
   (VS Code-style, with Recent projects) when you're done with one. Throughput per project, not across.
7. ~~**Failure forensics + auto-post-mortem.**~~ ✅ **Shipped 2026-06-21** — `forensics.py` classifies
   every failed run into a taxonomy (under-specified / too-big / merge-conflict / gate-fail / security /
   product-blocker / infra / transient) with a recommended fix each, surfaces repeat offenders, and
   **auto-writes a deterministic post-mortem** to `postmortems/<TICKET>.md` once a ticket fails
   `postmortem_after` (3) times. Cockpit `/forensics` page + `general forensics`. 37/37 tests.
8. ~~**Memory consolidation + learning from rejections.**~~ ✅ **Shipped 2026-06-21** — `consolidate.py`
   dedups/prunes the Lessons log (UNIT.live.md), and scans FAIL reviews to cluster recurring rejection
   themes (tenant filter, missing tests, typing, …); when one recurs across ≥2 tickets it folds a lesson
   into the log + surfaces a drill proposal. Runs free after every Scribe pass; `general consolidate` +
   cockpit /memory surface. 23/23 tests. **This closes P1.**

### P2 — reach & polish

9. ~~**Cockpit Jira-connection UI.**~~ ✅ **Shipped 2026-06-20** — `connections.py` + a `/jira` cockpit
   panel: pick the Jira a project uses, quick-connect a new one (name + site URL + email + token,
   stored in a gitignored `jira_connections.json`, masked in the UI), switch projects from the same
   page. The adapter prefers an assigned connection, falling back to `email_env`/`token_env`. 37/37 tests.
10. ~~**One-command new-product onboarding.**~~ ✅ **Shipped 2026-06-21** — `onboarding.py` +
    `general onboard <name> <repo> [--jira <id>] [--write]` + a cockpit **➕ Product** page: detects the
    repo's base/protected branches, refuses duplicates, backs up `config.yaml` and does a surgical insert
    under `apps:` (comments survive), and optionally wires a saved Jira connection to the new project.
    Preview-by-default. 33/33 tests. (#11 below — click-to-onboard discovered repos — now just wires the
    "Found nearby" list into this.)
11. ~~**Runnable discovered repos.**~~ ✅ **Shipped 2026-06-21** — git repos found beside your configured
    ones now appear on the **➕ Product** page as click-to-onboard chips: clicking one pre-fills the
    onboard form (name + path + auto-detected branches), so a discovered repo is one click + confirm
    away from being a worked project. Reuses the onboarding engine. 10/10 tests. **This closes the
    backlog.**

### Known debt (surface, don't forget)

- Two scratch harnesses are stale (`patrol_test`, `wr_test`) — a fake-signature drift and a missing
  `sys.path`; fold the fixes in when #2 promotes the suite into the repo.
- Full Jira browser-OAuth is deferred (token env vars work today).

## Notes

- The General self-hosts on Roman's Mac (Claude Max login, no API key). Cowork edits the source; Roman runs it.
- Repo: github.com/roman-berlin/Elite-Unit (private), `main` (prod, the VPS runs it) + `dev` (active dev on the Mac).
- Roman runs separate products: Automatixy CRM, SignalDesk, MQL5 EAs — the war room's project switcher serves this.
