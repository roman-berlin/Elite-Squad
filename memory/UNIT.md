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

- 2026-06-17: Unit Memory established. Officers now read this protocol before acting.
<!-- SCRIBE:END -->
