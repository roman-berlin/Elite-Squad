# The Unit — officer definitions

Every officer is just a text file with three parts (per the "agents are text files"
principle): **Identity** (who they are), **Knowledge** (what they ground on), **Skills**
(the SOP — how they work). Sharpen the file → sharpen the officer. The **Drillmaster**
proposes edits to these files from the unit's track record; you approve.

## Chain of command

```
COMMANDER (you)
  └── THE GENERAL (orchestrator)
       ├── Engineer    (Builder)   — builds on a field branch     → engineer.md
       ├── Inspector   (Reviewer)  — independent read-only audit   → inspector.md
       ├── Drillmaster (Trainer)   — improves the officers daily   → drillmaster.md
       ├── Scout       (QA)        — browser/e2e on DEV            (PLANNED)
       ├── Provost     (Security)  — security gate                 (PLANNED)
       └── Quartermaster (DevOps)  — CI / deploy readiness         (PLANNED)
            └── Engineer's squad (in your repo's .claude/agents):
                  Vanguard (FE) · Ordnance (BE) · Logistics (DB) ·
                  Scribe (Docs) · AI officer · Judge Advocate (Legal) · Growth
```

## Build vs check (no duplication)

Officers that **build** live *inside* the Engineer (your elite squad — FE/BE/DB/AI/…).
Officers that **independently verify** (Inspector, Scout, Provost) are *separate* and
read-only — the bridge-builders don't sign off their own bridge. Defense in depth.

## How these map to your elite protocol

Your `run-elite-protocol` squad **is** the Engineer's specialist corps and in-build gates
(Discover → Product → Architect → … → QA/DoD → Deploy). The Engineer runs Claude Code with
your repo's `.claude/` loaded, so it can already call them. The files here define the
General's *own* officers (Engineer, Inspector, Drillmaster) and a template so you can
formalize any specialist the same way.

## Adding an officer (the 15-minute method)

1. Describe (or screen-record + narrate the *why*) how you do the task and what "good"
   looks like.
2. Turn it into `Identity / Knowledge / Skills` using `_TEMPLATE.md`.
3. Drop it in your repo's `.claude/agents/<name>.md` so the Engineer can dispatch to it.
4. Manage it: review its work, let the Drillmaster refine the file when it slips.
