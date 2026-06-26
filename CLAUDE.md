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

Repo-root docs: `README.md` (how to run), `ARCHITECTURE.md` (design), `TECHNICAL.md` /
`TECHNICAL_DESIGN.md`, `DEPLOYMENT.md` / `VPS_DEPLOYMENT.md`, `ROADMAP.md`, `QA_MANUAL.md`,
`WAR_ROOM.md` (the cockpit / field manual), `ORG.md` (chain of command & roster). The live
officer **roster** isn't a committed doc: `orchestrator/roster.py` regenerates it at runtime into
a gitignored file (`./general roster`), so the checked-in sources are that code plus `ORG.md` — it
isn't listed above because it won't exist in a fresh checkout.

Everything under `Documentation/` (these are the only files there — keep this list honest):

- `Documentation/SYSTEM_OVERVIEW.md` — the unit's end-to-end system map.
- `Documentation/REVIEW_BACKLOG.md` — deferred review findings / tech-debt queue.
- `Documentation/Development_Status.md` — the **feature changelog**: one line per successful
  live land, newest first, written automatically by the Technical Writer (EU-41). Created on the
  first land if absent.
- `Documentation/UNIT_REVIEW_2026-06-25.md` — a point-in-time unit review.
- `Documentation/OFFICER_NAMING.md` — canonical old→new officer-name map and the policy for what
  stays army-themed by design (EU-57).

A regression guard (`tests/eu43_docs_reality_test.py`) greps this file for every repo-root
`*.md`, `Documentation/*.md`, and `.claude/skills/*` path it names and fails if any of them
don't exist, so the docs can't drift back out of sync with the repo.
