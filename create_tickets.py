#!/usr/bin/env python3
"""Create Jira tickets from officer discussion findings (EU-144).

Technical Project Manager subtask: Parse the officer discussion transcript and
create well-defined Jira tickets for each distinct issue, prioritized by impact.
"""

import sys
import os
from pathlib import Path

# Add the orchestrator directory to the path
repo_root = Path(__file__).parent
sys.path.insert(0, str(repo_root))

from orchestrator.backlog.jira import JiraAdapter
from orchestrator.config import AppConfig

# Officer discussion issues extracted from EU-144 transcript
# Each issue: title, type, severity, body (description + acceptance criteria)
OFFICER_ISSUES = [
    {
        "title": "Wire DEV post-merge health check - 32 lands with zero automated verification",
        "type": "Task",
        "severity": "CRITICAL",
        "body": """**What:** Stand up the automated DEV health check that fires after every merge and confirms the build actually passes.

**Where:** orchestrator/sentinel.py (SRE post-merge integration)

**Why it matters:**
- 32 tickets merged to DEV with zero confirmed browser/e2e coverage running against it
- Scout, QA Engineer, SRE, and Sentinel all flagged this as the BIGGEST unresolved risk
- Every defect caught on diff is a diff-level catch; what's actually broken in the assembled app on DEV is invisible
- If a merge breaks the DEV build today, we find out manually or not at all
- No rollback trigger exists — SRE has theoretical authority, not actual

**Impact:**
- Silent broken builds on DEV that persist until manual discovery
- No automated post-merge verification that the assembled app actually runs
- Missing rollback trigger when a merge breaks the live environment

**Acceptance Criteria:**
- Build passes on DEV after merge (gate commands run clean)
- TypeScript checks pass (npx tsc --noEmit or equivalent)
- Dev server starts successfully (smoke test confirms app actually runs)
- Basic auth smoke test confirms auth + protected-route redirect work in headless browser
- Check fires AUTOMATICALLY after every DEV merge (no manual trigger)
- On failure: SRE auto-reverts merge OR flags it with clear rollback trigger (Telegram + audit + ticket comment)
- Health check runs within bounded timeout (doesn't hang or exhaust resources)"""
    },
    {
        "title": "Move tenant isolation check to commit-time - 37 security blocks burning passes",
        "type": "Task",
        "severity": "CRITICAL",
        "body": """**What:** Implement a pre-commit lint rule or code pattern check that flags any Supabase query WITHOUT an explicit tenant filter before the code is even pushed.

**Where:** Builder pre-commit checks / Vanguard's identity file / static analysis guard

**Why it matters:**
- 37 security blocks this cycle from missing explicit business_id filters in Supabase queries
- Provost Marshal: \"the single biggest unresolved risk I carry right now is tenant isolation at the query layer\"
- Security Officer: \"A tenant guard that fails-open or a query missing the filter is a data leak waiting to happen\"
- The check happens at the Provost gate, not at build origin — burns a full pass every time it fires
- Runtime authz-bypass defects bypass ALL gates (types, validation, diff review)

**Impact:**
- Tenant isolation bypass at query layer = data leak vulnerability
- 37 security blocks burning passes when the defect should be caught at commit time
- Runtime correctness unverified — RLS plus explicit filter may not behave correctly end-to-end

**Acceptance Criteria:**
- Pre-commit check fires BEFORE code is pushed (git hook or equivalent)
- Any query touching a multi-tenant table WITHOUT explicit business_id filter is blocked
- Error message is clear: \"Multi-tenant query {table} lacks explicit tenant filter — add AND business_id = auth.uid() or equivalent\"
- Check doesn't false-positive on single-tenant tables or auth.uid() already present
- Builder's existing tenant-filter detection is reused (don't duplicate logic)
- Check runs fast (< 2 seconds) so it doesn't slow commit workflow
- Security Officer's Provost gate STILL runs (this is defense-in-depth, not replacement)"""
    },
    {
        "title": "Wire Performance Engineer routing - 72 performance blocks burning passes",
        "type": "Task",
        "severity": "HIGH",
        "body": """**What:** Integrate the Performance Engineer charter into the build sequence (Builder → Performance Engineer → Inspector routing).

**Where:** orchestrator/builder.py (performance check integration), Engineering Coach protocol

**Why it matters:**
- 72 performance blocks this cycle — the ×72 gap
- Engineering Manager: \"the roster's actually solid now — the gap isn't headcount, it's deployment doctrine\"
- Performance Engineer commission APPROVED in Lessons (2026-06-30) but not integrated/wired yet
- Dev Team Lead: \"72 performance blocks are burning passes because the charter isn't live\"
- Charter exists; Engineering Coach has integration protocol; Builder just needs to wire it

**Impact:**
- 72 performance blocks per cycle burning passes when they should be caught before first build
- Performance benchmarking missing from hot paths at build origin
- No coverage countersignature before handoff to Inspector

**Acceptance Criteria:**
- Builder runs performance check before Reviewer (routing: Builder → Performance Engineer → Inspector)
- Performance Engineer executes before/after benchmark on hot paths
- Coverage countersignature required before handoff to Inspector
- Charter requirements fully wired (benchmark threshold, regression detection, optimization gate)
- Performance check output included in build summary
- Performance failures block the build (same as any other gate failure)"""
    },
    {
        "title": "Wire mandatory pre-build test questions into Vanguard identity file - 186 test blocks",
        "type": "Task",
        "severity": "HIGH",
        "body": """**What:** Wire a mandatory pre-build questions card into Vanguard's identity file — three explicit checkboxes that must be answered before the first commit.

**Where:** Vanguard's identity file (officer definition), pre-commit hook or build-start guard

**Why it matters:**
- 186 test blocks this cycle — the top failure category
- Drillmaster: \"tests ×40 and a11y ×27 are the top two failures, and both have written fixes sitting in the officer files that soldiers aren't internalizing\"
- Engineering Coach: \"soldiers aren't internalizing 'tests WITH the code' despite standing orders\"
- Code Reviewer: \"tests ×197 is what's bleeding passes — soldiers still aren't writing them with the code\"
- The Test Engineer is deployed, but ROOT CAUSE is discipline at build origin

**Impact:**
- 186 test blocks burning passes when tests should be written WITH the code
- Builder keeps shipping PRs without tests, Reviewer keeps blocking them
- Rework loop bleeding the unit

**Acceptance Criteria:**
- Pre-build questions card appears BEFORE the first commit, not at gate time
- Three mandatory checkboxes (hard stop — build doesn't begin until answered):
  1. Did I write a happy-path test for this change?
  2. Did I write a regression test covering the bug or edge case?
  3. Did I run the typecheck?
- Same weight as reading CLAUDE.md (can't proceed until answered)
- Answers are logged to audit trail (for review/analysis)
- Questions are part of Vanguard's identity (not a brittle hook)
- System prevents code commit if any checkbox is unchecked"""
    },
    {
        "title": "Tighten pre-commit scope check - 19 max-effort hits from scope creep",
        "type": "Task",
        "severity": "MEDIUM",
        "body": """**What:** Implement a tighter pre-commit scope validation that the Builder runs BEFORE the first pass to catch out-of-scope work early.

**Where:** orchestrator/builder.py (scope validation), pre-commit check

**Why it matters:**
- 19 max-effort hits this cycle from scope creep at build time
- Field Engineer: \"scope creep at build time — Vanguard pulling in unrelated changes that the Reviewer then blocks\"
- Dev Team Lead: \"scope creep at build time burning more compute than the ticket deserves\"
- Standing Orders already address this but soldiers aren't internalizing it
- Tightening the check BEFORE the first pass cuts drag before it burns compute

**Impact:**
- 19 tickets hitting max effort from out-of-scope changes
- Builder burning passes on work outside the ticket's scope
- Reviewer blocking after compute already spent

**Acceptance Criteria:**
- Pre-commit scope check validates ticket bounds BEFORE first build pass
- Check reads the ticket's acceptance criteria and compares against changed files
- Flags any changed file/path that doesn't map to the ticket's stated scope
- Clear error message: \"File {path} is outside ticket scope — either update the ticket or remove the change\"
- False-positive safe (allowlist for config/gate files that legitimately change)
- Reviewer's scope validation is kept (defense-in-depth)
- Scope check runs fast (< 3 seconds for typical diffs)"""
    },
    {
        "title": "Harden completeness doctrine at Reviewer gate - types/validation/tenant filters/error handling",
        "type": "Task",
        "severity": "MEDIUM",
        "body": """**What:** Make the Builder's completeness checklist (types, validation, tenant filters, error handling) BLOCKING at the Reviewer gate, not advisory.

**Where:** orchestrator/reviewer.py (blocking conditions), Builder checklist enforcement

**Why it matters:**
- Code Reviewer: \"the Builder already has the checklist (types, validation, tenant filters, error handling). Make it blocking, not a suggestion\"
- Completeness doctrine enforcement gap — checklist exists but doesn't fire at gate
- Lessons & Decisions log: enforcement is missing at the process level
- These four elements are the top causes of correctness blocks (96 this cycle)

**Impact:**
- Types, validation, tenant filters, and error handling missing from PRs
- Correctness blocks burning passes (96 this cycle)
- Reviewer gate is advisory instead of blocking on completeness

**Acceptance Criteria:**
- Reviewer gate BLOCKS if any of the four are missing:
  1. Complete typing (no any, bare dicts, or unannotated functions)
  2. Input validation on all user-facing endpoints
  3. Explicit tenant filters on all multi-tenant queries
  4. Error handling on all fallible operations
- Each check is explicit and auditable (reviewer lists which of the four failed)
- Error message is clear: \"Completeness check failed: missing {types|validation|tenant_filter|error_handling}\"
- Builder is aware of the blocking conditions (can't skip them)
- Gate doesn't false-positive on code that legitimately has no validation/error handling needs"""
    },
    {
        "title": "Add accessibility (a11y) pre-commit check - 27 a11y failures burning passes",
        "type": "Task",
        "severity": "MEDIUM",
        "body": """**What:** Implement a pre-commit accessibility check using axe DevTools or equivalent that validates every changed component.

**Where:** Pre-commit hook / gate commands, Vanguard's identity file

**Why it matters:**
- 27 a11y failures this cycle — second-highest failure category after tests
- Drillmaster: \"a11y ×27 are the top two failures, and both have written fixes sitting in the officer files\"
- Check exists in doctrine but soldiers aren't internalizing it
- Every officer flagged a11y as a burn area

**Impact:**
- 27 a11y blocks burning passes when violations should be caught at commit time
- Components shipped without keyboard navigation, ARIA labels, or contrast checks
- QA Engineer blocking PRs for basic a11y violations

**Acceptance Criteria:**
- Pre-commit a11y check runs on every component file changed in the PR
- Uses axe DevTools or equivalent automated a11y scanner
- Checks minimum: keyboard navigability, ARIA labels where needed, color contrast
- Fails with clear violation list: \"a11y violation in {component}: {violation type}\"
- False-positive safe (allowlist for known-satisfactory patterns)
- Check runs fast (< 5 seconds for typical component changes)
- Violations are documented in the build summary (not just a silent fail)"""
    },
    {
        "title": "Improve spec discipline at origin - 96 correctness blocks from vague requirements",
        "type": "Task",
        "severity": "MEDIUM",
        "body": """**What:** Tighten spec discipline at origin — Engineering Manager rejects underspecified specs (no error handling, no edge cases, no validation rules) BEFORE they reach the build queue.

**Where:** orchestrator/pm.py (spec validation), Engineering Manager intake process

**Why it matters:**
- Code Reviewer: \"correctness ×96 is quieter and just as expensive — that's logic bugs, edge cases, and missing error handling\"
- \"a lot of those correctness blocks come from vague tickets — if the spec doesn't say what happens on error, what the edge cases are, or what validation applies, the Builder can't build it right\"
- 96 correctness blocks from logic bugs, edge cases, missing error handling
- Root cause is underspecified tickets, not implementation bugs

**Impact:**
- 96 correctness blocks burning passes from logic bugs and edge cases
- Builder can't build correctly when spec is vague on error/edge cases
- Reviewer catches what should have been in the spec

**Acceptance Criteria:**
- Engineering Manager validates spec completeness BEFORE queuing to build
- Required spec elements:
  1. Error handling: what happens on error? (retry, fail gracefully, user message?)
  2. Edge cases: what are the boundary conditions? (null, empty, max length?)
  3. Validation: what inputs are valid/invalid? (format, range, type?)
- Spec rejection is clear: \"Spec underspecified: missing {error_handling|edge_cases|validation}\"
- Vague specs are returned to the Commander for clarification (not queued)
- Spec validation runs fast (automated check of acceptance criteria)
- Validated specs produce better builds (fewer correctness blocks)"""
    }
]


def create_tickets():
    """Create all the Jira tickets for the officer discussion issues."""
    # Create a mock AppConfig for the EU project
    # In production, this would come from config.yaml
    eu_app = AppConfig(
        name="elite-unit",
        repo_path="/Users/romanberlin/Projects/General",
        base_branch="dev",
        protected_branch="main",
        backlog_backend="jira",
        backlog={
            "base_url": "https://yourteam.atlassian.net",  # Will be overridden by env/connection
            "project_key": "EU",
            "ready_status": "To Do",
        }
    )

    try:
        adapter = JiraAdapter(eu_app)
        print("Jira adapter initialized successfully")
    except Exception as e:
        print(f"Failed to initialize Jira adapter: {e}")
        print("\nThis script requires:")
        print("  - JIRA_EMAIL and JIRA_API_TOKEN environment variables set")
        print("  - A valid Jira connection configured (or use Quick Connect in cockpit)")
        return 1

    print(f"\nCreating {len(OFFICER_ISSUES)} tickets for officer discussion issues...\n")

    filed = []
    deduped = []
    failed = []

    for issue in OFFICER_ISSUES:
        title = issue["title"]
        severity = issue["severity"]

        # Check for duplicate
        try:
            existing = adapter.find_open_by_summary(title)
            if existing:
                print(f"↺ {existing} already open — [{severity}] {title}")
                deduped.append(existing)
                continue
        except Exception as e:
            print(f"⚠ Duplicate check failed for '{title}': {e}")

        # Create the ticket
        try:
            # Build full description with severity header
            description = f"**Severity:** {severity}\n\n{issue['body']}"

            key = adapter.create_task(
                summary=title,
                description=description,
                labels=["officer-discussion", "process-improvement"],
                issue_type=issue["type"]
            )

            if key:
                print(f"✓ {key} filed — [{severity}] {title}")
                filed.append(key)
            else:
                print(f"✗ filing not supported — {title}")
                failed.append(title)
        except Exception as e:
            err = str(e)[:100]
            print(f"✗ failed ({err}) — {title}")
            failed.append(title)

    print(f"\n=== Summary ===")
    print(f"Filed:    {len(filed)}")
    print(f"Deduped:  {len(deduped)}")
    print(f"Failed:   {len(failed)}")

    if filed:
        print(f"\nCreated tickets: {', '.join(filed)}")

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(create_tickets())
