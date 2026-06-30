# Technical Project Manager Report: EU-144 Officer Discussion Tickets

## Executive Summary

Parsed the officer discussion transcript from EU-144 and extracted **8 distinct, actionable issues** that require dedicated tickets. Each issue has been:

1. **De-duplicated** across officer mentions (e.g., DEV health check appeared from Scout, QA Engineer, SRE, and Sentinel — grouped into ONE ticket)
2. **Prioritized by severity** based on officer emphasis and numerical impact (CRITICAL → HIGH → MEDIUM)
3. **Written with full acceptance criteria** following Jira best practices
4. **Ready for filing** in project EU, assigned to ROMAN BERLIN

## Ticket Proposals (Sorted by Priority)

### CRITICAL (2 tickets)

**1. Wire DEV post-merge health check - 32 lands with zero automated verification**
- **Severity:** CRITICAL
- **Type:** Task
- **Rationale:** Scout, QA Engineer, SRE, and Sentinel ALL flagged this as the BIGGEST unresolved risk. 32 tickets merged to DEV with zero confirmed browser/e2e coverage. No rollback trigger exists when a merge breaks the live environment.
- **Impact:** Silent broken builds on DEV, no automated verification, manual discovery only
- **Key Acceptance Criteria:**
  - Build passes on DEV after merge
  - TypeScript checks pass
  - Dev server starts successfully
  - Basic auth smoke test in headless browser
  - Automatic firing after every DEV merge
  - On failure: SRE auto-reverts OR flags with clear rollback trigger

**2. Move tenant isolation check to commit-time - 37 security blocks burning passes**
- **Severity:** CRITICAL
- **Type:** Task
- **Rationale:** Provost Marshal and Security Officer both emphasized this as the single biggest security risk. 37 security blocks from missing explicit business_id filters. The check happens at the Provost gate instead of at build origin.
- **Impact:** Data leak vulnerability, runtime correctness unverified, 37 security blocks burning passes
- **Key Acceptance Criteria:**
  - Pre-commit check fires BEFORE code is pushed
  - Blocks any query touching multi-tenant table WITHOUT explicit tenant filter
  - Clear error message: "Multi-tenant query {table} lacks explicit tenant filter"
  - Reuses Builder's existing tenant-filter detection
  - Runs fast (< 2 seconds)
  - Defense-in-depth: Provost gate STILL runs

### HIGH (2 tickets)

**3. Wire Performance Engineer routing - 72 performance blocks burning passes**
- **Severity:** HIGH
- **Type:** Task
- **Rationale:** Engineering Manager and Dev Team Lead emphasized this. 72 performance blocks this cycle. Charter APPROVED in Lessons (2026-06-30) but not integrated/wired yet. Engineering Coach has protocol; Builder just needs to wire it.
- **Impact:** 72 performance blocks burning passes, benchmarking missing at build origin, no coverage countersignature
- **Key Acceptance Criteria:**
  - Builder runs performance check before Reviewer
  - Routing: Builder → Performance Engineer → Inspector
  - Before/after benchmark on hot paths
  - Coverage countersignature required before handoff
  - Charter requirements fully wired
  - Performance failures block the build

**4. Wire mandatory pre-build test questions into Vanguard identity file - 186 test blocks**
- **Severity:** HIGH
- **Type:** Task
- **Rationale:** Drillmaster, Engineering Coach, and Code Reviewer all emphasized this. 186 test blocks this cycle — the top failure category. Test Engineer is deployed, but ROOT CAUSE is discipline at build origin.
- **Impact:** 186 test blocks burning passes, Builder shipping PRs without tests, rework loop bleeding the unit
- **Key Acceptance Criteria:**
  - Pre-build questions card appears BEFORE first commit (not at gate time)
  - Three mandatory checkboxes (hard stop):
    1. Did I write a happy-path test for this change?
    2. Did I write a regression test covering the bug or edge case?
    3. Did I run the typecheck?
  - Same weight as reading CLAUDE.md
  - Answers logged to audit trail
  - Part of Vanguard's identity (not a brittle hook)
  - System prevents commit if any checkbox unchecked

### MEDIUM (4 tickets)

**5. Tighten pre-commit scope check - 19 max-effort hits from scope creep**
- **Severity:** MEDIUM
- **Type:** Task
- **Rationale:** Dev Team Lead and Field Engineer emphasized this. 19 max-effort hits from scope creep at build time. Standing Orders address this but soldiers aren't internalizing it.
- **Impact:** 19 tickets hitting max effort from out-of-scope changes, Builder burning passes on work outside ticket scope
- **Key Acceptance Criteria:**
  - Pre-commit scope check validates ticket bounds BEFORE first build pass
  - Reads acceptance criteria, compares against changed files
  - Flags changes that don't map to ticket scope
  - Clear error message
  - False-positive safe (allowlist for config/gate files)
  - Runs fast (< 3 seconds)

**6. Harden completeness doctrine at Reviewer gate - types/validation/tenant filters/error handling**
- **Severity:** MEDIUM
- **Type:** Task
- **Rationale:** Code Reviewer emphasized this. Builder has checklist but it's advisory, not blocking. These four elements are top causes of correctness blocks (96 this cycle).
- **Impact:** Missing completeness from PRs, 96 correctness blocks burning passes, gate is advisory instead of blocking
- **Key Acceptance Criteria:**
  - Reviewer gate BLOCKS if any of the four are missing:
    1. Complete typing (no any, bare dicts, unannotated functions)
    2. Input validation on all user-facing endpoints
    3. Explicit tenant filters on all multi-tenant queries
    4. Error handling on all fallible operations
  - Each check explicit and auditable
  - Clear error message
  - Builder aware of blocking conditions
  - No false positives on code that legitimately has no validation/error handling needs

**7. Add accessibility (a11y) pre-commit check - 27 a11y failures burning passes**
- **Severity:** MEDIUM
- **Type:** Task
- **Rationale:** Drillmaster emphasized this. 27 a11y failures this cycle — second-highest failure category after tests. Check exists in doctrine but soldiers aren't internalizing it.
- **Impact:** 27 a11y blocks burning passes, components shipped without keyboard navigation/ARIA/contrast
- **Key Acceptance Criteria:**
  - Pre-commit a11y check runs on every component file changed
  - Uses axe DevTools or equivalent
  - Checks: keyboard navigability, ARIA labels, color contrast
  - Fails with clear violation list
  - False-positive safe (allowlist for known patterns)
  - Runs fast (< 5 seconds)
  - Violations documented in build summary

**8. Improve spec discipline at origin - 96 correctness blocks from vague requirements**
- **Severity:** MEDIUM
- **Type:** Task
- **Rationale:** Code Reviewer emphasized this. 96 correctness blocks from logic bugs, edge cases, missing error handling. Root cause is underspecified tickets, not implementation bugs.
- **Impact:** 96 correctness blocks burning passes, Builder can't build correctly when spec is vague
- **Key Acceptance Criteria:**
  - Engineering Manager validates spec completeness BEFORE queuing to build
  - Required spec elements:
    1. Error handling: what happens on error?
    2. Edge cases: what are the boundary conditions?
    3. Validation: what inputs are valid/invalid?
  - Spec rejection is clear
  - Vague specs returned to Commander for clarification (not queued)
  - Spec validation runs fast
  - Validated specs produce better builds

## Issues NOT Created (Already in Lessons or Out of Scope)

- **Two-tenant smoke test:** Already approved and documented in Lessons (2026-06-30)
- **Test Engineer commission:** Already deployed (2026-06-30)
- **Performance Engineer commission:** Already approved (2026-06-30) — ticket created to WIRE it (issue #3 above)
- **Env parity / MAIN→DEV migration:** Explicitly out of scope per Standing Orders — DEV is the live environment until prod exists
- **CI workflow file changes:** Hard-guardrail protected — Commander applies by hand (EU-23 proven pattern)

## Prioritization Logic

Tickets are prioritized by **officer emphasis × numerical impact**:

| Priority | Criteria |
|----------|-----------|
| **CRITICAL** | Multiple senior officers flag as BIGGEST risk, OR security vulnerability with exploit potential, OR prevents rollback/protection |
| **HIGH** | >50 blocks per cycle, OR approved charter awaiting wiring, OR top-2 failure category |
| **MEDIUM** | 20-50 blocks per cycle, OR process improvement with clear impact |

## Next Steps for the Commander

1. **Review the 8 ticket proposals** in `officer_discussion_tickets.json`
2. **Run the filing script** with `--file` flag to create them in Jira project EU:
   ```bash
   # The unit's filing system will create tickets with:
   # - Assignee: ROMAN BERLIN
   # - Project: EU
   # - Priority: Set by severity field above
   # - Labels: officer-discussion, process-improvement
   ```
3. **Consider ordering:** CRITICAL tickets (1-2) should be tackled before HIGH (3-4), then MEDIUM (5-8)

## Files Created

1. **`officer_discussion_tickets.json`** — Machine-readable ticket proposals (ready for filing)
2. **`EU-144_TPM_REPORT.md`** — This human-readable report
3. **`create_tickets.py`** — Script to file tickets via Jira (requires JIRA_EMAIL/JIRA_API_TOKEN)

---

**Technical Project Officer — EU-144 Subtask Complete**
