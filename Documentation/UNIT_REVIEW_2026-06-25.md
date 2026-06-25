# The Elite Unit — Engineering Review 2026-06-25

Fresh, code-grounded review of the orchestrator (Python 3.12 / Flask / Jira / Telegram) on branch
`dev`. Every finding below was read in the current source and adversarially re-verified; line numbers
are against the working tree at review time (which includes the uncommitted EU-38 context-scope WIP in
`agent/builder/memory/recon/usage`). Companion to [SYSTEM_OVERVIEW.md](SYSTEM_OVERVIEW.md) and the
prior [REVIEW_BACKLOG.md](REVIEW_BACKLOG.md).

**Baseline:** `.venv/bin/python tests/run_all.py` → **102 harnesses, 1507 checks, ALL GREEN** (note F1
below — that "green" is not trustworthy). Plain `python3` crashes the suite (`ModuleNotFoundError:
requests`) — the deps live only in `.venv`; see F11.

**Prior review status (REVIEW_BACKLOG F1–F16).** The prior cycle has largely landed:
**F1, F2, F4, F5, F6, F7, F8, F9, F10, F11, F12, F13 are fixed** in current code (guard secret-read +
exfil + absolute-`rm -rf`; provost fail-closed; armed `daily_token_budget`; Telegram `copy.copy(cfg)`;
autopilot retry-before-park; guard-absent warning; `audit.jsonl` flock; PM-on-any-no-change; reviewer
parse-retry; decision-reply off the poll thread; `_run_lock` on the cockpit run routes). **F14** is now
documented + tested. **F3, F15, F16 remain partial/open** and resurface below where still live. The
findings here are **new** issues in the evolved code (the Sonnet-first ladder, `scrum.py`,
`test_engineer.py`, `cockpit_state.py`, the test harness), explicitly excluding the already-ticketed
**EU-38** (builder input-context bloat) and **EU-40** (officer-name single-source-of-truth).

---

## F1: Make the test suite actually fail — ~28 harnesses print FAIL but exit 0
- **Severity:** P0 (correctness/security)
- **Area:** `tests/run_all.py`, `tests/*_test.py`, `config.yaml`
- **Problem:** `run_all.py` decides pass/fail **solely on the harness subprocess return code**
  (`tests/run_all.py:36`); the `"X/Y passed"` line is parsed only to sum `total_checks`
  (`tests/run_all.py:33-35`). But ~28 harnesses use a soft tally helper that never raises and never
  exits non-zero: e.g. `tests/sync_test.py` defines `check(n, c, d)` at `tests/sync_test.py:23` and
  ends with `print(f"{passed}/{len(results)} passed")` + `print("RESULT:", "ALL GREEN" if … else "… FAIL")`
  at `tests/sync_test.py:106-111` — **no `sys.exit(1)`**. 33 harnesses contain no `sys.exit`/`SystemExit`
  at all; of those, the ~28 that print a `passed`/`FAIL` line (sync, sync_promote, squad, pm, council,
  events, recon, patrol, shipreview, worktree_lock, …) exit 0 even when their own `check()`s fail. A
  failing harness then *also* inflates the green tally (its partial `8/10` is added to `total_checks`).
- **Impact:** The unit gates **its own merges** on `gate_commands: ["python3 tests/run_all.py"]`
  (`config.yaml:60`) and `_land` re-runs that gate on the trial branch (`orchestrator/loop.py:705`). A
  self-modifying EU build that breaks behavior covered only by a soft-tally harness passes the gate
  showing **ALL GREEN** and auto-merges to DEV. The suite is the "guard that guards the guard"
  (`tests/run_all.py:2`) and it is failure-blind for ~1/4 of itself.
- **Fix:** Give every harness a single hard gate. Cheapest: have `run_all.py` also require the parsed
  `passed == total` per harness (treat a `"k/n passed"` with `k<n` as a failure regardless of exit
  code), and treat a harness that prints no count as suspect. Better: add `sys.exit(0 if passed==len(results) else 1)`
  to the shared `check()` epilogue used across these files.
- **Effort:** S
- **Depends on:** none

## F2: The guard cannot stop secret exfiltration — it has no concept of env vars or `jira_connections.json`
- **Severity:** P0 (correctness/security)
- **Area:** `orchestrator/guard.py`, `orchestrator/connections.py`, `orchestrator/notify.py`, `orchestrator/backlog/jira.py`
- **Problem:** `guard.is_dangerous` (`orchestrator/guard.py:129-159`) inspects only file paths and a
  fixed set of shell substrings. It has **no branch for reading the process environment** — yet every
  live secret is in `os.environ` (`TELEGRAM_BOT_TOKEN`/`CHAT_ID` at `orchestrator/notify.py:44-45`,
  `JIRA_API_TOKEN` in `orchestrator/backlog/jira.py`, `ANTHROPIC_API_KEY`), and the officer subprocess
  inherits the parent environment. So even on the *guarded* builder, `printenv`, `env`,
  `python -c "import os;print(os.environ)"`, or `curl --data "$(env)" …` sail straight through — the
  exfil denylist (`_EXFIL_DOTFILE`, `orchestrator/guard.py:96-104`) matches a dotfile/`*.key` payload,
  not a command-substituted env dump. Separately, `_SECRET_PATH` (`orchestrator/guard.py:23-30`) omits
  `jira_connections.json`, the **plaintext, multi-product** Jira-token store at the repo root
  (`orchestrator/connections.py:24,61-71`) — so `cat jira_connections.json` / `curl --data @jira_connections.json`
  is not blocked either.
- **Impact:** The Builder runs `permission_mode="bypassPermissions"` with the guard as the *only*
  code-level boundary (`orchestrator/builder.py:270-277`). A prompt-injected ticket (or a builder bug)
  can leak every product's Jira credentials and the Telegram control token, with the secret-read
  hardening (prior F1) intact but bypassed. The denylist is path-shaped; the secrets are environment-
  and one-file-shaped.
- **Fix:** Add an env-exfil branch to `is_dangerous`: block `Bash` commands containing `printenv`/`env`
  (bare or piped to a net tool) and `os.environ`/`process.env` dumps, and treat a net tool combined
  with a `$(…)`/backtick payload as exfil. Add `jira_connections.json` (and the audit/ledger files) to
  `_SECRET_PATH`. Residual risk remains — worktree isolation + not feeding untrusted tickets is still
  the real boundary — but close the obvious env hole.
- **Effort:** M
- **Depends on:** none

## F3: Install the guard hook on the read-only Bash officers (provost / scout / quartermaster)
- **Severity:** P0 (correctness/security)
- **Area:** `orchestrator/recon.py`, `orchestrator/provost.py`, `orchestrator/scout.py`, `orchestrator/quartermaster.py`
- **Problem:** `hooks=guard.hooks_config()` is attached on exactly three officers — `builder.py:277`,
  `squad.py:176`, `test_engineer.py:141` (the write-capable ones). The **read-only recon officers run
  `bypassPermissions` with `Bash` allowed and no hook**: `recon._opts` builds options with
  `disallowed_tools=["Write","Edit","NotebookEdit"]` but **no `hooks=`** (`orchestrator/recon.py:88-100`),
  and the callers pass `Bash` in `soldier_tools` — provost (`orchestrator/provost.py:62`), scout
  (`orchestrator/scout.py:58`), quartermaster (`orchestrator/quartermaster.py:61`). `provost.gate` has
  the same inline gap (`orchestrator/provost.py:106-116`: Bash + bypassPermissions, no hooks) — and it
  reads an **attacker-influenceable diff**. The guard's `is_dangerous` *does* handle Bash
  (`cat .env`/exfil), so the protection exists; it is simply never wired to these agents. `warn_if_absent`
  isn't called for them either, so even the loud warning is silent.
- **Impact:** The unit's most security-sensitive read-only agents — the ones whose whole job is to read
  recent DEV changes and the diff under gate — are the ones where the F1/F2 denylist is **inert**. A
  prompt-injected instruction in code/comments could get the Security/QA/Release officer to read and
  exfiltrate a secret.
- **Fix:** Add `hooks=guard.hooks_config()` to `recon._opts` and to `provost.gate`'s `ClaudeAgentOptions`,
  and call `guard.warn_if_absent(officer)` on those paths. Read-only officers still need `Bash` for
  `npm/bun audit`, so deny-by-content (the existing hook) is the right tool, not removing Bash.
- **Effort:** S
- **Depends on:** none

## F4: The "economical model ladder" is largely inert — auto_model is on but most code still runs Opus
- **Severity:** P1 (real pain)
- **Area:** `orchestrator/models.py`, `orchestrator/builder.py`, `orchestrator/reviewer.py`, plus every officer that hardcodes a model
- **Problem:** `auto_model: true` is on (`config.yaml:4`, default flipped to `True` at
  `orchestrator/config.py:123`), but the ladder it gates barely fires. Three independent reasons:
  (1) **Builder almost always starts on Opus** — `size_ticket` maps any `score≥1` to `"high"`
  (`orchestrator/builder.py:136-137`), `_HEAVY_EFFORT` includes `"high"` (`orchestrator/models.py:88`),
  and `for_builder` sets `base=2` (Opus) for heavy effort (`orchestrator/models.py:121`); so a routine
  2-AC ticket is Opus on pass 1 and "Sonnet-first" only bites trivial tickets. (2) **Reviewer never
  escalates** — `for_reviewer` accepts an `iteration` and has the escalation branch
  (`orchestrator/models.py:126-135`), but `reviewer.review` calls `models.for_reviewer(cfg, diff)` with
  **no iteration** (`orchestrator/reviewer.py:86`; both loop call sites `orchestrator/loop.py:549,558`
  also omit it), so it defaults to 1 — the `iteration>1` branch is dead code and the docstring's
  "escalating on re-review" is false. (3) **Most agent calls bypass the ladder entirely** —
  `models.for_*` are called *only* in `builder.py:267` and `reviewer.py:86`; the Test Engineer
  (`orchestrator/test_engineer.py:135`), squad planner + soldiers (`orchestrator/squad.py:155,172`),
  PM (`orchestrator/pm.py:154,174`), and all recon (`orchestrator/provost.py:61,107`,
  `orchestrator/recon.py:108-114`) hardcode `cfg.builder_model`/`cfg.reviewer_model` = Opus. The
  `SYSTEM_OVERVIEW.md:357,479` still documents the *old* "auto off, Opus-always" policy (prior F15 drift).
- **Impact:** The cost lever the config advertises is mostly a placebo; the dominant burn (Test Engineer
  per iteration, soldiers, recon) is unconditionally Opus. `usage.code_mix` will report a low `cheap_pct`
  not because Opus is needed but because the ladder never reaches most calls.
- **Fix:** Thread `iteration` into the reviewer call (`reviewer.review` → `for_reviewer(cfg, diff, iteration)`).
  Decide deliberately whether non-builder officers should honor `auto_model` (route them through a
  `models.for_officer(...)` helper) or document that they're intentionally pinned. Reconsider whether
  auto-sized `"high"` should map to Opus base, or only `"max"`/explicit pins. Update SYSTEM_OVERVIEW §9.
- **Effort:** M
- **Depends on:** none

## F5: A dead Jira token looks like an empty queue — UNREACHABLE alert is suppressed after any prior idle
- **Severity:** P1 (real pain)
- **Area:** `orchestrator/autopilot.py`
- **Problem:** `idle_announced` (`orchestrator/autopilot.py:169`) is **one boolean gating two different
  branches**: the "boards UNREACHABLE" alert and the "queue clear" message, both under
  `if not idle_announced:` (`orchestrator/autopilot.py:230`). It is set `True` whichever branch fires
  (`:241`) and reset `False` **only when a non-empty worklist is produced** (`:247`). Sequence: the
  queue legitimately empties → "queue clear" prints and sets the flag; later the Jira token expires
  (Jira answers an unauthenticated search with HTTP 200 + no issues, recorded in
  `intake.LAST_DRAIN_ERRORS`, `orchestrator/intake.py:87-89`) → empty worklist again, but the flag is
  already `True`, so the whole block is skipped and the "⚠️ Autopilot can't read your backlog" Telegram
  **never fires**.
- **Impact:** This is the exact failure the feature was built to catch ("EU's whole To Do column hidden
  behind a dead `JIRA_API_TOKEN`", `orchestrator/autopilot.py:224-228`) silently regressing whenever
  the board idled just before going dark. A hung backlog is indistinguishable from an empty one.
- **Fix:** Key the announce on the *reason*, not a bare boolean — e.g. remember the last announced state
  as `(bool(unreachable), frozenset(unreachable))` and re-announce on any transition (clear→unreachable
  or a changed unreachable set), so going dark always pushes once.
- **Effort:** S
- **Depends on:** none

## F6: Generalize the F8 lock — `blocked`/`pending_decisions`/`usage_ledger` still race and lose writes
- **Severity:** P1 (real pain)
- **Area:** `orchestrator/autopilot.py`, `orchestrator/decisions.py`, `orchestrator/usage.py`, `orchestrator/governor.py`
- **Problem:** Prior F8 added a `threading.Lock` + `fcntl.flock` to `audit.jsonl`
  (`orchestrator/audit.py:24,36-45`) but the fix was **not generalized** to the other shared state files,
  all written from the same concurrent contexts (in-process autopilot loop + Telegram poll thread +
  cockpit Flask threads):
  - **`blocked_tickets.json`** — the loop snapshots `blocked = load_blocked(cfg)` at
    `orchestrator/autopilot.py:219`, runs `run_loop` for minutes (`:251`), then writes back the **stale**
    set (`:278-281`); `save_blocked` is a lockless `write_text` (`:84-88`). A `/unblock <id>` issued
    mid-cycle (poller → `autopilot.unblock`, `decisions.py:181`) is silently **re-parked** when a
    different ticket parks in that same cycle.
  - **`pending_decisions.json`** — `load`/`_save` (`orchestrator/decisions.py:32-43`) have no lock and
    `_save` is a non-atomic `write_text`; `add` (`:47-56`, also called from `loop.py:108`) and `resolve`
    (`:69-83`) are unsynchronized read-modify-write writers that can **lose or resurrect** a decision.
  - **`usage_ledger.jsonl` / `usage.jsonl`** — `usage.record` (`orchestrator/usage.py:68-71`) and
    `governor.note_call` (`orchestrator/governor.py:27-29`) append with bare `open("a")` and no lock;
    torn/interleaved lines are dropped by `_rows` (`usage.py:81-84`), so today's burn **under-counts**
    and the runaway-budget auto-pause (`usage.budget_status`, `:154-168`) can fail to trip.
- **Impact:** The exact "stuck ticket never retries" failure the park/unblock machinery exists to
  prevent; lost Commander decisions; and a weakened cost guard — all from the lock that already exists
  one module over.
- **Fix:** Lift the F8 pattern (`audit.py`) into a small shared `locked_append`/`locked_rmw` helper and
  use it in `save_blocked`, `decisions._save`, `usage.record`, `governor.note_call`. Re-read
  `blocked`/`error_counts` immediately before the write-back in the autopilot park block.
- **Effort:** M
- **Depends on:** none

## F7: Autopilot Start and the Telegram/answer run-threads bypass the F13 run-guard
- **Severity:** P1 (real pain)
- **Area:** `orchestrator/server.py`, `orchestrator/autopilot.py`, `orchestrator/decisions.py`
- **Problem:** F13's `_run_lock` + `_state["active"]` guard (`orchestrator/cockpit_state.py:24`) is held
  only by the three cockpit run POSTs (`orchestrator/server.py:280-284,330-334,1556-1560`). Three other
  paths that start a run/loop take **neither**: the autopilot background thread
  (`server.py:131` → `autopilot.autopilot` → `run_loop` at `autopilot.py:251`), the
  Start/Stop toggle handler (`server.py:120` check / `:139` set — no lock), and the Telegram/answer-box
  resume (`decisions._run_bg` at `decisions.py:122,131`; `answer_api._bg` at `server.py:1511-1533`).
  Flask runs `threaded=True` (`server.py:1622`), so these are real TOCTOU windows. Two consequences: a
  double "Start autopilot" **orphans the first loop's stop `Event`**, so the cockpit can no longer stop
  it (you must kill the process); and a manual `/drain` while autopilot is live can `run_loop` the same
  app concurrently. (The catastrophic dual `reset --hard`/`clean -fd` is prevented by the per-app
  worktree `flock`, `loop.py:263` — the second run is *deferred*, not destructive — so the data is safe;
  the orphaned stop is the sharp edge.)
- **Impact:** "Stop" in the War Room silently does nothing after an accidental double-Start; runs can
  overlap in ways the `_state["active"]` badge doesn't reflect.
- **Fix:** Take `_run_lock` and set/clear `_state["active"]` in the autopilot-start handler and in
  `decisions._run_bg`/`answer_api._bg` (mirror the run routes). Make Start idempotent: if an autopilot
  Event already exists, refuse a second start instead of overwriting it.
- **Effort:** S
- **Depends on:** F6 (share the same locking approach)

## F8: Add tests for the land path — the only moment DEV changes is untested
- **Severity:** P1 (real pain)
- **Area:** `tests/`, `orchestrator/git_ops.py`, `orchestrator/loop.py`
- **Problem:** A repo-wide grep (excluding worktrees/backups) finds **zero** test references to
  `trial_merge`, `land_trial`, `push_base`, `abandon_trial`, or `sync_main_base`. The two tests that
  drive the real `Git` class exercise only the *feature* path (`git_add_hygiene_test.py`:
  `checkout_feature`/`diff_against_base`/`commit_all`) and the SRE rollback (`sentinel_test.py`). The
  trial-merge → validate-on-throwaway → ff-push-to-`origin/<base>` sequence (`orchestrator/git_ops.py:212,237`,
  driven by `orchestrator/loop.py:704,728`) — "the ONLY moment DEV changes" — and the never-touch-MAIN
  guard (`git_ops.py:241-242`) have **no regression test**.
- **Impact:** The single most consequential, hardest-to-reverse operation in the system (it pushes to a
  shared branch) is the least covered. A regression in the land/guard logic would ship silently — and
  per F1 the suite might not even notice. This is the live remnant of prior F3 (untrusted-by-tests land
  path).
- **Fix:** Add a hermetic git test (temp bare origin + worktree, as `sync_test.py` already does for
  state sync) asserting: a clean trial ff-pushes `origin/<base>` and retires the feature branch; a
  conflicting trial leaves base untouched and opens the PR path; and `land_trial`/`merge_no_ff` raise
  on the protected branch.
- **Effort:** M
- **Depends on:** F1 (so a failing land test actually fails the suite)

## F9: Observability — the UNREACHABLE condition is never audited, and outcomes live in two vocabularies
- **Severity:** P1 (real pain)
- **Area:** `orchestrator/autopilot.py`, `orchestrator/audit.py`, `orchestrator/contracts.py`, `orchestrator/loop.py`
- **Problem:** The autopilot's reachable audit events are only `autopilot_start`/`budget_pause`/
  `git_hold`/`memory_learn`/`autopilot_stop`. The **idle-because-unreachable** branch
  (`orchestrator/autopilot.py:229-241`) emits a `print` + a once-per-stretch `notify.send` and **zero
  `audit.record`** — so a backlog that went dark at 3am leaves no trail in `audit.jsonl`, which is "the
  spine of observability." Separately, outcomes are tracked in **two disjoint vocabularies**: the
  `Outcome` enum (`orchestrator/contracts.py:130-136`) used by the programmatic `TicketReport` path, and
  a parallel set of **hand-typed audit-event strings** (`audit.record("merged"/"pr_opened"/"dryrun_land"/
  "no_changes"/"ticket_exception"/"pm_triage", …` at `orchestrator/loop.py:117,492,647,722,747,781`)
  that the dashboard/forensics reconstruct outcomes from — drift between them silently mis-classifies a
  run.
- **Impact:** When an unattended run goes wrong, the one durable record is missing exactly the
  "why is the queue empty" signal, and outcome reporting depends on string literals kept in sync by hand.
- **Fix:** `audit.record("backlog_unreachable", boards=…)` in the unreachable branch (and a
  `queue_clear` event for the benign case). Derive the audit event name from `Outcome` (or add a tested
  mapping) so the enum is the single source for run outcomes.
- **Effort:** S
- **Depends on:** none

## F10: Stop re-running the Test Engineer (and the full gate) on every build iteration
- **Severity:** P2 (nice-to-have)
- **Area:** `orchestrator/loop.py`, `orchestrator/test_engineer.py`
- **Problem:** `ensure_coverage` runs a **full-tools Opus agent** (`orchestrator/test_engineer.py:127,134-144`,
  `model=cfg.builder_model`, `max_turns=60`) **unconditionally inside the per-iteration retry loop**
  (`orchestrator/loop.py:517-519`; the loop is `for iteration in range(1, max_iterations+1)`,
  `loop.py:409`, default 4). On a review rejection (`loop.py:616`) the next pass re-runs
  build → gate → **Test Engineer** again, with no "diff/scope unchanged → skip" guard. The verification
  gate is itself invoked up to **3× per iteration** (`loop.py:503` pre-review, `:539` post-Test-Engineer,
  `:705` trial) — and for the Elite-Unit app that gate is the *entire* `python3 tests/run_all.py`
  (`config.yaml:60`), so a 4-pass EU ticket can run the full suite ~12 times plus 4 Opus coverage agents.
- **Impact:** Large, mostly-redundant token + wall-clock burn on exactly the retry-heavy tickets — and
  it compounds F4 (the coverage agent is always Opus).
- **Fix:** Skip the coverage pass when the build diff is unchanged since its last run (hash the changed
  paths), or run it only once review is otherwise ship-ready; and gate the post-coverage re-gate on the
  Test Engineer having actually added files.
- **Effort:** S
- **Depends on:** none

## F11: Fix the Elite-Unit self-host config — a bun command and a venv-blind gate on a Python repo
- **Severity:** P2 (nice-to-have)
- **Area:** `config.yaml`, `orchestrator/config.py`, `orchestrator/loop.py`, `orchestrator/gate.py`
- **Problem:** `worktree_setup_cmd` is a **unit-wide** config knob (`orchestrator/config.py:190`) set to
  `"bun install --frozen-lockfile"` (`config.yaml:47`) for the Bun product — but it runs on **first
  creation of *every* app's worktree** (`orchestrator/loop.py:152-154`), including the Elite-Unit repo,
  which has no `package.json`/`bun.lock` (its gate is `python3 tests/run_all.py`, `config.yaml:55-60`).
  So a fresh EU worktree runs a frozen bun install that errors every time. Worse, the EU gate command is
  bare **`python3`** — which the gate runs via `subprocess` inheriting the parent `PATH`
  (`orchestrator/gate.py:25-29`). The deps (`requests`, `Flask`, `PyYAML`, `claude-agent-sdk`) live only
  in `.venv`, which is gitignored and therefore **absent from the linked worktree**; if the autopilot
  process isn't launched with the venv on `PATH`, `python3 tests/run_all.py` dies with
  `ModuleNotFoundError: requests` (reproduced during this review) — turning every EU self-build gate red
  for a reason unrelated to the ticket, burning retries until it parks.
- **Impact:** The unit improving *itself* (EU tickets) is the most fragile path: a wrong-interpreter gate
  fails 100% and a wrong setup command noisily errors on worktree creation.
- **Fix:** Make `worktree_setup_cmd` per-app (move it onto `AppConfig`) or no-op it when there's no
  manifest; pin the EU gate to the venv interpreter (`gate_commands: ["./.venv/bin/python tests/run_all.py"]`)
  or set `gate_env`/a wrapper that guarantees the venv. Add a health check that the EU gate interpreter
  resolves the deps.
- **Effort:** S
- **Depends on:** none

## F12: Cockpit correctness — broken ship-preview Jira links, a drifted phase bar, and chat eaten by decisions
- **Severity:** P2 (nice-to-have)
- **Area:** `orchestrator/server.py`, `orchestrator/warroom.py`, `orchestrator/loop.py`, `orchestrator/decisions.py`
- **Problem:** Three independent cockpit/UX bugs:
  (a) **Ship-preview Jira links never render** — `server.py:1373` reads
  `(app_cfg.backlog or {}).get("site", "")`, but no backlog block defines `site` (config uses
  `base_url`, `config.yaml:63`; `server.py:1000` itself keys off `base_url`). `jira_base` is therefore
  always `""`, and the link branch at `server.py:1379` silently degrades to plain text.
  (b) **The phase bar is hardcoded twice and has drifted** — `loop.py:122` lists 4 phases
  (`Build/Gate/Review/Land`) for the terminal `_bar`, while `warroom.py:300` lists 5
  (`…/Security/Land`) for the web bar; **both omit the Test Engineer step** that actually runs between
  Gate and Review (`loop.py:515-544`). (prior F15-style code/display drift.)
  (c) **Free-text chat is swallowed as a decision answer** — `route_message` treats *any* non-slash
  message as a decision reply whenever a decision is parked (`decisions.py:215`); with no `id:` prefix,
  `resolve` pops the **oldest** pending decision (`decisions.py:69-83,100-107`). So while any question is
  parked, a Commander who types a normal chat message instead injects it as that decision's answer and
  triggers a rebuild.
- **Impact:** Broken deploy-preview links, a progress bar that misrepresents the real pipeline, and a
  footgun where you can't talk to the unit without accidentally resolving a parked question.
- **Fix:** (a) read `base_url`; (b) derive both bars from one `PHASES` constant that includes the
  Security + Test Engineer steps; (c) require the `id:`/explicit-reply form (or a leading marker) to
  route a message to a decision, and otherwise send free text to the CTO chat.
- **Effort:** S
- **Depends on:** none

## F13: Remove drifted duplicates — dead launchd plists, a stale `_PARKED`, and a one-pass retry guard
- **Severity:** P2 (nice-to-have)
- **Area:** `scripts/`, `orchestrator/events.py`, `orchestrator/autopilot.py`, `orchestrator/loop.py`
- **Problem:** Single-source-of-truth / dead-code drift in three spots:
  (a) **Two schedulers** — 5 `scripts/com.roman.general.*.plist` launch agents + their `scripts/run-*.sh`
  wrappers are loaded by nothing (`grep` for `launchctl`/`LaunchAgents`/`.plist` is empty); the live
  scheduler is `scripts/install-server-cron.sh:23-31`, which re-encodes the same council 06:30 /
  small-talk / weekly-patrol cadence. The plists are retired (`SYSTEM_OVERVIEW.md:213-215`) but still in
  the tree, so editing one schedule silently misses the other.
  (b) **`_PARKED` defined twice and already divergent** — `autopilot._PARKED = (ESCALATED, PR_OPENED)`
  (`orchestrator/autopilot.py:32`, ERRORED handled via retry) vs `events._PARKED =
  (ESCALATED, PR_OPENED, ERRORED)` (`orchestrator/events.py:19`); the `events` copy is **dead** (never
  referenced) but is a live drift trap.
  (c) **The stuck-retry guard only compares the immediately-previous pass** — `prev_reject_sig`
  (`orchestrator/loop.py:405,621-627`) breaks only when `sig == prev_reject_sig`, so an A/B/A/B
  rejection oscillation never trips it and burns all `max_iterations` Opus passes before escalating.
- **Impact:** Minor individually, but each is a place where "change it in one spot, forget the other"
  produces a wrong schedule, a mis-classified outcome, or wasted passes.
- **Fix:** Delete the retired plists + `run-*.sh` (or move to `legacy/`); delete `events._PARKED` and
  import the autopilot one (or hoist a single `PARKED` constant into `contracts.py`); track the last 2–3
  reject signatures in the retry guard.
- **Effort:** S
- **Depends on:** none

---

## Recommended build order

1. **F1 — make the suite fail honestly.** Foundational: until ~28 harnesses can actually go red, you
   cannot trust the verification of *any* other fix here, and the unit's own merge gate is this suite.
2. **F8 — test the land path.** Pairs with F1: add the highest-stakes missing tests (trial-merge /
   ff-push / never-MAIN) once a failing test is guaranteed to fail the suite.
3. **F2 — guard env/secret exfil.** P0 security, independent; the largest remaining hole in the
   bypassPermissions boundary.
4. **F3 — guard hook on recon officers.** Small, security-critical, same area as F2 — land together.
5. **F6 — generalize the F8 lock to the JSON/ledger state files.** Foundational concurrency fix that
   F5/F7 reliability and the budget guard all lean on.
6. **F7 — run-guard on autopilot-start and the resume threads.** Builds directly on F6's locking; fixes
   the "Stop does nothing" sharp edge.
7. **F5 — UNREACHABLE alert after idle.** Small, high-value safety fix; pairs naturally with…
8. **F9 — audit the UNREACHABLE/queue states + unify the outcome vocabulary.** Same code path as F5;
   restores 3am debuggability.
9. **F4 — fix the model ladder.** Larger cost win; independent. Do before F10 (same subsystem).
10. **F10 — stop redundant Test Engineer/gate reruns.** Compounds F4; needs the diff-hash guard.
11. **F11 — Elite-Unit self-host config.** Small config fix; unblocks reliable EU self-builds.
12. **F12 — cockpit correctness cluster.** Three small, independent UX/correctness bugs.
13. **F13 — dead-code / SSOT cleanup.** Lowest risk; do last, any order.
