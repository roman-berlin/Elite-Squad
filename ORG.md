# The Elite Unit — chain of command, mission flow & daily council

An elite autonomous software unit. The CTO commands the officers; the officers build,
verify, train, and manage the corps; and every morning they muster, study their record, and
brief the Commander. Officers are text files (`officers/*.md`) in Identity / Knowledge /
Skills form — sharpen the file, sharpen the officer.

> Legend: **solid = active today**, _dashed = planned_. Army terminology; every officer must
> earn its post.

## Chain of command

```mermaid
graph TD
    C["COMMANDER — you<br/>sets the mission · QAs DEV · owns MAIN"]
    G["CTO — orchestrator<br/>git custody · the loop · keep-DEV-green merge"]
    C --> G

    G --> ADJ["Engineering Manager · S-1 Personnel (HR)<br/>recruits / retires officers · propose-only"]
    G --> ENG["Dev Team Lead · Builder<br/>Sonnet · full tools · builds on a worktree"]
    G --> INS["Code Reviewer · Reviewer<br/>Opus · read-only · independent audit"]
    G --> DRL["Engineering Coach · Doctrine & Training<br/>improves the officers from the record"]
    G --> SCT["QA Engineer · S-2 Recon (QA)<br/>browser / e2e on DEV"]
    G --> PRV["Security Engineer · Security<br/>secrets · tenant-isolation · CVEs"]
    G --> QM["Release Manager · S-4 DevOps<br/>CI / deploy readiness"]

    ENG --> SQ["Dev Team Lead's squad — your repo's .claude/agents<br/>Frontend Engineer · Ordnance BE · Logistics DB<br/>Technical Writer Docs · AI · Judge Advocate Legal · Growth"]

    classDef command fill:#3b2f0b,stroke:#f59e0b,color:#fde68a;
    classDef active fill:#0b3d2e,stroke:#10b981,color:#d1fae5;
    classDef planned fill:#1f2937,stroke:#6b7280,color:#9ca3af,stroke-dasharray:5 5;
    class C,G command;
    class ADJ,ENG,INS,DRL,SCT,PRV,QM,SQ active;
```

**Build vs. check (no one signs off their own bridge):** officers that *build* live inside the
Dev Team Lead (the FE/BE/DB/AI squad). Officers that *independently verify* — Code
Reviewer, and the planned QA Engineer & Security Engineer — are separate and read-only. Defense in depth. The
**Engineering Manager** changes the *composition* of the corps (hire/retire); the **Engineering Coach** sharpens
the officers already in post.

**Ranks & chain of recruitment.** Commander → CTO → **major officers** (sit on the council)
→ **junior officers / sub-leads** → **engineers**. A major may recruit its own engineers (its
`.claude/agents` squad) and, when a focus area needs its own leadership, junior officers who are
handed engineers for sub-tasks — **every hire is gated by the Engineering Manager's (HR) approval**. Standing
up a new *major* officer needs the CTO's call and your sign-off. Only majors sit on the daily
council; everyone below reports up the chain.

## Mission flow (one ticket)

```mermaid
flowchart LR
    BL["Backlog · Jira<br/>In Progress, then To Do<br/>assignee = you · by Rank"] --> GEN["CTO<br/>takes one → In Progress"]
    GEN --> BUILD["Dev Team Lead builds<br/>isolated worktree"]
    BUILD --> GATE["Gate<br/>tests · lint · types"]
    GATE --> TE["Test Engineer<br/>coverage gate · happy-path + regression"]
    TE --> REV{"Code Reviewer<br/>pass?"}
    REV -- "fail · up to 4 passes" --> BUILD
    REV -- "pass" --> LAND["Land<br/>ff-push DEV · retire branch<br/>sync your DEV · ticket → QA · Telegram"]
    REV -- "exhausted / needs human" --> ESC["Escalate to you<br/>PR or Needs Human"]
    LAND --> QA["You QA on DEV"]
    QA --> MAIN["You merge DEV → MAIN"]

    classDef cmd fill:#3b2f0b,stroke:#f59e0b,color:#fde68a;
    class GEN,QA,MAIN cmd;
```

## Daily council (the unit studies every day)

At **10:00** the officers muster: each gives a short SITREP from its lens on the unit's recent
record, the Engineering Coach names the one drill worth running, the Engineering Manager covers personnel, and
the CTO chairs — producing a briefing, the day's orders, and the questions only you can
answer. The unit can also call an **ad-hoc muster** to work a specific improvement
(`general council --topic "…"`).

Only the **major officers** sit on the council — the squads do not attend; each major consults
and reports for its own engineers, keeping the muster sharp. The unit solves its own technical
and process problems and escalates to you **only** for genuinely Commander-level calls (product
direction, business strategy, irreversible decisions). Most mornings, that's *None*.

```mermaid
flowchart LR
    REC["Record<br/>(audit log)"] --> M["10:00 Muster<br/>officers' round-table"]
    NOTES["Your standing<br/>guidance"] --> M
    M --> BRF["CTO's<br/>briefing"]
    BRF --> TG["Telegram summary"]
    BRF --> DASH["Cockpit history"]
    BRF --> Q["FOR THE COMMANDER<br/>open questions → you"]
    Q -. "you reply in Telegram" .-> NOTES
```

Run it: `general council` (now) · scheduled 10:00 via `scripts/com.roman.general.council.plist`
· from your phone with `/council` · transcripts + history in the cockpit at `/council`.

## Roster

| Officer | Codename | Role | Status | Defined in |
|---|---|---|---|---|
| CTO | — | Orchestrator: git, the loop, the merge | **active** | `orchestrator/` |
| Engineering Manager | S-1 | Personnel (HR): recruit / retire officers | **active** | `officers/adjutant.md`, `adjutant.py` |
| Dev Team Lead | Builder | Implements the ticket on a worktree | **active** | `officers/engineer.md`, `builder.py` |
| Test Engineer | Tests | Coverage gate after build, before review: happy-path + regression tests, owns the PR coverage artifact | **active** | `officers/test-engineer.md`, `test_engineer.py` |
| Code Reviewer | Reviewer | Independent read-only spec + quality audit | **active** | `officers/inspector.md`, `reviewer.py` |
| Engineering Coach | Doctrine | Reviews the record, proposes officer upgrades | **active** | `officers/drillmaster.md`, `drillmaster.py` |
| QA Engineer | S-2 | Browser / e2e smoke on DEV (flows + a11y) | **active** | `officers/scout.md`, `scout.py` |
| Security Engineer | — | Security: secrets, tenant-isolation, authz, CVEs | **active** | `officers/provost.md`, `provost.py` |
| Release Manager | S-4 | CI / deploy readiness (build, migrations, env) | **active** | `officers/quartermaster.md`, `quartermaster.py` |

## Jira lifecycle (a ticket's path)

```
To Do ──(CTO picks it up)──▶ In Progress ──(green merge to DEV)──▶ QA ──(your sign-off)──▶ Done
                                      ▲                                   │
                                      └──────────(you send it back)───────┘
```

Queue order when draining: **In Progress first** (resume), then **To Do top-to-bottom** by
board Rank, only tickets **assigned to you**.

## Adding an officer (the 15-minute method)

1. Describe how you do the task and what "good" looks like.
2. Turn it into `Identity / Knowledge / Skills` with `officers/_TEMPLATE.md`.
3. Drop a build-specialist in your repo's `.claude/agents/<name>.md`; a verifier gets its own
   `officers/<name>.md` and a hook in the loop.
4. Manage it — review its work; the Engineering Coach refines the file, the Engineering Manager decides whether
   it stays in post.
