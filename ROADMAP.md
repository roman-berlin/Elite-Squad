# The Elite Unit — Roadmap

The durable plan. The live task list in Cowork mirrors this, but this file is the source of
truth (version-controlled, reviewable on GitHub). Update it as we ship.

Last updated: 2026-06-16.

## Shipped

- **Autonomous pipeline** — build → gate → review → **security gate (Provost)** → land on DEV → QA, on an isolated git worktree; MAIN never touched.
- **Seven officers** — Adjutant (S-1/HR) · Field Engineer (Builder) · Inspector General (Reviewer) · Scout (S-2/QA) · Provost Marshal (Security) · Quartermaster (S-4/DevOps) · Drillmaster (Doctrine).
- **Autopilot** — always-on worker: resume In Progress, else take top To Do (assignee-pinned to you), with a park-guard so it never spins on a stuck ticket; KeepAlive launchd.
- **Daily council** — 10:00 muster, briefing to Telegram, transcript saved, escalates only Commander-level calls.
- **Two-way Telegram** — the General answers you; your replies become standing guidance.
- **Self-improvement** — `drill --apply` writes approved officer/squad edits (backed up).
- **Self-staffing** — Adjutant proposes/executes hires (HR-gated): soldiers + junior officers under each major.
- **Self-filing** — Scout/Provost/Quartermaster file their own findings as Jira tickets (`--file`, de-duped, assigned to you).
- **Find → fix → verify loop** — proven end-to-end (Provost found AUTO-12 → built → re-gated → merged → QA).
- **Builder unattended** — loads no repo settings so nested `ask` rules never block writes; reads CLAUDE.md/.claude/rules for conventions.

## Next — in priority order

1. **War Room cockpit** (the big one) — full-screen, served via `general serve`:
   - Live run streaming (SSE) — phases + officer tool-calls in real time.
   - Officer roster & activity — who's doing what, what each raised.
   - Metrics — passes/ticket trend, escalation rate, throughput, security-block rate, cost.
   - **Multi-project Jira selector** — switch which project/app you're driving (separate projects).
   - UX/UI excellence — best-practice, responsive, genuinely useful. (Roman iterates later.)
2. **Unit Memory** — living `memory/UNIT.md` (this roadmap is its first artifact): mission, per-app state, decisions, standing guidance, learned conventions, gotchas. Injected into every officer; a Scribe step appends after each council; git = audit trail.
3. **Scheduled patrols** — Scout/Provost/Quartermaster on a cadence with `--file`, so the unit continuously finds → files → fixes unprompted.
4. **Free-form officer discussion + ad-hoc meetings** — upgrade the council from one-statement-each to a real multi-round debate; officers can call a meeting to resolve a topic.
5. **Finding 1 — superadmin authz** — Provost files it; Roman + the General build the fix together (platform-admin probe + test invariant).

## Notes

- The General self-hosts on Roman's Mac (Claude Max login, no API key). Cowork edits the source; Roman runs it.
- Repo: github.com/roman-berlin/The-General (private), `main` + `dev`.
- Roman runs separate products: Automatixy CRM, SignalDesk, MQL5 EAs — the war room's project switcher serves this.
