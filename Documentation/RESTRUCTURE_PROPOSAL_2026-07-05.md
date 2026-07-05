# Elite-Unit Audit & Restructure — Phase 1 Findings + Phase 2 Proposal

**Date:** 2026-07-05 · **Status:** DRAFT — awaiting Commander approval/annotation
**Evidence base:** audit.jsonl (2,555 events, 2026-06-15 → 07-01), usage_ledger.jsonl (2,013 per-call
rows, 06-21 → 07-01), git history, live filesystem state, code at `3889b42`. Every number below is
measured; anything unproven is marked *uncertain*.
**Scope:** Phase 0 forensics + Phase 0.5 Quick Wins are DONE (13 commits on dev, `dd2b684..ac9a34b`).
Nothing in Phase 2 (sections 2–8 below) is implemented.

---

## 0. What actually happened (crash + economics, condensed)

Not one crash — four stacked causes on 2026-07-01:

1. **Claude Max plan quota exhausted** (the root economic cause). Last Anthropic call 13:58:14;
   from 14:37:34 every call silently routed to z.ai GLM (`ANTHROPIC_BASE_URL` flip). 11-day burn:
   **1.38B input / 16.4M output tokens, $1,682 API-equivalent, 2,013 calls** — peak day 06-28 alone
   264M input / $311 ≈ 1.5× the $200/mo plan in one day.
2. **EU-173 died of a real code crash**: `NameError: name 'commenter' is not defined` (audit
   l.2541, 15:17:11) introduced by manual commit `745fedc` made while the unit was live; fixed 17
   min later (`0ab888a`). Now pinned by `tests/eu173_commenter_crash_test.py`.
3. **EU-174 burned 15,535,048 builder tokens in 4 max-effort passes against an unwinnable gate**:
   the auto-changelog had written EU-119's `launchctl` verify-hint into
   `Documentation/Development_Status.md`, which `scheduler_single_source_test` forbids — the unit
   poisoned its own gate at 14:20 (`ca5e2a5`) and billed every later ticket for it (identical
   16/17 failure in all 4 passes + EU-173's gate). Escalated by design at 23:53:44 — the last event.
4. **The host process was then killed hard** (no `autopilot_stop`; SIGKILL/terminal-close class,
   exact signal unprovable). Bonus: EU-108-rerun, EU-173, EU-174 all ran as **ghost sessions** —
   `server.py:788/871` and `decisions.py:389` invoke the loop with **no run boundary events**, so
   the audit trail structurally could not close them.

**"~3 hours per ticket" verdict:** REFUTED as the median (167 merges: median 19.4 min, mean 28) but
CONFIRMED as the end-state tail — per-day median degraded 4.4 min (06-22) → **63.8 min (06-30)**,
a 14× slide in 8 days; EU-131 3.44h, AUTO-54 lifetime 61.8h across 3 attempts (its first merge was
wiped by `sentinel_revert` 2 s after landing), EU-108's re-run 2.59h/24.4M tokens for zero merges.

**Working-hypothesis verdict (org-chart / context reloading):** PARTIALLY REFUTED.
Officers do NOT reload a doc corpus — no pipeline prompt loads WAR_ROOM/ORG/Documentation, there is
no per-invocation "memory audit" (the scribe runs per council, 0.06% of burn), and fixed prompt
assembly is only ~150K tokens/ticket (~2%). The burn lives elsewhere (§1). The org-chart shape does
cost real money, but through **invocation count × agentic turns × retry escalation**, not startup
context. Ceremony (councils/meetings/smalltalk/chat) is <3% of tokens — deleting it saves theater,
not the budget.

---

## 1. Where the hours and tokens actually go — top 5 sinks, measured

| # | Sink | Measured size | Mechanism |
|---|---|---|---|
| 1 | **Builder per-turn context replay + retry escalation** | 716.5M input tokens (52% of ALL input; $792), 286 calls, 81% on Opus; median 51.6K tokens/turn × 37–204 turns/pass; input:output = **84:1** | Every agentic turn re-sends system + full tool-result history. Retries escalate effort→turns→context: EU-174's passes grew 1.96M → 2.25M → 3.49M → **7.77M input tokens** (pass 4, 119 turns). The ~18K-token fixed floor (≈15.6K of it the Claude-Code harness itself) × ~145 turns/ticket ≈ 2.6M tokens/ticket = ~35% of burn; accumulated tool-result replay is the rest |
| 2 | **The escalation tail — failed tickets** | Jul-1 alone: EU-108 24.4M + EU-174 15.5M + EU-153 7.6M = **47.5M tokens for zero merges**; token_burn median per ticket 10.3M | max_iterations=4 × per-retry effort escalation × a red base gate no builder could fix. The gate-fail path had **no repeat-detection at all** (loop.py retry_stuck guards only the review path — 0 fires in 100 retries) |
| 3 | **Delegation / soldiers** | 340M input (249 calls) ON TOP of the builder; squad-lead 14.7M (157 calls); the `-lead` planning twin runs before every recon officer and almost always returns SOLO (architect-lead 22 ↔ architect 22, pm-lead 43 ↔ 44) | L/XL tickets spawn up to 4 sequential soldier builds, then the Builder builds again; the planning pass is nearly pure overhead |
| 4 | **Test Engineer per pass** | 161.8M input, 179 calls, avg 904K/call | Runs every pass unless the diff hash is unchanged (EU-53); re-runs the whole suite inside an Opus/Sonnet agent |
| 5 | **Reviewer + moving goalposts + the EU-108 Opus pin** | Reviewer 100M input, 264 calls, 88% Opus. **135/135 (100%) of round-≥2 blocking objections were textually NEW** (median Jaccard-to-prior 0.10); **34% of FAIL reviews carried the reviewer's own `spec_met: true`**. The EU-108 "Sonnet cap → Opus" fallback pinned builder/soldier/reviewer to Opus from Jul-1 15:55 through the crash, skipping the cheap-first ladder entirely | Each round raises new objections → more passes; each retry escalates the model; Opus = 76% of total cost ($1,279) |

Also measured, for scale: **all councils, meetings, smalltalk, chats and staff officers combined
< 3% of tokens.** The economics fix must target the build loop, nothing else matters.

Quick Wins already in place against sinks 1/2: hard 2-pass cap, 400k-token/30-min per-ticket
budgets, per-call `agent_call` instrumentation (model/tokens/duration — none of which were recorded
before: 0/2,555 audit events had tokens or duration; only 5/387 build events had a model, all GLM).

---

## 2. Officer disposition — from ~24 LLM roles to 3

Ledger ground truth (2,013 calls, 11 days). Target end state: **Planner/Architect (Opus) ·
Builder (cheap executor) · Reviewer (Opus)** + deterministic scripts. Verdicts:

| Officer (calls / input tokens) | Verdict | Why |
|---|---|---|
| Builder (286 / 716.5M) | **KEEP → the Builder** | The job. Gets ONLY spec + in-scope files + gate failures (largely true today — the fat is turns/retries, not prompt). Cheap executor model (§6) |
| Reviewer (264 / 100M) | **KEEP → the Reviewer (Opus)** | Reviews the DIFF against the spec's AC only; runs only after deterministic gates are green; ≤2 rounds (already enforced). Add: objections must cite the AC they violate — `spec_met:true` + FAIL becomes structurally impossible |
| Architect + architect-lead (44 / 6.2M) | **MERGE → the Planner** | ONE Opus invocation per ticket: ticket → spec with testable AC + in-scope file list. Absorbs squad-lead sizing, senior-PM triage, scrum split decisions |
| Squad-lead (157 / 14.7M) + soldiers (249 / 340M) | **DELETE (delegation off)** | Soldiers doubled build burn; the Planner's in-scope file list replaces the split. Re-introduce only with evidence a single Builder can't handle a ticket class |
| Test Engineer (179 / 161.8M) | **MERGE → Builder + script** | The spec's AC include tests; the Builder writes them; the deterministic gate runs them. Kill the separate 904K-avg Opus/Sonnet pass |
| Provost-gate (161 / 8.0M, all Opus) | **MERGE → Reviewer + script** | Secrets/deps → deterministic scan (§3); judgment items become one Reviewer checklist section |
| PM + pm-lead (87 / 4.7M) | **MERGE → Planner (triage) / DELETE (findings-triage → diff-path matching script)** | Its RESOLVE requeue also re-opens the loop cap — one more reason to fold it into the Planner's single decision |
| Senior PM (12 / 4.2M) | **DELETE** | Disabled by default; its deterministic override (AC/label → CONTINUE) already neuters it |
| Scrum (9 / 1.0M) | **MERGE → Planner** | Splitting is a planning decision |
| Gap-detect (36, Haiku) | **REPLACE WITH SCRIPT** | `routing.classify_task` (already in repo) is the same keyword classifier for free |
| HR (5) + specialists | **DELETE (dormant)** | 5 calls ever; ephemeral-specialist synthesis = template + string substitution if ever needed |
| Sentinel | **KEEP (already a script)** | Proof the deterministic pattern works. But decide on `sentinel_revert` semantics — it wiped AUTO-54's merged work 2 s after landing and forced 2 re-runs |
| Quartermaster (30) | **REPLACE WITH SCRIPT** | Build/typecheck/migration/lockfile checks are commands; a report needs no LLM |
| Scout (31) | **PARTIAL SCRIPT** | Playwright run = script; triage of output = cheap model, on-demand only |
| Provost recon (13) | **KEEP, scheduled** | Weekly security sweep has real value; cheap model + script pre-pass |
| The-General/chair (79) + council voices (~200 calls total) + scribe (35) + smalltalk (36) + standup/group (~56) | **DELETE from cron; keep on-demand commands** | <3% of tokens but ~100% of the org-chart noise. Scribe → weekly job. Smalltalk: zero delivery function |
| Liaison (0 calls ever) | **DELETE (dead)** | Defined, never invoked |
| Adjutant (54) + Drillmaster (32) | **DELETE** | Their `-apply` paths: **0 invocations ever** — proposals nobody applies. Officer .md files aren't even loaded into prompts (only test-engineer.md is; `performance-engineer.md`, `engineer.md`, `inspector-squad.md` have zero code references) |
| Notify distillers (29, Haiku) + roster (1) | **REPLACE WITH SCRIPT** | Deterministic `bulletize` fallback already exists in notify.py |

**Net effect on the per-ticket call chain:** today ~8.3 LLM invocations/ticket (architect-lead,
architect, gap-detect, squad-lead, builder×N, TE×N, reviewer×N, pm, provost-gate) → target **3–5**
(Planner ×1, Builder ×1–2, Reviewer ×1–2), each with fewer turns and no effort escalation.

---

## 3. Deterministic gate design (runs BEFORE any LLM reviewer)

Order per pass — each failure feeds the Builder as plain text (a free review round), and no LLM
reviewer runs until all are green:

1. **Red-base short-circuit (NEW — the EU-174 killer):** before pass 1, run the gate against the
   BASE commit. If the base itself is red with the same fingerprint → BLOCK the ticket immediately
   with "base branch is broken", Telegram the Commander. Would have saved ~40M tokens on Jul 1
   alone. Also: identical gate-failure fingerprint on consecutive passes → escalate, don't rebuild.
2. **Test suite** (exists: `gate.py` / `tests/run_all.py`) — consolidated per §5 to minutes.
3. **Lint/format** (`ruff check` — fast, not currently a gate).
4. **Secret-leak guard** (regex/gitleaks over the diff — replaces the secrets half of the 161
   Opus provost-gate calls).
5. **Dependency/lockfile sanity** (script — the Quartermaster's actual checklist).
6. Only then: **Reviewer (Opus, diff + spec AC only, ≤2 rounds)**.

---

## 4. Context diet — per role, before/after

Measured today: fixed floor ≈ **18.0K tokens per invocation** (min observed 17,674), of which
~15.6K is the Claude-Code harness (system prompt + tool schemas — not controllable from prompt
strings), ~1.8K `memory.preamble()` (UNIT.md + UNIT.live.md), 0.3–3.4K officer system strings.
The doc corpus is NOT loaded (hypothesis refuted); there is NO per-invocation memory audit — the
scribe already runs per-council only, so the "weekly memory job" is a formalization, not a change.

| Role | Loads today | Loads after | Lever |
|---|---|---|---|
| Planner (new) | — | ticket + repo map + AC template (~4K) + preamble | One Opus call/ticket |
| Builder | trimmed preamble (1K) + BUILDER_SYSTEM (1.4K) + ticket/ADR/feedback; re-Reads CLAUDE.md in 49% of passes; **turns 37–204, escalating** | spec + in-scope files + gate-failure text ONLY; **max_turns hard-capped (~60), NO effort escalation on retry** (retry effort was the burn multiplier); prompt-cache CLAUDE.md | The real lever is turns × passes, not prompt bytes |
| Reviewer | preamble (1.8K) + REVIEWER_SYSTEM + project CLAUDE.md + FULL diff | spec AC + diff only (drop preamble/CLAUDE.md — the AC are the contract) | Small |
| Test Engineer | fattest system (13.5K) × 179 calls | **role deleted** (§2) | −162M tokens |
| Everything scheduled/ceremonial | preamble each | cron OFF / on-demand | Noise removal |

Arithmetic honesty: cutting invocations 8.3→4 and capping turns/effort attacks the ~7.5M-token
average pipeline burn per ticket at its two real factors; prompt trimming alone would save ~2%.

---

## 5. Test-suite consolidation (256 collected harnesses)

Measured: run_all.py is sequential, one subprocess per file, **zero timing anywhere** (also true of
audit.jsonl — now fixed by QW4 `duration_s`). Full suite ≈ **175–200 s** locally; **12 files are
63% of the runtime (112 s)**, all autopilot-wait harnesses.

- **Nightly-only tag (new, in run_all.py):** the 12 heavy files (eu118_plan_limit 18.8s,
  autopilot_idle_then_dark 10.1s, autopilot_idle_transitions 9.5s, eu87_drain_order 9.3s,
  eu61_autopilot_resume_queue 9.3s, autopilot_unblock_reread 8.8s, autopilot_drain_order 8.3s,
  integration_sse 8.1s, autopilot_retry 8.1s, autopilot_unreachable 7.4s,
  eu116_no_changes_drain_loop 7.3s, idle_announce 7.1s). Per-land gate drops to ~65s; a trivial
  subprocess pool halves it again.
- **Duplicates to merge/delete:** eu87_drain_order vs autopilot_drain_order (identical EU-87 pin,
  ~17.6s combined); the EU-122 triple (dual_provider / dual_provider_budget / budget_monitor);
  idle_then_dark + idle_transitions (siblings, pay the autopilot wait once).
- **Orphans never executed:** `tests/test_senior_pm.py`, `tests/test_autopilot.py` (wrong prefix —
  the glob is `*_test.py`) — review-then-fold or rename. CI runs the same glob, so these are
  invisible there too.
- **Merge candidates (~35 files → ~12 suites):** EU-63 tabs (5), EU-65 liaison (5), EU-61 (7→2),
  EU-104 quartet, EU-120 trio, EU-43 docs trio, chat cluster (5), warroom_triage trio; 62 of 112
  EU-pin files boot the identical Config+Flask fixture independently.
- **The rot lesson from this session:** 13 of 256 harnesses were red on dev before Phase 0.5 —
  stale stubs vs two-day-old signatures and pins of deliberately-removed UI. The EU-153 overnight
  gate saw **107/248 red**. A red suite converts every ticket into a 4-pass burn loop; suite
  health IS an economics control.

---

## 6. Model routing — Opus planner/reviewer, GLM 5.2 builder (design only, NOT implemented)

**What exists:** `routing.py` (EU-174, inert behind `ROUTING_ENABLED=false`): keyword/size
classifier, LOCAL (Ollama) vs CLOUD tiers, cloud model default already `"glm-5.2"` via
`ANTHROPIC_MODEL`. The z.ai Anthropic-compatible endpoint demonstrably works (18 GLM calls landed
in the ledger on Jul 1; provider detection EU-123 tags them). The EU-108 sonnet-cap fallback shows
where per-officer model policy lives (`models.py`).

**Two confirmed defects to fix before ANY routing goes live:**
1. `agent.py:239-247` — the Opus-fallback retry clones options but **drops `cwd`, `hooks` (the
   guard denylist) and `disallowed_tools`**: a Builder retry runs outside the worktree with no
   guard under bypassPermissions.
2. `agent.py:165-177` — env restore is skipped when `ANTHROPIC_BASE_URL` was originally unset
   (`if routing_tier and _original_base_url is not None`): one LOCAL-tier call permanently points
   the whole process at Ollama.
Also: `usage.py:101` stores only the model FAMILY and records GLM calls as `m:"opus"` — the ledger
lies about routed calls; record the full model id + provider.

**Design (minimal, at the existing seams):**
- A routing table `{tag → (model, effort, max_turns)}` consulted in `models.py` (S2) and applied
  via `options.model` in `run_agent` (S1) — **no env mutation**. Planner/Reviewer → `claude-opus-4-8`;
  Builder/soldier tags → `glm-5.2` (via z.ai base URL configured once at process start, or an
  explicit per-call client), officers/chat → Haiku/off.
- **Automatic fallback GLM → Sonnet**: on `is_error`, parse-failure, or gate-fail streak of 2,
  re-run the pass on `claude-sonnet-4-6` and record a `builder_fallback` audit event. Subsumes and
  replaces the EU-108 Opus pin (which currently pins to the MOST expensive tier until Friday —
  an anti-economics fallback; ledger: builder 233/286 calls Opus while it was active).
- Keep `run_agent_with_fallback` as the single wrapper for all tags (today only builder+reviewer).
- **Evaluation before trust:** 10-ticket shadow run; compare gate-pass rate, review-pass rate,
  tokens, wall time vs the Sonnet baseline. GLM quality is UNPROVEN here — EU-173/174's GLM
  failures were red-base artifacts, not model verdicts.
- Effort estimate: routing table + option-based override + fallback + ledger fix ≈ **1–2 days**;
  eval harness reuses swebench_* scaffolding ≈ 1 day.

---

## 7. Event flow — one bus, audit.jsonl

**Verdict: NOT single-source today.** audit.jsonl is the dominant, correctly-locked log (1 writer
choke point, 18 emitting modules) but the cockpit stitches **three planes**: audit + ~20 mutable
sidecar files + in-process volatile state (run flags, stdout ring) reconciled by crash heuristics.
Telegram is fully imperative: **~74 direct `notify.send()` sites in 14 modules** (+23 via loop's
`_notify`) — no event→notification bridge; one escalation writes 5 stores (audit, pending_decisions,
Jira status, Jira comment, Telegram) with no consistency guarantee.

**Minimal change to get there (design):**
1. `AuditLog.subscribe(fn)` — ~15 lines at the existing choke point (audit.py:32).
2. One **Telegram notifier subscriber** with an event→template/severity/dedup table
   (`needs_human, merged, ticket_exception, security_block, budget_*, plan_limit,
   autopilot_start/stop/unclean_restart, sentinel_revert, ticket_budget_exceeded,
   builder_reviewer_disagreement`); persist dedup keys (today's one-shot flags are in-memory and
   re-fire after every restart). Migrate the 74 sites incrementally: record event → delete send.
3. Sidecar files become **single-writer projections** of new state-change events
   (`decision_added/resolved`, `ticket_parked/unparked`, `proposal_*`, `dismissed`,
   `run_claimed/released` — the last one lets the EU-104 ghost-card heuristics be deleted).
4. **Immediate tactical hardening (5 one-line diffs, independent of the bus):** proposals.json
   (HIGH: Flask thread × Telegram poller lost-update race), approvals.json, dashboard.dismiss,
   governor usage.jsonl prune, usage_ledger prune → `locking.locked_rmw/locked_append`.
5. Close the **ghost-session gap**: record run boundaries in `server.py:788/871` and
   `decisions.py:389`, and move `autopilot_stop` (autopilot.py:800) inside the `finally`.
QW4's `agent_call` events already moved per-call economics INTO audit.jsonl (the cockpit's usage
pages can later derive from it; usage_ledger stays as the compact rollup).

---

## 8. Ramp-up plan (after you approve)

1. **Pre-flight (one sitting):** verify `.env` no longer points `ANTHROPIC_BASE_URL` at z.ai
   unless intended; delete stale `state/sonnet_fallback_state.json` (expired Jul-3; auto-clears on
   first check anyway); decide the EU-108 fallback fate (§6); fix the 2 routing defects if routing
   stays merged; clean the **5 launchd agents still firing for scripts deleted on 06-26**
   (`com.roman.general-autopull/council/patrol/smalltalk/sync` — council/sync err logs have grown
   since Jun 26; VPS cron is the sanctioned scheduler).
2. **Restart under supervision:** `bash scripts/install-mac-autopilot-daemon.sh Elite-Unit` —
   KeepAlive auto-restart + the new unclean-restart Telegram alert. `max_tickets_per_run` stays 1
   (already the default), QW3 2-pass cap + QW4 400k/30-min budgets active.
3. **Observe 1–2 days** with the new telemetry: per-call `agent_call` (model/tokens/duration),
   `token_burn_report`, `ticket_budget_exceeded`, `builder_reviewer_disagreement`. Success gates:
   median ticket < 25 min, < 3M tokens; zero silent continuations past budget; zero unclosed runs.
4. **Then raise** `max_tickets_per_run` and/or begin Phase 2 (this document, top to bottom:
   deterministic gates → 3-role collapse → routing → event bus → test consolidation).

---

## Appendix A — Quick Wins shipped this session (13 commits, all verified)

| QW | Commit(s) | Note |
|---|---|---|
| Gate greening | dd2b684, 8aec78c, 5461b15, ba24ddf, 9c1c745 | **13 pre-existing red harnesses repaired** (stale stubs vs Jul-1 manual commits; pins of removed Needs-you UI; changelog self-poisoning allow-list; CLAUDE.md doc index) |
| QW1 dry-run | 615ab33 | Premise pre-satisfied by `1c2cceb` (live default); stale claims removed; `doctor` already optional |
| QW2 state | ca463a4, d44a7e5 | 6 tracked runtime files untracked; `*.json.lock/tmp` ignored; state root → `state/` via `audit_path` (all sidecars follow `with_name`); files physically moved on this host. VPS keeps its own pinned `audit_path` until migrated at deploy |
| QW3 loop cap | 38c03cf | `HARD_MAX_PASSES=2`, not overridable upward; `builder_reviewer_disagreement` event; residual: PM RESOLVE requeue grants ≤1 extra capped attempt (decision for you: kill it in Phase 2?) |
| QW4 budgets | ecaead8 | 400k tokens / 30 min per ticket → BLOCKED + Telegram; `agent_call` audit event + `duration_s` everywhere; `tests/ticket_budget_test.py` 12/12 |
| QW5 watchdog | 530834e | launchd keepalive (NOT PM2 — no-Node rule; installer existed, plist lints OK) + unclean-restart Telegram alert. **Deliberately not activated** — activation = ramp-up step 2 |
| QW6 crash pin | 3423db9 | `eu173_commenter_crash_test.py` — the NameError path, 4/4 |
| QW7 reaper | ac9a34b | Detached-HEAD fix (production creates ONLY detached worktrees — the reaper was structurally blind to its own target class); **the live Jul-1 orphan was reaped on this host with the fixed code**; wiring was already correct, it had just never run |

## Appendix B — flagged, NOT touched (your call)

- `orchestrator/reaper.py` — dead near-duplicate of `git_ops.reap_stale_worktrees` (0 importers,
  already diverging). Recommend delete.
- The 5 stale launchd agents on this Mac firing deleted scripts since `bf1ce92` (06-26).
- Ghost-session boundaries + `autopilot_stop` outside `finally` (§7.5) — small, but Phase 2 scope.
- `sentinel_revert` wiped AUTO-54's merge 2 s after landing (06-29 00:04) — semantics review.
- EU-108's post-merge re-run (07-01 10:02, 24.4M tokens after a successful 05:04 merge) — the
  trigger is not in the audit log (*uncertain*); the run-boundary fix will make this diagnosable.
- EU-174's builder edited the MAIN repo tree despite worktree isolation (agent.py/builder.py
  mtimes 23:53 in the main tree, committed later as `3889b42`) — isolation leak for
  self-development tickets, unverified mechanism (*uncertain*).
