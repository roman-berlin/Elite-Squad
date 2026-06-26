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
- **No separate production environment exists yet — DEV *is* the live environment.** Everything the
  Commander calls "production" today runs on DEV. The `vercel.json` `/api/*` rewrites that point at the
  DEV backend are **intentional and correct**: do NOT flag them as an env-parity defect, and do NOT
  freeze or hold DEV→MAIN over them. A dedicated production environment (separate backend + config) will
  be stood up before any real launch; re-activate the env-parity / production-cutover gates only once the
  Commander announces that prod exists. Until then, "ship to MAIN" is just an internal integration
  milestone, not a customer-facing deploy.
- Obey each repo's CLAUDE.md and .claude/rules (Bun-only, tenant isolation, zero-trust).
- **Stay strictly in the ticket's scope. NEVER change dependency versions, the lockfile
  (`bun.lock`/`package-lock`), `overrides`/`resolutions`, or any app the ticket doesn't name — unless
  the ticket is explicitly about that.** Most max-pass blow-ups are the Builder bundling unrelated churn
  (a Supabase bump, a type loosened to `any`, an out-of-scope app touched) that the Reviewer then blocks.
  If finishing seems to require a dep/lockfile change, STOP and leave it — do the in-scope work only.
- **CI workflow files (`.github/workflows/`) are Commander-applied, by design.** The hard guardrail
  permanently blocks writes there (a CI file can exfiltrate every secret on the runner). For a ticket
  whose only change is a CI workflow: do the full analysis + verification, POST THE EXACT READY-TO-APPLY
  DIFF as a ticket comment, escalate to the Commander ONCE, and STOP. Never re-attempt the write or loop —
  the Commander applies it by hand. (Proven on AUTO-23.)
- Escalate to the Commander only for critical product/strategy decisions — solve problems yourself.
- **Elite-Unit / the EU Jira project IS this orchestrator tool — NOT a product.** It is a Python 3.12 /
  Flask (cockpit) / Jira / Telegram CLI app. It has NO Supabase, NO Vercel, NO web frontend, NO
  multi-tenant DB / RLS, and is NOT part of the Automatixy monorepo. NEVER scaffold `apps/eu-app/`, a
  Supabase backend, RLS, a Vercel/HTTP probe, or any product/web infrastructure for EU, and never ask
  the Commander for Supabase keys "for EU". EU tickets are improvements to the unit's OWN Python
  codebase (`orchestrator/`, the cockpit, the officers, `tests/`).

## Per-App Notes

### automatixy
- Stack: React/Vite + FastAPI + Supabase. Branches: DEV / MAIN (uppercase). Jira project AUTO.
- (Add conventions, gotchas, and hot spots here as the unit learns them.)

### Elite-Unit (EU)
- This IS the orchestrator itself — a Python 3.12 / Flask cockpit / Jira / Telegram CLI tool. Repo:
  `~/Projects/General` (GitHub `roman-berlin/Elite-Unit`), branches `dev`/`main` (lowercase). Jira project EU.
- NO Supabase, NO Vercel, NO web app, NO database, NO RLS — it is not a SaaS product and not in the
  Automatixy monorepo. EU tickets = fixes/features in `orchestrator/` + `tests/`. Tests: `python3 tests/run_all.py`.

<!-- SCRIBE:BEGIN -->
## Lessons & Decisions  _(Technical Writer-maintained — newest first)_

- 2026-06-19: a11y ×9 and tests ×7 across 17 tickets confirm the Engineer gate has not yet proven it blocks these defects at build — today's proof case (Scout + Inspector cross-check on the `running` ticket) is the first real gate validation cycle.
- 2026-06-19: Zero browser/e2e coverage until Scout's smoke test today — all prior coverage was build-side only; assembled-app defects were entirely blind. Scout's DEV run is the unit's first live-app lens.
- 2026-06-19: Gate proof protocol: Engineer logs before/after finding-record; Inspector confirms those same defect classes do NOT reach his auditors; Scout reports runtime findings in the same schema. All three must close on one ticket before the gate is declared proven.
- 2026-06-19: Provost and Quartermaster remain PLANNED — pre-flight (secret-scan + cross-tenant SELECT + env-parity on `vercel.json`) has not been executed; no signal until Commander authorizes.
- 2026-06-18: Scout (QA) activated — keyboard happy-path smoke test on the `running` ticket is the debut proof case, scoped to runtime/a11y. Provost and Quartermaster stay PLANNED with checks riding the Commander's pre-flight.
- 2026-06-18: All three verifiers (Engineer, Scout, Inspector) log to one shared finding-record schema — ticket, flow, axe rule-id, coverage delta, pass/fail — so numbers cross-check across lenses.
- 2026-06-18: Diff-tag DEV-ahead commits before any downstream scan — freeze the surface so Provost's secret-scan/cross-tenant SELECT and QM's env-parity audit hit a stable diff.
- 2026-06-18: Promotion ≠ deploy — DEV→MAIN as integration milestone is defensible on green build/types/migrations alone; production cutover is a separate gate (legal attestations + deploy-config). "Merge is clean" never implies "ship is clean."
- 2026-06-18: Green build/typecheck hides cross-environment config defects — every prod `vercel.json` `/api/*` rewrite pointed at the DEV backend while all gates stayed green. Env-parity is a BLOCKING pre-flight: all prod `/api/*` targets must resolve to prod backend before cutover.
- 2026-06-18: Migration mechanics ≠ migration outcome — QM can certify RLS migrations as idempotent/txn-wrapped/rollback-present while resulting policies fail open. Tenant-RLS is BLOCKING: require a scratch-DB cross-tenant SELECT returning zero rows before prod.
- 2026-06-18: A defined BLOCKING check isn't a run check — Provost's secret-scan and tenant-RLS only gate once actually executed across DEV-ahead commits; Commander pre-flight must run them, not assume existence cleared them.
- 2026-06-18: Irreversible/binary defects bypass the "prove-it-before-gating" rule — Provost secret-scan and QM migration-file-exists are BLOCKING immediately: a leaked key or silent schema divergence can't be un-shipped.
- 2026-06-18: Gate activation doctrine — observation-only checks graduate to blocking one per cycle, on data; never pad the corps or harden a gate before it has run a single ticket.
- 2026-06-18: A new build-side gate is unproven until instrumented — tag a11y/coverage pre- AND post-gate; Inspector cross-checks auditor findings against those numbers. Add only one build-side rule per cycle.
- 2026-06-18: Diff-level review is blind to assembled-app defects (focus traps, broken live regions, route-change regressions) — these need a running-app lens on DEV (Scout), not a sharper patch audit.
- 2026-06-18: Standing FE Knowledge requirement — keyboard happy-path: tab every control, operate it, assert focus visible/never-lost, zero console errors; assert against assembled DEV build where flow crosses route change or live region.
- 2026-06-16: Engineer exit gate hard-requires axe-core-clean AND non-negative coverage delta — a11y and tests are the unit's systemic FE defects; kill at build origin, not in review cycles.
- 2026-06-16: Enforce build rules at a single point — edit the Engineer's exit gate only; leave `post-dev-checklist` untouched to avoid duplicate/conflicting enforcement.
- 2026-06-16: Repeated max-effort burns on one ticket = work scoped wrong or exit criteria undefined at build time; treat as signal to tighten exit gates, not to retry harder or add bodies.
- 2026-06-17: Unit Memory established. Officers read this protocol before acting.
<!-- SCRIBE:END -->
