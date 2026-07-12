# Elite-Unit — 2026-07-12 Delta-Audit Report — Index of Findings (EU-262)

**Date:** 2026-07-12 · **Type:** read-only delta audit (system live, two drains running throughout;
auditor never touched code/git/processes — Jira-only output). **Method:** a 104-agent fleet across 6
workstreams (delta metrics · crash root-cause · product-quality sample · security · economics ·
fresh-eyes), every finding adversarially refuted-first and deduped against all 129 open tickets at
audit time. **Scope:** EU only per Roman's steer — no AUTO tickets/comments were created; product-quality
findings are folded into the EU pipeline tickets below plus the appendix at the end of this doc.

This document is the durable index — the full findings live on the Jira tickets it points to; this is
the map so the next audit (or the next reader) doesn't have to re-derive it from 129 tickets.

---

## 1. Headline: the "#1 open mystery" is root-caused

The repeated `Command failed with exit code 1` CLI deaths that killed the EU-228/EU-232 attempts are
**not crashes** — they are **max-turns exhaustion** (turn 97 against a cap of 96) surfacing as a
process error.

Commit `95042c2` (07-10 13:43) made `agent.py:357` swallow the SDK's `error_max_turns`, so
`loop.py:992` routes those results to a generic `ERRORED` instead of the purpose-built scrum-split
handler — which has been **unreachable ever since** (0 turn-limit splits after the land, vs. 6 before).

**Result:** 20 doomed 97-turn re-builds, ~$180 over 30h (~36% of post-07-10 spend), plus ~$70 more that
is unmetered/invisible to the budget gates.

→ **EU-248 (Highest, do first)** — unblocks EU-228 and EU-232.

---

## 2. Delta metrics — BEFORE (07-07..09) vs AFTER (07-10..11 21:20)

| Metric | Before EU | Before AUTO | After EU | After AUTO |
|---|---|---|---|---|
| ticket_starts / distinct | 28 / 19 | 34 / 29 | 42 / 27 | 64 / 38 |
| merged | 12 | 14 | 12 | 24 |
| lands / start | 0.43 | 0.41 | 0.29 | 0.38 |
| builds per land (mean) | 1.58 | 1.21 | 1.25 | 1.42 |
| needs_human | 5 | 9 | 4 | 5 |
| ticket_exceptions | 2 | 2 | 8 | 8 |
| tokens on non-landing work | 43% | — | 53% | — |

**Landed-fix verdicts:**
- `11bc9bb` gate-flake re-run — **MOVED** (stuck-count 2 → 0)
- `12ecb49` stuck-column routing — **MOVED**
- `95042c2` error-result degrade — **MOVED but REGRESSED SIDEWAYS** (severed the turn-limit split → EU-248)
- EU-219 park-repeat-ERRORED — **MOVED** (but only after 26-42M wasted tokens/ticket)
- EU-229 decision lifecycle — **MOVED** (no ghosts)
- EU-223 + `89e1f39` per-app/active-provider budget — **MOVED / TOO-EARLY** (per-app live only ~2h at audit time)
- bun-`ENOENT` PATH — **already fixed same-day** (`c2f5f17`)

---

## 3. Economics (07-07..now)

**$755.90 / 778.6M tokens.** Daily burn accelerating: 07-09 $140 → 07-10 $187 → 07-11 $311.

Spend by role: Builder 74% · Reviewer 11.7% (all ticket-unattributed) · Planner 10.7% · PM 3.1% ·
ceremony ~0.5%. Median landed ticket: **$2.88**.

**Top-3 waste:**
1. Turn-cap re-burn — **$180.82 / 30h** → EU-248.
2. Escalation/park burn — **~$137-148 (~22%)** → EU-230 comment.
3. Reviewer spend unattributed to any ticket → EU-258.

---

## 4. New EU tickets filed (14)

### Highest
| Ticket | Title |
|---|---|
| **EU-248** | `[autopilot/loop]` Max-turns exhaustion misclassified as a crash; turn-limit scrum-split unreachable since `95042c2` (~$180/30h). **← do first.** |

### High — pipeline verification honesty
*(the biggest quality risk: the unit ships unverified work to DEV marked PASS)*

| Ticket | Title |
|---|---|
| EU-249 | `[gate]` Pre-merge gate runs no tests & never checks test-file collectability (admitted-red + phantom-path tests merge). |
| EU-250 | `[reviewer/planner]` Reviewer asserts `spec_met=true` on run-the-tests ACs it can't execute; planner silently yields `testable_ac=0` on ~15% of BUILD plans. |
| EU-251 | `[loop/land]` CI-fix tickets advance on false "CI will go green" claims; land path never reads the GH Actions conclusion. |

### High — autonomy / reliability
| Ticket | Title |
|---|---|
| EU-252 | `[autopilot]` Drain starves In Progress (EU-233..237 + AUTO-3/4/5/28 stranded); jql-override + `[:cap]`-before-tier-split break resume-first. |
| EU-253 | `[observability]` Automode drains write zero per-ticket run logs (blinded this audit's crash forensics). |
| EU-256 | `[concurrency]` Unlocked/merge-less shared-state writers (`error_counts.json` overwrite defeats park-after-3; `backend_pref`/`save_blocked`/changelog). |
| EU-257 | `[telegram]` `poll_loop` has no process singleton/stop/offset-lock → leaked pollers double-route Commander replies on the VPS. |

### High — security
| Ticket | Title |
|---|---|
| EU-254 | `[cockpit]` No CSRF/Origin/Host validation on ~30 state-changing POST endpoints (broader than EU-187). |
| EU-255 | `[security]` Officer & gate subprocesses inherit the unit's full env (JIRA/TELEGRAM tokens) while building untrusted product code. |

### Medium — debt
| Ticket | Title |
|---|---|
| EU-258 | `[economics]` Reviewer/PM ledger rows carry no `ticket_id`/`pass` (~16% spend unattributable). |
| EU-259 | `[git]` `land_trial` non-ff recovery rebases & pushes without re-gating; stale `merge_sha` for rollback. |
| EU-260 | `[docs]` `ORG.md` & `roster.py` still ship the deleted Test Engineer (daily roster regenerates the phantom). |
| EU-261 | `[loop/land]` Land commits embed the builder's raw chat transcript + 185-char subjects. |

---

## 5. Evidence comments added to existing tickets (5)

- **EU-228** — the exit-1 class is 8/9 turn-limit (not infra); must not shadow EU-248; a DNS blip
  charged 4 tickets a strike in one second; bun half already fixed.
- **EU-222** — premise stale: the SDK already prices `glm-4.6` ($295 all-time in the ledger); gate
  against observed rates.
- **EU-230** — escalation/park burn sized (~$137-148, ~22%); engage the PM after the first failed review.
- **EU-198** — QA sign-off evidence: reviewer heavy tail is gone post-fix (max $1.45 vs. $21.06 outlier).
- **EU-224** — keepalive respawns from a dirty tree (live at audit time: uncommitted `sentinel.py` in
  the SRE revert path); add a dirty flag + refuse-on-dirty.

---

## 6. Top-3 for the drain queue

1. **EU-248** — largest daily bleed, root-causes the crash class, unblocks EU-228/EU-232.
   Deterministic, testable, no LLM.
2. **EU-249 → EU-250 → EU-251** (verification-honesty cluster) — the pipeline is currently certifying
   test/CI outcomes it never observed; EU-249 first (make the gate actually run the diff's tests).
3. **EU-252** — a whole epic (EU-233..237) is invisibly stranded; cheap ordering fix, restores
   resume-first for both drains.

---

## 7. Product-quality sample (7 AUTO lands reviewed on DEV)

Verdicts kept in this report only — **no AUTO tickets were filed**, per scope.

| Ticket | Verdict | Note |
|---|---|---|
| AUTO-97 | FAIL | Landed with author-admitted failing tests (localStorage mock) + unused `useParams` import → EU-249 |
| AUTO-101 | FAIL | 481-line test at phantom path `apps/zeltivo-crm/apps/zeltivo-crm/…`, never collected + broken RRD mock → EU-249 |
| AUTO-109 | FAIL | Central e2e AC self-admittedly unmet, reviewer PASS `spec_met=true` → EU-250 |
| AUTO-112 | PASS w/ notes | CI red on merge; "CI will go green" claim was false → EU-251 |
| AUTO-117 | PASS w/ notes | KPI cards `role=button` hide the value from screen readers (a11y regression) |
| AUTO-121 | PASS w/ notes | Funnel conversion semantics diverge between doc and code |
| AUTO-127 | PASS w/ notes | Fine |

**3 FAIL / 4 PASS-with-notes / 0 clean PASS** across 7 sampled lands — the pipeline's taste is the
systemic finding, now owned by EU-249/250/251.

> Completeness-critic caveat: only 7 of ~33 lands were sampled and the 12 EU self-lands got no quality
> review — a follow-up audit should generalize the FAIL rate.

---

## 8. Product-side items for Roman's hands (not filed as AUTO, per scope)

- Clean up the 2 stranded test files on product DEV (`apps/zeltivo-crm/apps/zeltivo-crm/…` from AUTO-101
  + AUTO-95) and fix AUTO-101's `import('react-router-dom')` mock (use `await vi.importActual`).
  EU-249 prevents recurrence.
- `chmod 600` the product repo `.env` (currently `0644`, holds `SUPABASE_SERVICE_ROLE_KEY`, readable by
  the officer building in that tree — see EU-255).

---

## 9. Refuted / dropped (so the next audit doesn't redo them)

- "Fragment-chain continues past a failed fragment" — skeptic disproved.
- AUTO-122 shared-setup root-cause.
- Edge-function end-before-start.
- `anonClient` nullable typecheck.

**Negative results** (checked, found clean): launchd plist embeds no secrets (PATH only); Telegram
inbound is `chat_id`-gated; secret-value leak grep across `state/logs` = 0 (secrets hygiene clean);
`state/audit.jsonl` + `usage_ledger` parse 100% clean under two live drains.

---

*Companion index for [SYSTEM_AUDIT_2026-07-06.md](SYSTEM_AUDIT_2026-07-06.md) — that audit covered
full-system combat-readiness; this one is the 07-07..07-12 delta on top of it. Cross-reference by
ticket ID (EU-248..EU-261) for the live acceptance criteria; this doc is the durable map, not a
substitute for the tickets themselves.*
