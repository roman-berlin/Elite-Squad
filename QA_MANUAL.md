# Elite Unit — Manual QA Test Guide

A hands-on checklist to test the whole system yourself. Work top-to-bottom; later tests assume
the earlier ones passed. Tick **PASS/FAIL** and note anything odd.

**Conventions**
- "Cockpit" = the War Room at `http://localhost:8787`.
- "Live feed" = the panel that streams the unit's steps.
- Commands run from `~/Projects/General` (`./general <cmd>`).
- Keep a **dry-run** mindset: only the tests explicitly marked **LIVE** push/merge/file. Everything
  else is safe.
- For safety, do most testing with **Autopilot OFF** so nothing runs unprompted while you poke.

---

## 0. Preconditions
- [ ] Latest code pulled and committed (`git status` clean on `DEV`).
- [ ] `.env` present; Telegram configured if you want to test notifications.
- [ ] No stray cockpit running: `lsof -ti tcp:8787` returns nothing (or you'll relaunch anyway).

---

## 1. Launch & health gate
**1.1 Launch** — double-click **War Room.command** (or `./general serve`).
- Expect: terminal prints `★ War Room: http://localhost:8787`, the browser opens the cockpit, and the Mac stays awake (caffeinate) while it's open.

**1.2 Healthy state** — look at the header.
- Expect: green **"● System healthy"** pill; **no** red banner; the **live** dot (next to the time) is green.

**1.3 Health detail** — click the health pill.
- Expect: a dropdown lists the checks (Claude login, Agent SDK, git, per-app repo/branch/Jira). A **Re-check** button reloads.

**1.4 Unhealthy gating (optional, destructive-ish)** — temporarily rename `.env` or log out of Claude, then reload.
- Expect: **red banner** listing the exact failing checks; **Run / Autopilot / Choose-a-ticket are disabled**. Restore `.env`, Re-check → green again.

---

## 2. Cockpit basics
**2.1 KPIs** — top strip.
- Expect: Merged → DEV today / Needs you / Parked / Security blocks / Tokens, each a number.

**2.2 Roster** — right column, 10 officers.
- Expect: CTO, Engineering Manager, Product Manager, Scrum Master, Dev Team Lead, Code Reviewer, QA Engineer, Security Engineer, Release Manager, SRE, each with a status dot + "last seen".

**2.3 Activity feed** — recent outcomes + councils.

**2.4 Project switcher** — the header dropdown.
- Expect: switching scopes the KPIs/feed/active-run to that project (URL gains `?app=…`).

---

## 3. Live streaming (SSE)
**3.1 Connected** — with the cockpit open and idle.
- Expect: the header **live** dot is green (stream connected). The footer time refreshes within ~2s.

**3.2 Real-time push** — run any dry-run (see 4.2) and watch the **Live feed**.
- Expect: officer steps appear within ~0.5s of happening (not every 5s), including per-tool lines like `· builder: Edit …`. The phase bar advances Build→Gate→Review→Land in real time.

**3.3 Fallback** — stop the server, watch the dot.
- Expect: dot goes amber; when the server returns, it reconnects to green on its own (the page also keeps trying a 5s poll meanwhile).

---

## 4. Choosing & running a ticket
**4.1 Ticket picker** — click **Choose a ticket**.
- Expect: only **your** assigned tickets (In Progress + To Do, board-ranked) with checkboxes; summaries are escaped (no broken HTML).

**4.2 Dry-run** — tick one ticket, leave **live OFF**, **Develop selected**.
- Expect: redirect to the cockpit; Active-run shows the ticket **● running** with a **dry-run · no changes** chip; the feed streams; at the end it reports "would merge / would open PR" and **nothing is pushed or changed in Jira**.

**4.3 LIVE confirm guard** — tick a ticket, check **live**, submit.
- Expect: a **confirm dialog** ("Run LIVE — build and MERGE to DEV…"). Cancel = nothing happens.

**4.4 LIVE run (when you're ready)** — confirm the dialog. **[LIVE]**
- Expect: **live → DEV** chip; on success it merges to DEV (your QA column in Jira), MAIN untouched; Telegram pings if configured.

**4.5 Free task** — **+ Free task** → type a bug, pick effort `auto`, live OFF, Run.
- Expect: same dry-run behavior for an ad-hoc (ephemeral) task; no Jira writes.

---

## 5. Run controls
**5.1 Elapsed + cost** — during any run, watch the Active-run meta.
- Expect: **elapsed** ticks up; **cost** appears once the builder reports.

**5.2 Stop a run** — during a manual run, click **■ Stop**.
- Expect: confirm dialog; on confirm the run halts at the next safe checkpoint (before the next build pass / before merge) — **no half-merge**; the feed prints "stopped by Commander".

**5.3 Single-run lock** — try launching a second run while one is active.
- Expect: it's ignored (one run at a time).

---

## 6. Autopilot
**6.1 Start confirm** — header **Autopilot → Start**.
- Expect: confirm dialog ("Start Autopilot? …LIVE…"). Cancel = stays off.

**6.2 Running** — confirm. **[LIVE]**
- Expect: switch shows **Autopilot ON · <project>**, dot pulses; it resumes In Progress, else takes top To Do; live merges to DEV.

**6.3 Stop** — click **Stop**.
- Expect: switches OFF within a cycle; no new tickets picked up.

**6.4 Health gate** — with Autopilot, break health (1.4) and try Start.
- Expect: blocked with a "fix the health problems" message.

---

## 7. Recon delegation
*(armed: `delegation_enabled: true` in config.yaml — restart after changing)*

The flag arms the **read-only recon squads** the patrol officers (QA Engineer / Security
Engineer / Release Manager) field to parallelize their sweeps. (The build-time squad split —
soldiers building slices — was deleted in the Phase-2 §2 collapse; the Builder always builds
solo now.)

**7.1 Recon with the flag on** — run QA (toolbar → **Run QA**, or `./general patrol <app>`).
- Expect: the patrol report notes squads being fielded; findings still file as tickets.

**7.2 Flag off** — same patrol with `delegation_enabled: false`.
- Expect: officers sweep solo; same report shape, no squad lines.

---

## 8. Chat with the General
**8.1 Open** — toolbar **💬 Chat**.
- Expect: the **General | Group room** tabs; the General thread; a composer.

**8.2 Pending decision** — if a run escalated/halted, a card appears ("the unit needs your call").
- Expect: an inline reply box per decision. Answering it resumes the parked ticket (same as a Telegram reply).

**8.3 Free message** — type a question to the General.
- Expect: the General replies in the thread within a few seconds; your message + its answer persist (also logged as standing guidance).

**8.4 Unread badge** — when a decision is pending, the toolbar **💬 Chat** shows a red count.

---

## 9. Group room
**9.1 Open** — Chat → **Group room** tab (or Unit → Group room).
**9.2 Ask the unit** — type a question / brainstorm prompt, send.
- Expect: a "the unit is weighing in…" note, then the **relevant officers reply** in character (others add a short comment, off-lane ones stay silent). The General is **not** here (that's your 1:1 chat). Thread persists and auto-scrolls.

---

## 10. Daily standup
**10.1 Snapshot** — **Views → Daily standup**.
- Expect: instant snapshot (shipped today / needs-you / awaiting decision).

**10.2 Officer stand-up** — click **🫡 Hold stand-up**.
- Expect: "officers reporting…", then each officer's **Yesterday / Today / Blockers**, and a **Hand-offs & blockers** section pulling any "need <Officer>" lines.

---

## 11. Council, meeting, ship-review
**11.1 Council** — Unit → **Hold council** (or `./general council`).
- Expect: officers muster, the General posts SITREP / ORDERS / FOR THE COMMANDER; saved under **Councils & meetings**; the Scribe folds lessons into Unit Memory; Telegram briefing if configured.

**11.2 Meeting** — Unit → **Convene a meeting**, give a topic.
- Expect: the relevant officers debate, the General writes a decision record (saved under councils).

**11.3 Ship-review** — runs as phase 2 of the toolbar's **Run QA** (after the patrol files findings); the verdict posts to /council + Telegram.
- Expect: Quartermaster certifies, officers debate, the General issues **GO / NO-GO** — and explicitly leaves the MAIN promotion to **you**.

---

## 12. Scheduled patrols
**12.1 Propose-only** — `./general patrol automatixy --no-file`.
- Expect: Scout + Provost + Quartermaster sweep DEV; each reports `proposed N` / `clean`; **no Jira tickets created**.

**12.2 From the cockpit** — Unit → **🛰️ Run patrol**.
- Expect: a confirm (it FILES tickets); on confirm the three officers run; findings become Jira tickets (de-duped, assigned to you). **[LIVE — files tickets]**

**12.3 Resilience** — (sanity) if one officer errors, the others still run and report.

**12.4 Schedule** — `crontab -l | grep patrol` → the weekly `0 9 * * 1 … ./general patrol` line is present (installed by `scripts/install-server-cron.sh`). Fires weekly Mon 09:00.

---

## 13. Proactive autonomy
*(only fires under Autopilot; throttled by cooldown)*

**13.1 (retired)** — the auto-convened `MEETING:` pipeline was deleted with the events.py
autonomy layer (Phase-2 §2). Meetings are convened on demand (cockpit / CLI / Telegram).

**13.2 Meeting auto-spawn** *(`meeting_autospawn: true`)* — a meeting that proposes tickets files them (de-duped); drills/hires stay proposal-only.

**13.3 After-merge Scout** *(`scout_after_merge: true`)* — right after a LIVE merge, Scout smoke-tests DEV and files any regression. **[LIVE]**

---

## 14. Memory & scribe
**14.1 Unit memory** — Views → **Unit memory** (or `./general memory`).
- Expect: Mission, Standing Orders, per-app notes, Scribe-maintained Lessons (newest first). Your hand-edits between markers are preserved.

**14.2 (retired)** — `./general drill` and the Drillmaster were deleted (EU-327); charter
upkeep is Commander-driven now.

**14.3 Scribe** — Unit → **Update memory** (or `./general scribe`).
- Expect: recent councils/runs folded into Unit Memory between the protected markers.

---

## 15. Telegram two-way (if configured)
**15.1 Escalation** — when a run escalates/halts, you get a `❓ … needs your call` message.
**15.2 Reply** — reply `AUTO-XX: <your decision>`.
- Expect: the parked ticket resumes with your decision; the dashboard chat stays in sync.

---

## 16. Smoke regression after any change
- [ ] Cockpit loads, health green, live dot green.
- [ ] A dry-run streams and finishes cleanly.
- [ ] `./general patrol automatixy --no-file` runs all three officers.
- [ ] No errors in the terminal beyond expected warnings.

---

### Sign-off
| Area | Pass | Notes |
|---|---|---|
| Launch & health | ☐ | |
| Cockpit + SSE | ☐ | |
| Run (dry + live) + controls | ☐ | |
| Autopilot | ☐ | |
| Delegation | ☐ | |
| Chat + group + standup | ☐ | |
| Council/meeting/ship-review | ☐ | |
| Patrol + autonomy | ☐ | |
| Memory/scribe | ☐ | |
| Telegram | ☐ | |
