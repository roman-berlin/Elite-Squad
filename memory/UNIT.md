# Elite Unit — Living Protocol (Unit Memory)

The unit's shared memory. Every officer reads this before acting. The Commander owns the
Mission, Standing Orders, and Per-App Notes; the Scribe maintains only the Lessons &
Decisions log (between the markers). Keep it tight and current.

## Mission

Implement the Commander's Jira tickets across his products autonomously and safely:
build → gate → review → security → land on DEV → QA, on isolated worktrees, never touching
MAIN. Quality and tenant-safety over speed.

## Standing Orders (Commander)

- Only work tickets assigned to ROMAN BERLIN.
- Never touch MAIN; land on DEV, move the ticket to QA.
- Obey each repo's CLAUDE.md and .claude/rules (Bun-only, tenant isolation, zero-trust).
- Escalate to the Commander only for critical product/strategy decisions — solve problems yourself.

## Per-App Notes

### automatixy
- Stack: React/Vite + FastAPI + Supabase. Branches: DEV / MAIN (uppercase). Jira project AUTO.
- (Add conventions, gotchas, and hot spots here as the unit learns them.)

<!-- SCRIBE:BEGIN -->
## Lessons & Decisions  _(Scribe-maintained — newest first)_

- 2026-06-16: Engineer exit gate now hard-requires axe-core-clean AND non-negative coverage delta in `frontend-engineer.md` — a11y and tests failed every Inspector pass (×4 each) on the unit's first ticket because the build side had no mandatory stop. Kill these defects at build origin, not in review cycles.
- 2026-06-16: Inspector A11y and Test-Coverage auditors are calibrated correctly (caught every instance) — they now serve as final verifiers, not primary catch. Recurring Inspector bounces signal a missing build-side gate, not a check-side gap.
- 2026-06-16: Enforce build rules at a single point — edit the Engineer's exit gate only; leave `post-dev-checklist` untouched to avoid duplicate/conflicting enforcement.
- 2026-06-16: Three max-effort burns on one ticket = work harder than scoped or exit criteria undefined at build time; treat repeated max-effort as a signal to tighten exit gates, not retry harder.
- 2026-06-16: Hold roster as constituted; don't make personnel calls off a one-ticket baseline — re-examine passes/ticket after two more tickets before any corps change.
- 2026-06-17: Unit Memory established. Officers now read this protocol before acting.
<!-- SCRIBE:END -->
