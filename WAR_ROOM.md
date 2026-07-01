# War Room — field manual

The Elite Unit's command cockpit. One screen to see what the unit is doing, drive it,
and switch between your Jira projects.

## Open it

```
cd ~/Projects/General
./general serve
```

Then open **http://localhost:8787** in your browser. It binds to localhost only (no one
else can reach it). Runs happen in the background, so the page stays responsive.

The board **auto-refreshes every 5 seconds** — no need to reload.

---

## The header (top bar)

Left to right:

- **★ Elite Unit · War Room** — the title.
- **Project switcher** ← *this is where you choose your Jira project.* A dropdown listing
  every app in your `config.yaml` plus **All projects**. (This is the menu in your
  screenshot: automatixy / signaldesk / mql5-ea.)
- **Status pill** — `● autopilot live` when a run is in progress, `○ idle` otherwise.
- **updated HH:MM:SS** — when the board last refreshed.

### Choosing your Jira project

Pick a project from the switcher and the **whole board scopes to it**:

- KPIs, the active run, and the activity feed filter to that project.
- The control-bar actions (Run / drain / Report) target that project's repo + Jira.
- The URL updates to `/?app=<name>` — so you can bookmark a project view or share it.

Choose **All projects** for the unit-wide view across everything at once. (The roster is
always unit-wide — your officers serve every project.)

---

## KPI strip (the five numbers)

| Card | What it means |
|------|---------------|
| **Merged → DEV today** | Tickets the unit shipped to DEV (moved to QA) today. |
| **Merged total** | All-time merges to DEV (for the selected scope). |
| **Needs you** | Tickets parked for you: a PR was opened, an escalation, or an error. Amber when > 0. |
| **Parked** | Tickets the autopilot auto-skipped because they're stuck. Amber when > 0. |
| **Security blocks** | Times the Provost Marshal blocked a merge on a CRITICAL/HIGH finding. Red when > 0. |

---

## Active run + phase bar

The ticket the unit is working right now (or the most recent run if idle).

- **Phase bar:** `Build → Gate → Review → Security → Land`. A filled green dot = phase
  complete; an amber pulsing dot = the phase happening now.
- **● live** badge when a run is in progress; **last run** when idle.
- Below: current **pass** number, the latest **verdict**, and the **branch**.

---

## Roster (right column)

All **8 officers**, in chain of command, each with a status dot and last action:

- 🟢 **green (pulsing)** — on duty right now (Field Engineer + Inspector General during a build).
- 🔵 **blue** — acted in the last 10 minutes.
- ⚪ **grey** — idle.

The officers: **The General** (orchestrator) · **Adjutant** (S-1 / personnel) ·
**Field Engineer** (Builder) · **Inspector General** (Reviewer) · **Scout** (S-2 / QA) ·
**Provost Marshal** (security gate) · **Quartermaster** (S-4 / deploy) ·
**Drillmaster** (doctrine).

---

## Activity feed

The unit's pulse, newest first: merges, opened PRs, escalations, security blocks, and
daily-council outcomes — each with the ticket, project, and how long ago.

---

## Control bar (drive the unit)

- **App** — which project the action targets (defaults to your selected project).
- **Mode** — `task` (free-text work), `ticket key(s)` (e.g. `AUTO-13 AUTO-14`), or
  `drain my Jira` (work the whole queue).
- **Text** — the description, or the ticket keys, or blank for drain.
- **Effort** — Builder thinking depth for this run (`low / medium / high / max`), or default.
- **live** — unchecked = dry-run (full loop, no push/merge/Jira writes). Checked = live
  (build + merge to DEV).
- **▶ Run** — launch it.

Plus quick actions: **🐞 Report a problem** (file a QA bug with an optional screenshot),
**🫡 Daily standup**, **🎖️ Drill** (Drillmaster reviews the unit), **💬 Council**
(convene the officers), and **📋 Task log**.

### Task log (`/tasks`)

The full detailed table — every ticket with start/duration/passes/turns/verdict/PR, and an
expandable transcript per pass (the Builder's tool calls + summary, the Reviewer's verdict +
required changes). Click any row to expand.

---

## Add another Jira project to the switcher

Append an app to `config.yaml` — each product is its own repo + its own Jira project:

```yaml
apps:
  - name: signaldesk
    repo_path: ~/Projects/signaldesk
    base_branch: dev
    protected_branch: main
    backlog_backend: jira
    backlog:
      base_url: https://toibis.atlassian.net
      project: SIG
      assignee: "70121:051c9744-3c4d-4dfb-b2e5-d7a0e87c2443"
      queue_statuses: ["In Progress", "To Do"]
```

Restart `general serve` and the new project appears in the switcher.

---

## Good to know

- **Data source:** the board reads your real `audit.jsonl` (what the General actually did)
  and your real `config.yaml` apps. Nothing is invented.
- **Live streaming** of each officer's actions as they happen (per-event SSE) is the next
  war-room upgrade; today the board refreshes on a 5s poll.
