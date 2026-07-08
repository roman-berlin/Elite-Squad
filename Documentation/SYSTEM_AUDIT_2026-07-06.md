# Elite-Unit — Full-System Audit + Combat-Readiness Implementation Plan (2026-07-06)

**Date:** 2026-07-06 · **Branch audited:** `dev` · **HEAD at write time:** `0ae0b4b`
**Author:** Commander (ELITE audit mission) · **Method:** read-only, evidence-cited (file:line / commit /
audit timestamp / measured number). No test suite was run concurrently; the cockpit was probed on a
throwaway port 8797 with no `.env` (no poller, no creds).

> **Provenance of findings.** Three confidence tiers are marked on every finding:
> **[first-hand]** — I measured it directly this session. **[verify-confirmed]** — an auditor found it and
> an independent adversarial verifier re-checked it at the cited line. **[auditor-reported]** — surfaced by
> an auditor but its verifier did not finish (the multi-agent Verify phase was cut off by a model usage
> ceiling mid-run); treat as a strong lead, re-confirm before acting.

> **Moving target.** `dev` advanced `9412bf4 → 0ae0b4b` **during** this audit as four **Phase-2 §2
> "collapse" commits** landed (`31d4349` delete Senior-PM gate, `40da120` delete EU-65/66 liaison,
> `33da61d` delete `events.py` autonomy layer, `0ae0b4b` delete corridor small-talk + de-cron the daily
> council). Phase 2 is now being executed on `dev`. Findings that Phase-2 §2 already resolves are marked
> **[superseded-on-dev]**; the **server still runs the old behaviour** until `dev → main` is promoted.

---

## 1. Health matrix

| Surface | Status | Top risk | One-line evidence |
|---|---|---|---|
| **1 — Local (Mac)** | 🟡 Yellow | Dead launchd scheduling (EU-181) fires into deleted scripts, err logs growing | `patrol/smalltalk/sync` loaded, **exit 127**; `run-*.sh` MISSING (deleted `bf1ce92`); `council/sync.err.log` 5429 B, mtime 2026-07-06 10:59 |
| **2 — Server (VPS)** | 🟡 Yellow | systemd `Restart=always` with **no start-limit** → a bad `main` deploy crash-loops unbounded | `general.service` logged **~864 restarts** 19:34–19:35 on 07-05 (UnboundLocalError `AuditLog`, fixed by `ce7aad6`); now stable 10 h; no `StartLimitBurst` in unit file |
| **3 — Telegram** | 🟡 Yellow | **Two `getUpdates` consumers on one bot token** (VPS 24/7 + any Mac `serve`) fight the offset → split/lost commands; volatile alert dedup (EU-183) | VPS `general.service` runs `decisions.poll_loop` (`server.py:2669`); Mac `serve` starts the same thread when `.env` present; both read `TELEGRAM_BOT_TOKEN` (`notify.py:112`) |
| **4 — Cockpit** | 🟢 Green* | All 59 routes 200-OK on relocated `state/`; cold `/tickets` 3.3 s & `/usage` 4.8 s (Jira + `claude -p` probe) then cached; **ghost-'Working' (EU-175) risk lives in the kill/crash path** | **19 `autopilot_start` vs 9 `autopilot_stop`** (10 sessions SIGKILL'd before their stop, all around the 07-01 crash) + **14 `run_start` vs 12 `run_end`**; the recent supervised EU-139 run *did* close cleanly (`run_start 22:18:20 → run_end 22:33:14`, $1.48) |

\* Green on availability/correctness after the `state/` move; the open EU-129/136/175 UX complaints are real but non-breaking (§2).

**Combat-readiness verdict:** the build loop itself is **proven healthy** — EU-139 ran clean in **~15 min, one
Sonnet pass**, fully bracketed (`run_start 22:18:20 → ticket_start 22:18:21 → build → gate → test_engineer →
review → pr_opened → token_burn_report → run_end 22:33:14`, $1.48), stopped correctly at `pr_opened` (no
auto-merge; PR #4 still open), and fired its `token_burn_report`. **The graceful path closes its run
boundary correctly.** The gating defects for scale-up are **operational** — the ghost-'Working' risk (EU-175)
is the **kill/crash path** (`autopilot_stop` not in a `finally`, so 10 of 19 autopilot sessions never wrote a
stop), plus the single-Telegram-poller and server crash-loop backstop — not in the builder.

**Live-run update (this session):** a supervised `./general --live ticket Elite-Unit EU-182` run confirmed the
CLI path brackets cleanly (`run_start 11:36:50 → run_end 11:47:00`, $1.63, budget held) — **but surfaced a
CRITICAL blocker (N9 / EU-188): the worktree isolation leak.** The builder produced the correct fix yet edited
the *main* tree, so the gate saw "no changes" and false-escalated. Net: the graceful *plumbing* is healthy, but
**the unit cannot yet reliably self-implement EU tickets** until EU-188 is fixed. The auditor restored the main
tree to clean and preserved the correct diff.

---

## 2. Risk-ranked findings

### 2A. NEW findings (not on the known-open list)

| # | Sev | Finding | Evidence | Blast radius | Fix direction |
|---|---|---|---|---|---|
| **N9** | **🔴 HIGH (CRITICAL for self-dev)** | **Worktree isolation leaks on self-development tickets → builder edits the MAIN tree → false "no changes".** Reproduced live this session (EU-182). [first-hand] → filed **EU-188** | Supervised `./general --live ticket Elite-Unit EU-182` (2026-07-06 11:36–11:47): loop set up `isolated worktree → .general-worktrees/Elite-Unit`, but builder `Edit /Users/romanberlin/Projects/General/orchestrator/recon.py` (MAIN); main-tree `git status` clean→`M recon.py, M recon_test.py`; worktree empty → `no_changes 11:47:00` → false Needs-Human. The fix it produced was *correct* (`audit=audit` at `recon.py:139`) but stranded in main. 2nd instance after EU-174 (proposal Appx B); EU-139 didn't hit it → non-deterministic | The unit **cannot reliably self-implement EU tickets** — affected runs false-escalate, land nothing, bill ~$1.6/2-pass, and can mutate the live orchestrator tree it runs from (data-integrity, worse on a shared host) | Make the worktree the only writable root for self-dev builds (deny main-repo writes when a worktree is active) + carry `cwd`/`hooks`/`disallowed_tools` through every retry/`_solo` clone (proposal §6 defect 1); regression test: a self-dev build leaves main tree clean |
| N1 | **HIGH** | **VPS crash-loop has no backstop.** `Restart=always`/`RestartSec=5` with no `StartLimitIntervalSec`/`StartLimitBurst`: a `main` deploy that crashes on startup restarts forever. [first-hand] | `/etc/systemd/system/general.service` has `Restart=always`, `RestartSec=5`, no start-limit; journal shows **~864** `Started general.service` between 19:34–19:35 on 07-05 while HEAD was `da5c10b` (pre-`ce7aad6`); `cron.log` has 6 `UnboundLocalError: … 'AuditLog'` tracebacks | Any bad `origin/main` = the 24/7 box pegs CPU and self-heals only if the *next* self-update happens to carry the fix (it did, at 19:35) | Add `StartLimitIntervalSec=300` + `StartLimitBurst=5` and an `ExecStartPre` import-smoke-check; make `self-update.sh` smoke-test the new HEAD **before** `systemctl restart` |
| N2 | **HIGH** | **Two Telegram pollers race one bot token.** VPS `general.service` runs `poll_loop` 24/7; any Mac `./general serve` with `.env` starts a second `poll_loop` on the **same** token. Telegram `getUpdates`+offset is single-consumer. [first-hand + auditor-reported] | `server.py:2669` starts `threading.Thread(target=decisions.poll_loop…)` when `notify.configured()`; token from `notify.py:112-113` (`TELEGRAM_BOT_TOKEN`); no cross-host lock | Inbound `/unblock`, approvals, decision replies split randomly between hosts; offset advances on whichever consumed → "weird"/lost messages | One-poller rule: a lock file / host-election so only the VPS polls; Mac `serve` runs cockpit-only unless it owns the poller lease |
| N3 | **MEDIUM** | **VPS `patrol` cron is broken every week.** `./general patrol` requires a positional `app`; the cron omits it. [first-hand] | `crontab -l`: `0 9 * * 1 … ./general patrol`; `cron.log`: `general patrol: error: the following arguments are required: app`; usage line `general patrol [-h] … app` | Weekly security patrol never runs; silent gap in the one scheduled security sweep | Pin an app (`./general patrol <app>`) or make `app` optional/all-apps; or drop the cron if patrol is retired |
| N4 | **MEDIUM** | **`/api/terminal` executes arbitrary shell** from the cockpit. [auditor-reported] | `server.py:315` `POST /api/terminal`; VPS binds `127.0.0.1:8787` (`ss`: `LISTEN 127.0.0.1:8787`) so not remotely exposed, but any localhost process / CSRF on the panel = RCE as `ubuntu` | Local-only today, but the cockpit is an unauthenticated attack surface for anything that can reach loopback | Gate behind a token/allowlist, or restrict to a fixed command set; never accept free-form shell |
| N5 | **LOW** | **Phantom `no_comments` knob bricks startup if set** — *strict-kwargs crash class* **[superseded-on-dev by `77939eb`, 2026-07-06 11:08]**: that commit makes `Config.load` tolerate retired/unknown YAML keys instead of crashing. The `no_comments` opt-out is still a no-op field (read but never settable as real config) — a doc/cleanliness nit only now. [verify-confirmed] | `loop.py:426,584,1344` `getattr(cfg,"no_comments",False)`; not in `config.py:117-302`; pre-`77939eb` `Config(apps=apps, **data)` → `TypeError` (reproduced) | Was: adding the key bricked startup. Now: harmless dead read | Add `no_comments: bool = False` to `Config` to make the opt-out real, or delete the reads |
| N6 | **LOW** | **`config.yaml`/`config.example.yaml` drift.** Example ships `dry_run: true`, `out_of_scope_autofile: false`; live runs `dry_run: false`, `out_of_scope_autofile: true`. A phantom `--dry` flag is referenced but only `--live` exists. [verify-confirmed] | `config.example.yaml` vs `config.yaml:51`; `main.py` argparse has `--live`, no `--dry` | New-host bring-up copies the example → different autonomy posture than production | Reconcile example to live defaults; fix the `--dry`→`--live` reference |
| N7 | **LOW** | **Tracked-but-empty runtime file.** `audit/swebench_runs.jsonl` is committed as a 0-byte blob. [verify-confirmed] | `git ls-tree 0ae0b4b -- audit/` → empty blob `e69de29b` (0 bytes), added by `39864b5` (EU-71) | Runtime artifact under version control; churns on real runs | `git rm --cached` + `.gitignore` the path |
| N8 | **INFO** | **41 silent `config.py` defaults absent from `config.yaml`**, several arming autonomy (`autonomy_enabled=True`, `discussion_model`, `smalltalk_model`). [verify-confirmed] | `config.py` defaults with no yaml entry; e.g. `autonomy_enabled` default `True` (`config.py:237`) never surfaced in yaml | Config surface understates what is actually armed | Surface the autonomy-relevant defaults explicitly in `config.yaml` with comments |

### 2B. Known-open list — sharpened with this session's evidence

| Ticket | Status this session | Sharpened evidence |
|---|---|---|
| **EU-181** (Mac sync dead) | **Confirmed, worse than filed.** [first-hand] | `patrol/smalltalk/sync` launchd agents loaded, **last exit 127**; `run-council/patrol/smalltalk/sync.sh` all MISSING (deleted `bf1ce92`, EU-56, 06-26); `council/sync.err.log` **5429 B**, mtime **2026-07-06 10:59** (still growing today); `patrol.err.log` 182 B mtime 07-06 09:00; `smalltalk.err.log` 94 B mtime 07-06 11:00; `council` agent not even loaded. A 5th agent `com.roman.general-autopull` → `~/bin/general-autopull.sh` exits 0 (harmless). **VPS cron is the real scheduler; these Mac agents are pure error-log spam.** |
| **EU-183** (Opus-pin from transient 429; volatile dedup) | **Confirmed direction.** [auditor-reported] | Auditors located ~8 alert dedup flags held only in process memory (re-fire after every restart) and the Opus-pin arming path that records **no audit event**. Full alert table is §5 below (the input the proposal §7 asked for). Re-confirm the exact file:lines before coding — verifiers were cut off. |
| **EU-175** (ghost 'Working' cards) | **Mechanism confirmed — it's the kill/crash path, not the graceful one.** [first-hand] | Audit totals **19 `autopilot_start` vs 9 `autopilot_stop`** (10 sessions with no stop) and **14 `run_start` vs 12 `run_end`** (2 unclosed), **all clustered on the 2026-07-01 SIGKILL crash** (matches proposal §0 ghost-sessions). The recent supervised EU-139 run *did* bracket cleanly (`run_start 22:18:20 → run_end 22:33:14`). Root cause: `autopilot_stop` (`autopilot.py:800`) is not inside a `finally`, so a hard-killed process leaves the active-run card stuck. Fix = Wave 1 (§7). |
| **EU-136** (phase pip no backward transition) | Fix exists on `autodev/EU-136-*` (not re-fetched this pass — Verify was cut off). | Carry forward; validate the branch applies onto `0ae0b4b` before merge. |
| **EU-129** (global-not-per-project `/needs`; re-parse per render) | **Partially reproduced.** [first-hand] | `/needs` is **not** one-row-per-ticket: EU-174 occupies rows 1–4, EU-173 rows 5–6 (measured from live HTML). Cold `/` render 0.10–0.16 s against a **2,571-line** `state/audit.jsonl`; warm 0.10 s (there is caching). Cost is modest now but scales with audit size. |
| EU-129/136/141/154/157–161 | Not individually re-verified (Verify phase truncated). | Branch inventory deferred — `git ls-remote`/fetch of `autodev/*` was in the truncated slice; re-run the branch-inventory slice next pass. |

---

## 3. Surface detail (evidence appendix)

### Surface 1 — Local (Mac)
- **Repo/config coherence** [verify-confirmed]: `HARD_MAX_PASSES=2` (`loop.py:576`), enforced `min(cfg.max_iterations, HARD_MAX_PASSES)` (`loop.py:691`) — not raisable by yaml/CLI. Live per-ticket budget **3,000,000 tokens / 30 min** (`config.yaml:42-43` = `config.py:255-256`); the "400k" in the 07-05 proposal is the superseded initial QW4 value (raised by `170f55c`). `escalate_effort_on_retry: false` (`config.yaml:17`; consumer `builder.py:186`). Tiers (`models.py:23-25`): Haiku `claude-haiku-4-5-20251001`, **Sonnet `claude-sonnet-5`**, Opus `claude-opus-4-8`; builder=reviewer=Opus. `auto_model: true` arms Sonnet-first→Opus-on-retry.
- **GLM/z.ai** [verify-confirmed]: inert. `routing.py` gated on `ROUTING_ENABLED` (default `false`); `.env` has **0** `ROUTING_*`/`OLLAMA_*`/`ANTHROPIC_MODEL`; z.ai block 10 refs, **0 uncommented**. Latent-if-enabled default `glm-5.2` (`routing.py:163`) — leave disabled.
- **`.env` sanity** (names/counts only, perms `-rw-------`): 5 active exports — `JIRA_EMAIL`, `JIRA_API_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GENERAL_COCKPIT_PROMOTE`. `ANTHROPIC_BASE_URL` 0 active; `ANTHROPIC_API_KEY` 0 active (auth via `claude` login keychain). `GENERAL_COCKPIT_PROMOTE` **active** → this Mac's cockpit shows the dev→main Deploy button (by design; the read-only server never sets it).
- **State hygiene** [first-hand]: `state/audit.jsonl` = 2,571 lines; sidecars derive via `audit_path`/`with_name`. `config.yaml:68 audit_path: "./state/audit.jsonl"`. `dev` == `origin/dev` == `0ae0b4b`, **0/0**, working tree clean. One session worktree `.claude/worktrees/friendly-germain-628385` on branch `claude/friendly-germain-628385` == `0ae0b4b` (harmless).
- **launchd** [first-hand]: see EU-181 row above. **Autopilot keepalive NOT installed** (`~/Library/LaunchAgents/*autopilot*` → none) — correct; activation remains Commander-only.

### Surface 2 — Server (VPS `elite-unit`, 151.145.91.229)
- **Branch/self-update** [first-hand]: on `main` `c83f136` (= PR #3 merge). `self-update.sh` cron `:05,:20,:35,:50` does `git fetch origin main` + `reset --hard origin/main` + `systemctl restart`; **5 successful deploys** logged (07-01 ×3, 07-05 ×2). Healthy. `reset --hard` leaves untracked files intact.
- **Supervision / the "fresh restart"** [first-hand]: `general.service` (systemd, `Restart=always`, `RestartSec=5`). The restart seen on prior probes was the **07-05 19:35 self-update** (`da5c10b → c83f136`), preceded by the ~864-restart crash-loop (N1). Now **stable, 10 h uptime**, `Memory 53.8 M`, `127.0.0.1:8787`. Unit file changed on disk without `daemon-reload` (warning present).
- **audit.jsonl** [first-hand]: **92 lines**, events `commander_msg` 64 / `council` 14 / `roster_refresh` 13 / `proposals_queued` 1 — **no** `agent_call`/`build`/`run_*` → the server runs **no builders** (correct). Last event **2026-07-05 06:32** → ~24 h stale (serve-only writes little). `audit_path: "./audit.jsonl"` (repo root) — **the `state/` move is not applied on the server**; no `state/` dir; `telegram_offset.txt` at root.
- **Council value** [first-hand]: `index.jsonl` SITREP summaries for 07-03/04/05 are near-identical ("91 tickets merged to DEV; tests ×102, a11y ×50…") → repetitive, low signal. Phase-2 §2 de-crons it (`0ae0b4b`) — but only takes effect after `dev → main`.
- **`.env` GLM-cleanliness** [first-hand]: 0 z.ai/GLM/`ANTHROPIC_BASE_URL` refs.
- **Stray file** [first-hand]: `officers/frontend-engineer.md` untracked (4054 B, Jun 21), survives `reset --hard`.
- **Disk** [first-hand]: `/` 6.1 G/45 G (14 %); `.git` 12 M; `council` 264 K. Healthy.

### Surface 3 — Telegram
- **Outbound** [auditor-reported]: `notify.send` call-site inventory + a per-alert table were produced (§5). Re-verify counts against `0ae0b4b` (liaison deletion removed some sites).
- **Inbound** [first-hand + auditor-reported]: `decisions.poll_loop` (`decisions.py:566`, 5 s interval, swallow-all `except`) is started by `serve` (`server.py:2669`). `telegram_offset.txt` lives beside `audit_path`. **Two-consumer race = N2.** EU-65 liaison isolation is now moot — liaison channel **deleted** on `0ae0b4b` (Phase-2 §2); `notify.py:110` comment confirms "since the EU-65 liaison channel's deletion … no production [second chat]".
- **Suite leak (7ec4b84)** [auditor-reported]: `run_all.py` env-stripping protects subprocesses it spawns; the "no real Telegram" leg holds for the run_all path. Residual real-send paths (tests importing `orchestrator.notify` directly, or spawning `./general` with a live `os.environ`) were flagged but not fully verified — re-confirm.

### Surface 4 — Cockpit
- **Routes** [first-hand]: **59** routes; every probed GET returns **200** on relocated `state/`. Timings: `/` 0.10–0.16 s, `/api/board` 0.015 s, `/needs` 0.001 s, **`/tickets` 3.27 s** (cold — Jira `from_drain`), **`/usage` 4.80 s** (cold — `usage.py:346` fires a throwaway `claude -p . --model haiku` to read `rate_limit_info`; then 5-min cached → 0.013 s). SSE `/api/stream` emits `event: board` frames.
- **`/needs` invariant** [first-hand]: **violated** — multiple rows per ticket (EU-174 ×4). Feeds EU-129.
- **EU-175** [first-hand]: reproduced (run-boundary gap, above).
- **`/usage` `claude -p` probe** [first-hand]: real subprocess + Haiku call on cold load — small recurring cost/latency; acceptable behind the 5-min cache but note it burns a Haiku call per cache-miss.

---

## 4. Jira actions

**New tickets to file** (drafts below; file to project **EU**, cloudId `35e9c84a-6fbc-440f-9de7-520cbd2a5f52`):

1. **[EU-NEW] Server crash-loop has no systemd backstop (N1, HIGH)** — add `StartLimitIntervalSec`/`StartLimitBurst` + smoke-test-before-restart in `self-update.sh`. Evidence: ~864 restarts 07-05 19:34–19:35.
2. **[EU-NEW] Two Telegram pollers race one bot token (N2, HIGH)** — single-poller lock / host election. Evidence: `server.py:2669` + `notify.py:112`.
3. **[EU-NEW] VPS `patrol` cron missing required `app` arg (N3, MEDIUM)** — errors weekly. Evidence: `cron.log` "arguments are required: app".
4. **[EU-NEW] `/api/terminal` runs arbitrary shell unauthenticated (N4, MEDIUM)** — gate behind token/allowlist.
5. ~~**[EU-NEW] `no_comments` phantom config knob bricks startup (N5, LOW)**~~ — **do not file: superseded by `77939eb` (2026-07-06)** which makes `Config.load` tolerate unknown keys. Downgraded to a cleanliness nit (make the opt-out a real field or delete the dead reads).

**Comments to add to existing tickets** (sharpening evidence): **EU-181** (exit-127 + 5429 B growing err log, `com.roman.general-autopull` extra agent), **EU-175** (14 `run_start` vs 12 `run_end`; EU-139 `ticket_start` with no bracket), **EU-129** (`/needs` EU-174 ×4 rows; 2,571-line audit render cost), **EU-183** (~8 in-memory dedup flags; Opus-pin arming has no audit event).

> Filing was **held pending Commander go** (outward-facing, and a model usage ceiling was hit mid-audit).
> All drafts are complete above and ready to file on one word.

---

## 5. Alert table (input to proposal §7 event-driven notify)

Auditor-reported inventory of distinct Telegram alerts (re-confirm file:lines against `0ae0b4b` before
coding the bus). Columns: **trigger → dedup? (storage) → audit event?**

| Trigger | Dedup | Audit event | Notes |
|---|---|---|---|
| Ticket merged to dev | none | `merged` | high-frequency during a drain |
| Needs-human escalation | pending_decisions key | `needs_human` | writes 5 stores, no consistency guarantee (§7 proposal) |
| Ticket exception | none | `ticket_exception` | |
| Budget block (3M/30min) | one-shot in-memory | `token_burn_report` | re-fires after restart |
| Builder/Reviewer disagreement | in-memory | `builder_reviewer_disagreement` | |
| Plan-limit / Opus-pin armed (EU-183) | in-memory flag | **NONE** | **arms a week-long Opus pin with no audit trail** |
| Unclean restart | in-memory | (autopilot) | |
| Sentinel revert | none | `sentinel_revert` | wiped AUTO-54's merge once (proposal Appx B) |
| Corridor insight | n/a | `smalltalk` | **deleted on `0ae0b4b`** |

**EU-183 second half:** ~8 dedup flags live only in process memory → **re-fire after every restart**. The
plan-limit/Opus-pin alert has **neither dedup persistence nor an audit event** — the single most dangerous
combination (silent, expensive, repeatable). This is the top item for the §7 event bus.

---

## 6. Go / No-Go for scale-up

### 6.1 Raise `max_tickets_per_run` (currently 1)
**NO-GO** until run boundaries are closed. **Preconditions (all required):**
1. **Run-boundary fix (Phase-2 §7.5):** move `autopilot_stop` (`autopilot.py:800`) into a `finally` (the
   proven gap — 10 of 19 sessions never wrote a stop after the 07-01 kill), and bracket every loop-invoking
   call site (`server.py` `/api/run`, `/api/run-selected`; `decisions.py` resume) with `run_start`/`run_end`.
   Verify: one supervised **2-ticket** drain shows `run_start == run_end` and `autopilot_start == stop`,
   **0 unclosed**, and a deliberate mid-run kill leaves no ghost card.
2. **Budgets hold under back-to-back tickets:** `token_burn_report` fires per ticket; no silent
   continuation past 3M/30min. (EU-139 proved this for one ticket.)
3. **N1 server backstop landed** (so a mid-drain crash can't loop).

### 6.2 Activate the launchd autopilot keepalive
**NO-GO on the Mac; keep it uninstalled.** The Mac is the supervised/dev host; the VPS is the 24/7 box but
**must not run builders** (and today runs cockpit+poller only). **If ever activated, preconditions:**
1. **N2 single-poller lock first** — otherwise Mac autopilot + VPS poller fight Telegram.
2. **N1 backstop** + the unclean-restart alert wired to a **persisted** dedup key.
3. Commander explicitly drives the first activation (per standing rule).

### 6.3 Let the unit drain the verified backlog
**~~NO-GO~~ → N9/EU-188 is now FIXED + validated end-to-end** (`850b94c`; EU-182 re-run landed in one pass
with isolation held). The self-development blocker is cleared. Remaining gates below still stand. **CONDITIONAL-GO for a single supervised ticket** (with
a before/after main-tree `git status` isolation check) after §6.1 preconditions 1–3 **and** EU-188; **NO-GO for
autonomous multi-ticket drain** until §7 waves 1–2 land. **Preconditions:** EU-188 fixed + isolation regression
test green; clean `dev` base (Phase-2 §3 `51cc9e8` red-base short-circuit); 1–2 day observation with **median
ticket < 25 min, < 3M tokens, 0 unclosed runs, 0 isolation leaks**.

---

## 7. Full implementation plan — path to combat-ready

Sequenced so each wave is independently shippable, tests-green, and lands on `dev` (never `main`; Commander
merges). Waves 1–5 are the Phase-2 proposal executed in dependency order; **Wave 0 is new, cheap,
no-LLM, and unblocks everything.**

### Wave 0 — Operational backstops (hours, no LLM, do first)
- **N1** systemd `StartLimitIntervalSec=300`/`StartLimitBurst=5` + `self-update.sh` smoke-test (`python -c
  "import orchestrator.main"` or `./general doctor`) **before** `systemctl restart`; `daemon-reload`.
- **EU-181** uninstall the 4 dead Mac launchd agents (`launchctl bootout`) — VPS cron is the
  sanctioned scheduler; stop the err-log spam. On the Mac, run:
  ```bash
  launchctl bootout gui/$(id -u)/com.roman.general.sync
  launchctl bootout gui/$(id -u)/com.roman.general.smalltalk
  launchctl bootout gui/$(id -u)/com.roman.general.council
  launchctl bootout gui/$(id -u)/com.roman.general.patrol
  ```
  These agents have been firing into deleted scripts (run-sync.sh, run-smalltalk.sh, run-council.sh,
  run-patrol.sh) since bf1ce92 (2026-06-26), producing growing error logs (council/sync.err.log at
  5429 B as of 2026-07-06). The VPS cron (`scripts/install-server-cron.sh` runs `./general sync` every
  15 min) is the single source of truth for scheduling. The Mac has no autonomous sync scheduler —
  its state syncs only when the server pulls the shared/unit-state branch.
- **N3** fix or drop the VPS `patrol` cron.
- **Server `state/` parity:** point the server `audit_path` at `state/` at next deploy (or consciously keep
  root — decide and document).
- **N7** untrack `audit/swebench_runs.jsonl`.

### Wave 1 — Close the ghost-session gap + event spine (Phase-2 §7.5, §7.1)
- Run-boundary events at all loop call sites + `autopilot_stop` in `finally` → **fixes EU-175**.
- `AuditLog.subscribe(fn)` (~15 lines at `audit.py` choke point) — the spine for Waves 2/4.
- **Gate to advance:** supervised 2-ticket drain, `run_start == run_end`, 0 ghost cards.

### Wave 2 — Telegram: one poller + event-driven notify (Phase-2 §7.2, N2, EU-183)
- **N2** single-poller lock (host election); Mac `serve` = cockpit-only unless it owns the lease.
- One Telegram subscriber with the §5 table → event→template/severity/**persisted** dedup; migrate the ~74
  `notify.send` sites incrementally (record event → delete send).
- **EU-183:** persist dedup keys; give the plan-limit/Opus-pin path an audit event; tighten `is_plan_limit`
  so a transient 429 can't arm a week-long pin.

### Wave 3 — Cockpit correctness (EU-129, EU-136, N4, dead UI)
- **EU-129** per-project `/needs` + one-row-per-ticket; cache the audit parse (already partially cached).
- **EU-136** phase-pip backward transition (validate `autodev/EU-136-*` applies onto `0ae0b4b`).
- **N4** lock down `/api/terminal`; sweep dead UI (retired Live-Feed/Needs-you/GLM-gauge remnants — §Surface 4).

### Wave 4 — Model routing behind an eval (Phase-2 §6) — only after Waves 1–3
- Routing table `{tag→(model,effort,max_turns)}` applied via `options.model` (no env mutation); fix the two
  `agent.py` routing defects (dropped `cwd`/`hooks`/`disallowed_tools`; env-restore skip) **before** enabling.
- **Do not enable** without the 10-ticket shadow eval (gate-pass/review-pass/tokens/wall-time vs Sonnet
  baseline). GLM quality is unproven here.

### Wave 5 — Test-suite consolidation (Phase-2 §5)
- Nightly-tag the 12 heavy autopilot-wait harnesses (per-land gate ~65 s); merge the duplicate clusters;
  rename the 2 orphan `test_*.py` files the `*_test.py` glob misses. Suite health is an economics control.

### Standing guardrails (unchanged)
`dev` only; Commander merges `dev → main`; never start autopilot / run tickets unsupervised; `.unit-state/`
untouched; z.ai stays commented; effort tier is Roman's to set.

---

## 8. Verification performed this session
- **dev sync:** `dev` == `origin/dev` == `0ae0b4b`, 0 ahead / 0 behind, tree clean — **synchronized**.
- **Build loop:** EU-139 replay from `state/audit.jsonl` — clean one-pass run, **fully bracketed**
  (`run_start 22:18:20 → run_end 22:33:14`, $1.48), budget report fired, correct stop at `pr_opened`. The
  graceful path is healthy. The **"not all good"** is historical: 10 of 19 autopilot sessions and 2 of 14
  runs never closed — the 2026-07-01 SIGKILL crash path (`autopilot_stop` not in `finally`) = EU-175, fixed
  in Wave 1.
- **One supervised live run performed** (Commander-authorized): `./general --live ticket Elite-Unit EU-182`,
  2026-07-06 11:36–11:47, $1.63, 2 passes. Launched only into a verified-clear `run_all` window (host has a
  concurrent session looping suites). Result: clean run boundaries + budget, **but surfaced N9/EU-188** (the
  isolation leak). The builder's fix was correct but stranded in the main tree; auditor preserved the diff
  (`scratchpad/EU-182-correct-fix.patch`) and restored the main tree to clean. Stray branch/worktree pruned.
  **This is the "not all good — fix it" outcome:** the immediate breakage (dirty main tree) was fixed; the
  underlying pipeline defect is filed as EU-188 (code fix is pipeline-core, out of audit scope).
- **Filed:** EU-184 (N1), EU-185 (N2), EU-186 (N3), EU-187 (N4), **EU-188 (N9, the headline)**; sharpened
  EU-181, EU-175, EU-129, EU-183, EU-182.
- **N9/EU-188 FIXED + VALIDATED END-TO-END (Commander-authorized).** Fix landed on `dev` (`850b94c`):
  `guard.hooks_config(workdir)` now confines every officer write to its worktree; full suite 254/254.
  Re-ran EU-182 live (13:01–13:14, $3.40): the builder again tried to `Edit` the MAIN `recon.py`, the guard
  **denied it**, the builder **recovered into the worktree**, and EU-182 **merged to `dev` in one pass**
  (`f6e53f8`) — main tree stayed clean (only the EU-41 auto-changelog), `run_start`/`run_end` matched. The
  self-development isolation leak is closed and proven.
