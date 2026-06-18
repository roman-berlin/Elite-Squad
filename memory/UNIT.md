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

- 2026-06-18: Scout (QA) activated — the "QA on DEV" terminal step finally has a live owner; debut is the keyboard happy-path smoke test on the `running` ticket, scoped strictly to runtime/a11y. Activation only (file pre-drafted), not recruitment. Provost and Quartermaster stay PLANNED with checks riding the Commander's pre-flight.
- 2026-06-18: All three verifiers (Engineer build-gate, Scout runtime, Inspector diff-audit) log to one shared gate-comparable finding-record schema — ticket, flow, axe rule-id, coverage delta, pass/fail. One doctrine artifact, not per-officer formats, so numbers cross-check across lenses and corps decisions rest on comparable data.
- 2026-06-18: Diff-tag DEV-ahead commits before any downstream scan — freeze the surface so Provost's secret-scan/cross-tenant SELECT and the Quartermaster env-parity audit hit a stable diff, not a moving target.
- 2026-06-18: Promotion ≠ deploy — DEV→MAIN as an *integration* milestone is defensible on green build/types/migrations alone; a production *cutover* is a separate gate (legal attestations + deploy-config) those checks never touch. "Merge is clean" never implies "ship is clean."
- 2026-06-18: Green build/typecheck hides cross-environment config defects — every prod `vercel.json` `/api/*` rewrite pointed at the DEV backend while all gates stayed green. Env-parity is now an evidence-backed BLOCKING pre-flight: all prod `/api/*` targets must resolve to the prod backend before cutover.
- 2026-06-18: Migration *mechanics* ≠ migration *outcome* — QM can certify RLS migrations as idempotent/txn-wrapped/rollback-present while the resulting policies fail open and pass every gate. Tenant-RLS is BLOCKING: require a scratch-DB cross-tenant SELECT returning zero rows before prod.
- 2026-06-18: A *defined* BLOCKING check isn't a *run* check — Provost's secret-scan and tenant-RLS verification only gate once actually executed across the DEV-ahead commits; Commander pre-flight must run them, not assume the gate's existence cleared them.
- 2026-06-18: Irreversible/binary defects bypass the "prove-it-before-gating" rule — Provost secret-scan and QM migration-file-exists ride as BLOCKING immediately (no observation cycle): a leaked key or silent schema divergence can't be un-shipped. Env-parity and tenant-scope graduated to blocking on demonstrated evidence.
- 2026-06-18: Gate activation doctrine — observation-only checks graduate to blocking gates one per cycle, on data; never pad the corps or harden a gate before it has run a single ticket. New verification posts stand up only after the prior gate proves out.
- 2026-06-18: A new build-side gate is unproven until instrumented — when running the first ticket under it, tag a11y/coverage pre- AND post-gate and have the Inspector cross-check his auditor findings against those numbers, to confirm the gate kills defects at the bench vs merely shifting them downstream. Add only one build-side rule per cycle.
- 2026-06-18: Diff-level review is structurally blind to assembled-app defects (focus traps, broken live regions, route-change regressions) — these need a running-app lens on DEV (Scout), not a sharper patch audit. Recurring Inspector bounces signal a missing lens, not a miscalibrated check.
- 2026-06-18: Standing FE Knowledge requirement — keyboard happy-path test: tab every control, operate it, assert focus visible/never-lost, zero console errors; assert against the assembled DEV build where a flow crosses a route change or live region.
- 2026-06-16: Engineer exit gate hard-requires axe-core-clean AND non-negative coverage delta in `frontend-engineer.md` — a11y and tests are the unit's systemic FE defects (every other category is isolated noise). Kill these at build origin, not in review cycles.
- 2026-06-16: Inspector A11y and Test-Coverage auditors are calibrated correctly (caught every instance) — they serve as final verifiers, not primary catch.
- 2026-06-16: Enforce build rules at a single point — edit the Engineer's exit gate only; leave `post-dev-checklist` untouched to avoid duplicate/conflicting enforcement.
- 2026-06-16: Repeated max-effort burns on one ticket = work scoped wrong or exit criteria undefined at build time; treat it as a signal to tighten exit gates, not to retry harder or add bodies.
- 2026-06-16: Hold roster; make personnel calls on data, not a thin baseline — re-examine passes/ticket after the new gate runs full cycles before any corps change.
- 2026-06-17: Unit Memory established. Officers read this protocol before acting.
<!-- SCRIBE:END -->
