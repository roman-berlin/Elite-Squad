# Elite-Unit — 2026-07-16 Total Audit — Index of Findings (EU-358)

**Date:** 2026-07-16 · **Type:** full-system read-only audit (system live, ≥3 drains running throughout;
the auditors never touched code/git/processes — read + grep only). **Method:** a 12-auditor fleet, one
per subsystem, over all of `orchestrator/`, the `general` launcher, and `tests/`; **114 raw findings**,
each fed to a 3-lens adversarial panel (correctness · reachability · impact) that defaulted to *refuted*
unless the code confirmed the claim. Deduped against all 92 open EU tickets at audit time.

This document is the durable index — the fixes live in code (EU-358 commits) and the deferred findings
live on the tickets it points to. This is the map so the next audit doesn't re-derive it.

> Companion to [SYSTEM_AUDIT_2026-07-06.md](SYSTEM_AUDIT_2026-07-06.md) (combat-readiness) and
> [DELTA_AUDIT_2026-07-12.md](DELTA_AUDIT_2026-07-12.md) (07-07..12 delta). This is the 2026-07-16
> full re-sweep on top of both, cross-referenced by ticket ID.

---

## 1. Headline

The unit is **structurally healthy** — 334/334 harnesses green at audit start, the crash classes from the
07-06 and 07-12 audits (max-turns misclassification, reap self-halt, PID-file flakes) are closed. The
remaining risk is **not** a single crash class; it is a long tail of **swallowed-error / silent-loss**
seams: places where a failure returns a benign value instead of surfacing, so the drain keeps running
while quietly losing work, gating the wrong thing, or reporting green on unverified state.

The single most-cited structural issue (named by 8 of 12 auditors independently): **`state/audit.jsonl`
never rotates and every hot path re-parses the whole history** — the drain cycle and cockpit render both
scale with all-time history (→ EU-363).

---

## 2. Fixed in this audit (EU-358, landed on dev 2026-07-16)

Eleven verified defects with cheap, self-contained fixes were landed directly, each with a regression pin
in `tests/eu358_audit_fixes_test.py` (40 checks). Full suite stays green (334 → 335 harnesses).

| # | File | Defect | Fix |
|---|---|---|---|
| 1 | `backlog/jira.py` | No HTTP timeout on ANY Jira call — one half-open socket hangs the drain thread forever | `_with_default_timeout` wraps the session (JIRA_HTTP_TIMEOUT, 30s) |
| 2 | `scrum.py` | Mid-batch fragment-filing failure still closed the parent + reported ok=True → the unfiled fragments vanished | ok ⇔ ALL filed; partial leaves parent open + "INCOMPLETE" comment |
| 3 | `decisions.py` | Question validator rejected "I need to"/"Let me"/"Based on the"/"Looking at the" as substrings anywhere → ate legit decisions | line-start-anchored for common openers; distinctive markers still anywhere |
| 4 | `notify.py` | `send()` had no 4096-char chunking, no 429 retry → long escalations silently lost (400) | newline-boundary chunking + one bounded 429 retry |
| 5 | `connections.py` | Raw-token store written non-atomic, world-readable (0644), errors swallowed | tmp+os.replace, chmod 0600, visible warning on failure |
| 6 | `usage.py` | `daily_token_budget: 0` ("off") read as exhausted → pre-flight skipped every ticket | budget-off ⇒ no gating (matches `budget_status`) |
| 7 | `approvals.py` | All-failed filing batch stamped "approved" → proposals left queue with nothing on board | all-failed returns batch to pending + notify |
| 8 | `gate.py` | Post-timeout zombie reap had no timeout — a re-daemonized grandchild froze the whole drain | bounded 10s reap + partial-output tail in report |
| 9 | `warroom.py` | `\n` in a non-raw `_PAGE` JS regex rendered as a literal newline → SyntaxError killed the terminal script | escaped to `\\n` |
| 10 | `loop.py` | `_recent_no_changes` scanned all-time history → one old no_changes excluded a ticket from the drain forever | 48h window on the event timestamp |
| 11 | `decisions.py` | `/run`/`/drain` detected live mode by substring — "--liveness" flipped a dry-run to LIVE | token-anchored |

**Refuted / stale (not fixed):** autopilot `stop_event` not threaded (already fixed in cb5c759/EU-356);
pre-EU-336 decision-spam claims; a `reap_stale_worktrees` self-reap variant (fixed in EU-334).

---

## 3. Deferred findings — filed as EU tickets

Grouped fixes, each with acceptance criteria + a required regression pin:

### High — safety-net & integrity
| Ticket | Title |
|---|---|
| **EU-359** | `[sentinel]` Post-merge monitor false-skips/greens: hardcoded two_tenant env-shape + free-text misconfig matching (silently dead for every non-two-tenant app). |
| **EU-367** | `[loop/git/sync]` Land-path integrity trio: Jira writes after irreversible git effects; silent worktree→in-tree downgrade (EU-174 isolation-leak class); promote pushes stale local dev (EU-335 class). |
| **EU-372** | `[decisions/ci]` Pending decision popped before resumed run starts; Telegram offset advanced before routing; CI conclusion false-green on first-completed / multi-workflow commits. |

### High — hang-proofing (the drain must never freeze)
| Ticket | Title |
|---|---|
| **EU-360** | `[tests/run_all]` Per-harness timeout + strip JIRA_* from child env + tolerate non-UTF8 output. |
| **EU-366** | `[infra]` git/gh without timeouts or GIT_TERMINAL_PROMPT=0; worktree-setup installs unbounded; auth-probe stampede. |

### High — security
| Ticket | Title |
|---|---|
| **EU-364** | `[memory]` Officer-prompt injection surface: scribe output written verbatim into `UNIT.live.md` (prepended to every officer prompt) + unlocked writes. |
| **EU-371** | `[security]` One-char glob bypass of the secret-exfil denylist (`cat .env*`); `gate:allow-secret` honored on the diff under review; GLM token inherited by product subprocesses. |

### Medium — reliability / correctness
| Ticket | Title |
|---|---|
| **EU-361** | `[cockpit]` Server correctness batch: stale SSE log path, Stop-button race, one-shot flag races, missing run bracketing, false audit event, EU-254 port hardcode. |
| **EU-362** | `[cockpit]` Resource-leak batch: per-request Workspace leak, unbounded /api/terminal buffer, unbounded needs-summary cache. |
| **EU-363** | `[state]` `audit.jsonl` rotation/compaction — the #1 structural finding (8/12 auditors). |
| **EU-365** | `[jira-adapter]` status_map bypass in JQL, error-swallow→dup filings, single-page comments, truncated-title dedup, no 429 handling. |
| **EU-368** | `[autopilot]` PID liveness needs an identity check (recycled-PID false-alive + CLI clobbers a live daemon's PID file). |
| **EU-370** | `[officers]` Resilience: one bad officer aborts the whole ceremony; reviewer/architect crash on off-schema LLM output; failed Architect run parsed as a real ADR. |

### Evidence added to existing tickets
EU-345 (3 more cockpit-latency contributors), EU-322 (sharper infra-classify evidence), EU-256 (2 more
unlocked shared-state writers), EU-259 (verified land_trial rebase gate-bypass + fix design — the panel's
one **high** that stayed high; the ticket comment was permission-blocked in the audit session, so its
evidence lives here in §5 and on EU-367's cross-link).

---

## 4. Top recurring improvement themes (60 suggestions → 8 clusters)

1. **Rotate/compact `audit.jsonl`; read bounded windows** — 8 auditors. → EU-363.
2. **One blessed, locked, atomic write path for all runtime state** (jira_connections done; UNIT.live,
   recent_projects, error_counts, blocked remain) — 6 auditors. → EU-256, EU-364.
3. **Hang-proof every subprocess/HTTP seam with a timeout** (Jira+gate done) — 5 auditors. → EU-366.
4. **Structurally validate everything an LLM writes into persistent state** — 4 auditors. → EU-364, EU-370.
5. **Replace the `notes`-string protocol on TicketReport / thread structured signals** (turn-limit, stall,
   WIP-preservation) through typed fields — 4 auditors. → folded into EU-367/EU-370 follow-ups.
6. **Finish the EU-174 per-call routing migration; delete the process-global env window** — 3 auditors.
   → EU-238/EU-239 (existing).
7. **Content-addressed / in-batch de-dup for autofiled tickets** — 3 auditors. → EU-365.
8. **Delete the dead `orchestrator/reaper.py`** (0 importers, pre-EU-334 duplicate carrying the self-reap
   crash) — confirmed dead; safe delete, flagged for Roman (out of this batch's scope per stop-conditions).

---

## 5. EU-259 — the one high that stayed high (durable evidence)

`land_trial`'s non-fast-forward recovery (`git_ops.py:624-650`) fetches the moved `origin/dev`, rebases the
trial branch onto it, and **re-pushes with no gate re-run**. The only dev gate ran earlier (`loop.py:1785`)
on the PRE-rebase tree; the gate window is up to `gate_timeout_sec` (1800s) — ample for a concurrent land
(two orchestrator instances, or a human push). A plain rebase linearizes the trial (drops the merge commit,
replays feature commits with new shas), so `merge_sha` captured pre-land (`loop.py:1816`) is never on dev —
`sentinel.revert_merge_on_base` and `ci_conclusion.check` then reference a commit that doesn't exist on
origin. **Mitigations today:** sentinel needs `postmerge_commands` (disabled in live config), `red_base_check`
catches the broken combination before the next ticket, and `revert -m 1` still applies the correct inverse
diff in the clean case. **Residual:** an ungated combined tree lands on dev, reported MERGED/green, with no
distinguishing audit event. **Fix (panel consensus):** don't push after the rebase — raise `GitError` so the
ticket re-trials + re-gates against the new tip; have `land_trial` return the actually-pushed sha for
`merge_sha`; emit a `land_rebase_retry` audit event. Tracked on EU-259 + EU-367.

---

## 6. Negative results (checked, found clean)

- 334/334 harnesses green at audit start (full suite, this host).
- `.gitignore` correctly excludes every secret-bearing runtime file (config.yaml/.bak, jira_connections,
  secrets.json, state/, *.lock/*.tmp) — no tracked secrets.
- The max-turns crash class (EU-248), reap self-halt (EU-334), and PID-file flaky class (EU-355 writers)
  are closed and stay closed under the audit's re-read.
- Telegram inbound is `chat_id`-gated; the launchd plists embed no secrets.

---

*This doc is the durable map, not a substitute for the tickets. Live acceptance criteria are on
EU-358..EU-372; cross-reference by ticket ID. Method + per-finding verdicts were produced by the
12-auditor + 3-lens-verifier workflow run wf_365d8c95-f67.*
