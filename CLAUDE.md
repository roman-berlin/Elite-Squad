# CLAUDE.md — working in this repo

Conventions for any agent (or human) working in **this** repository. Read it before you edit.

> **What this repo is.** This is the **orchestrator itself** — the unattended dev unit (a.k.a.
> "The General" / the Elite Unit), *not* a product. It is a **Python 3.12** codebase: a CLI
> (`./general …`), a Flask cockpit, and adapters for Jira and Telegram. It has **no** Supabase,
> **no** Vercel, **no** web frontend, **no** database/RLS, **no** Bun/Node toolchain, and it is
> **not** part of the Automatixy monorepo. Tickets against this repo (Jira project **EU**) are
> improvements to the unit's own code under `orchestrator/`, the officers, and `tests/`.

## Ground rules

- **Python only.** Run with the repo `.venv`. Dependencies live in `requirements.txt`; do not
  add a `package.json`/`bun.lock` or any Node/Bun tooling.
- **Branches:** land on `dev`, never on `main`. The Commander merges `dev → main` after QA.
- **Stay in the ticket's scope.** Don't bump deps, rewrite unrelated officers, or touch product
  repos. EU tickets are about this orchestrator's own behaviour.
- **Tests are the gate.** `python3 tests/run_all.py` must stay green. Each harness stubs the
  Agent SDK (no network, no real models) and asserts one slice of behaviour; add a
  `tests/<name>_test.py` for what you change.

## Skills / automation — what actually exists

There is **no `.claude/skills/` directory** in this repo, and no `/skill-name` commands. The
behaviours the unit relies on are **built into the orchestrator**, not external skill files.
When you need one of them, edit the code that owns it:

| Behaviour (what older notes called a "skill") | Where it actually lives |
| --- | --- |
| Record what shipped (`update-development-status`) | `orchestrator/loop.py` → `_record_changelog()` appends to `Documentation/Development_Status.md` on every successful live land (EU-41). |
| File out-of-scope findings instead of losing them | `orchestrator/filing.py` (`parse_tickets` / `file_findings`) raises de-duped, labelled backlog tickets from an officer's findings block (EU-42). |
| In-build gate / exit checklist (`post-dev-checklist`) | The Builder's gate — `orchestrator/gate.py` and `loop.run_gate` — enforced at a single point. |
| The "elite protocol" specialist squad (`run-elite-protocol`) | The Engineer's specialist corps; see `officers/` and `officers/README.md`. The Engineer runs Claude Code with the *target app's* `.claude/` loaded, so those specialists live in **that app's** repo, not here. |
| Tenant-isolation / zero-trust checks (`verify-tenant-isolation`) | A **product-repo** concern (enforced by the target app's `.claude/rules`), not a behaviour of this orchestrator repo. |

If you genuinely add a `.claude/skills/<name>/…` file in the future, reference it by its real
path here so the doc keeps matching the filesystem.

## Documentation index

Repo-root docs: `README.md` (how to run), `ARCHITECTURE.md` (design), `config.server.example.yaml` (the SERVER host's canonical config shape — diff the live VPS file against it to catch drift), `TECHNICAL.md` /
`TECHNICAL_DESIGN.md`, `DEPLOYMENT.md` / `VPS_DEPLOYMENT.md`, `ROADMAP.md`, `QA_MANUAL.md`,
`SQUAD_HQ.md` (the cockpit / field manual — renamed from the old War Room manual in the SQUAD rebrand), `ORG.md` (chain of command & roster). The live
officer **roster** isn't a committed doc: `orchestrator/roster.py` regenerates it at runtime into
a gitignored file (`./general roster`), so the checked-in sources are that code plus `ORG.md` — it
isn't listed above because it won't exist in a fresh checkout.

Everything under `Documentation/` (these are the only files there — keep this list honest):

- `Documentation/SYSTEM_OVERVIEW.md` — the unit's end-to-end system map.
- `Documentation/BUILD_DOCTRINE.md` — the fast-but-stable build doctrine: the 7 mechanisms
  distilled from the 2026-07 direct-build sprint, with the failure ledger and the standing
  guard tests (vacuous-assertion, stub-signature, retired-subsystems) that enforce them.
- `Documentation/REVIEW_BACKLOG.md` — deferred review findings / tech-debt queue.
- `Documentation/PRODUCTION_AUDIT_2026-07-21.md` — the 16-agent production-readiness audit
  (8 dimensions, adversarially verified): 5 P0s fixed same-day, the P1/P2 deferred matrix.
- `Documentation/BRAND.md` — the SQUAD brand guide (2026-07-21): the user-facing terminology map
  (HQ, [Squad] comments, FOR YOU), voice rules, and what deliberately stays internal.
- `Documentation/Development_Status.md` — the **feature changelog**: one line per successful
  live land, newest first, written automatically by the Technical Writer (EU-41). Created on the
  first land if absent.
- `Documentation/UNIT_REVIEW_2026-06-25.md` — a point-in-time unit review.
- `Documentation/EU-153_Implementation_Summary.md` — point-in-time implementation summary of
  EU-153 (the LLM ticket commenter), committed with that land.
- `Documentation/RESTRUCTURE_PROPOSAL_2026-07-05.md` — the 2026-07-05 forensic audit findings and
  the Phase-2 restructure proposal (3 LLM roles, deterministic gates, routing) awaiting the
  Commander's approval.
- `Documentation/OFFICER_NAMING.md` — canonical old→new officer-name map and the policy for what
  stays army-themed by design (EU-57).
- `Documentation/SYSTEM_AUDIT_2026-07-06.md` — the 2026-07-06 four-surface full-system audit (Local /
  Server / Telegram / Cockpit), risk-ranked findings, the alert table, the scale-up go/no-go, and the
  wave-sequenced combat-readiness implementation plan.
- `Documentation/DELTA_AUDIT_2026-07-12.md` — the 2026-07-12 delta-audit index of findings (EU-262):
  root-cause of the max-turns-exhaustion crash class (EU-248), delta metrics, economics, the 14 new
  EU tickets it filed (EU-248..EU-261), and the 7-ticket AUTO product-quality sample.
- `Documentation/TOTAL_AUDIT_2026-07-16.md` — the 2026-07-16 full-system re-sweep (EU-358): a
  12-auditor + 3-lens-verifier pass over all of `orchestrator/`, the launcher, and `tests/`; the 11
  defects fixed directly (EU-358, pinned by `tests/eu358_audit_fixes_test.py`), the deferred findings
  filed as EU-359..EU-372, the 8 recurring improvement clusters, and the durable EU-259 evidence.
- `Documentation/PATH_MIGRATION_AUDIT_2026-07-22.md` — EU-431: the 2026-07-21 `state/` migration
  orphaned every `Path(cfg.audit_path).with_name(...)` sibling. The council archive is now adopted
  on boot (`adopt_legacy_council`); this lists every affected sidecar, the `cron.log`-stays
  decision, and the recommended follow-ups (postmortems, signature_filed.json, …).
- `Documentation/QWEN_BACKEND_2026-07-24.md` — 2026-07-24: wiring Qwen as the hybrid secondary
  (builder) during the GLM weekly-cap outage. The working QwenCloud Token-Plan Anthropic endpoint,
  which model ids each plan actually serves, the Opus-reviewer quality baseline, and the flagship
  (`qwen3.8-max-preview`) builder experiment with its provisional KEEP decision.
- `Documentation/UX_AUDIT_2026-07-25.md` — the 2026-07-25 12-agent deep UX/UI audit of every cockpit
  surface (33 surfaces, 156 raw findings → 14 filed tickets): the two P0s (per-project action results
  never rendered; the live-log panel writing into a detached node), the 7-High/6-Medium/1-Low plan,
  and the EU-543 prerequisite note.
- `Documentation/EU-480_MULTI_RUN_BOARD_VERIFICATION.md` — EU-480 (2026-07-24): the verify & close
  of the multi-run Active-run panel (EU-458 epic): per-AC evidence mapping EU-485/486/487/488 to
  AC1–AC3 + the `max_concurrent_builders` cap, the full-gate integration pass (463/463 harnesses,
  8929 checks), the single-run byte-identity regression guard, the MANUAL TEST recipe, and the
  out-of-scope finding that the no-changes (EU-396) close path skips `_maybe_close_epic`.
- `Documentation/PEEK_DISMISS_MIGRATION.md` — EU-648 epic (2026-07-27): how the one-shot action
  result moved from the destructive pop-on-render `_result_banner` to the persistent dismissible
  strip on the live board — the landed-piece map (EU-653…EU-677), the persistent-until-dismissed
  semantics, the two writer scopes (per-project vs unit-wide) and EU-673's `_view_state` overlay
  that closes the AC1 gap (unit-level results like the QA verdict never reached the live board).

A regression guard (`tests/eu43_docs_reality_test.py`) greps this file for every repo-root
`*.md`, `Documentation/*.md`, and `.claude/skills/*` path it names and fails if any of them
don't exist, so the docs can't drift back out of sync with the repo.
