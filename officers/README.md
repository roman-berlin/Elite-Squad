# The Unit — officer definitions

Every officer is just a text file with three parts (per the "agents are text files"
principle): **Identity** (who they are), **Knowledge** (what they ground on), **Skills**
(the SOP — how they work). Sharpen the file → sharpen the officer. (The Engineering Coach that once proposed
edits to these files was retired in EU-327 — charter upkeep is Commander-driven now.)

Filenames are the *internal keys* (`scout.md` = the `scout` officer) and stay army-themed by
design — see `Documentation/OFFICER_NAMING.md`. The headings below are the canonical display
names (EU-57); `orchestrator/officers.py` maps one to the other.

## Chain of command

```
COMMANDER (you)
  └── CTO (orchestrator)
       ├── Engineering Manager (S-1 HR)  — recruits / retires officers  → adjutant.md
       ├── Dev Team Lead   (Builder)     — builds on an isolated worktree → engineer.md
       │     └── squad (in your repo's .claude/agents):
       │           Frontend Engineer · Ordnance (BE) · Logistics (DB) ·
       │           Technical Writer (Docs) · AI officer · Judge Advocate (Legal) · Growth
       ├── Code Reviewer   (Reviewer)    — independent read-only audit  → inspector.md
       ├── Performance Engineer (Perf)   — hot-path benchmark gate      → performance-engineer.md
       ├── QA Engineer     (S-2 Recon)   — browser / e2e on DEV          → scout.md
       ├── Security Engineer (Security)  — security gate                 → provost.md
       ├── Release Manager (S-4 DevOps)  — CI / deploy readiness         → quartermaster.md
       └── SRE (Sentinel)                — post-merge suite + forward-only rollback → sentinel.md
```

Every officer above is **in post today** — QA Engineer, Security Engineer and Release Manager
were tagged `(PLANNED)` here long after `scout.py` / `provost.py` / `quartermaster.py` shipped and
started running (EU-260); `tests/eu260_org_reality_test.py` now fails if a live officer is called
planned again.

## Build vs check (no duplication)

Officers that **build** live *inside* the Dev Team Lead (your elite squad — FE/BE/DB/AI/…).
Officers that **independently verify** (Code Reviewer, QA Engineer, Security Engineer) are
*separate* and read-only — the bridge-builders don't sign off their own bridge. Defense in depth.

## How these map to your elite protocol

Your `run-elite-protocol` squad **is** the Dev Team Lead's specialist corps and in-build gates
(Discover → Product → Architect → … → QA/DoD → Deploy). The Dev Team Lead runs Claude Code with
your repo's `.claude/` loaded, so it can already call them. The files here define the CTO's *own*
officers (the tree above) and a template so you can formalize any specialist the same way.

## Adding an officer (the 15-minute method)

1. Describe (or screen-record + narrate the *why*) how you do the task and what "good"
   looks like.
2. Turn it into `Identity / Knowledge / Skills` using `_TEMPLATE.md`.
3. Drop it in your repo's `.claude/agents/<name>.md` so the Dev Team Lead can dispatch to it.
4. Manage it: review its work and refine the file yourself when it slips.
