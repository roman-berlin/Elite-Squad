# SQUAD — brand guide

*Adopted 2026-07-21 (Commander decision). This file is the canonical brand map — the same way
`OFFICER_NAMING.md` governs internal officer names, this governs everything a USER sees. A guard
test (`tests/brand_test.py`) pins the user-facing surfaces to it.*

## The brand

**SQUAD** — *your autonomous dev squad.*

A squad of AI engineers that takes tickets from your board to merged, reviewed code — and only
interrupts you for the calls that are genuinely yours. The register is modern dev-tool
(Linear/Raycast): warm, competent, direct. Confident, never boastful; honest about failures.

- **Wordmark**: `⬢ SQUAD` — the hexagon badge (⬢, U+2B22) in the brand accent + the name in
  uppercase. The hexagon = a tight unit of cells working as one.
- **Accent**: coral — `--brand: #ff7a59` on dark, `#e8590c` on light. Used for the wordmark and
  brand moments only; functional colors (ok/bad/dim) are unchanged.
- **The user is "you"** — direct address. Internally the code may say Commander; the UI never does.

## Terminology map (old → new)

| Old (user-facing) | New | Where |
| --- | --- | --- |
| War Room | **HQ** ("Squad HQ") | cockpit title, header, startup line |
| Elite Unit / the unit / The General | **SQUAD** / "the squad" | wordmark, notify voice, docs |
| Commander | **you** (direct address) | all user-facing text |
| `[General]` Jira comment prefix | **`[Squad]`** | writes; reads accept both (history) |
| FOR THE COMMANDER (daily) | **FOR YOU** | daily brief section |
| Unit Memory | **Squad memory** | memory page + buttons |
| run / drain (visible copy) | **mission** / "working the queue" | soft-touch, where it reads naturally |
| Full squad / Elite squad | *(unchanged — already on-brand)* | squad selector |
| Needs you | *(unchanged — already on-brand)* | KPI, page |
| officer(s) | **engineer(s)** | all user-facing surfaces incl. cockpit, generated ROSTER.md, CLI --help; internal code/docs keep "officer" per OFFICER_NAMING.md |

**What deliberately stays**: internal identifiers, module names (`warroom.py`), audit event
names, officer names (per `OFFICER_NAMING.md`), the `./general` CLI, and code comments —
renaming those is churn without user value. The brand is the surface.

## Voice rules

1. Lead with the outcome: "Landed AUTO-59 — ready for your check", not "The process completed".
2. "The squad" does things; "you" decide things. Never "the Commander must…".
3. Honest failures, no drama: "Ran out of budget twice — this one needs re-scoping."
4. One emoji per message, functional not decorative. ⬢ is reserved for brand moments.
5. Every ask ends with the action: what to click, what to reply.
