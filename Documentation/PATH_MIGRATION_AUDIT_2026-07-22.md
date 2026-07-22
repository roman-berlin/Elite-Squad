# Path-Migration Audit — 2026-07-22 (EU-431)

## What broke

On 2026-07-21 ~10:55 the live `config.yaml` `audit_path` was moved by hand over SSH from a
repo-root location into `state/` (i.e. `audit.jsonl` → `state/audit.jsonl`). Every runtime sidecar
is derived from it via `Path(cfg.audit_path).with_name(<name>)`, so that one edit silently relocated
**every** sibling one level deeper into `state/` — but the files themselves were not moved. Anything
that existed at `~/General/<name>` before 10:55 is now orphaned there: the code reads/writes
`~/General/state/<name>` (fresh/empty), the real data still sits at `~/General/<name>`.

`config.yaml` is gitignored, so the repo has no source of truth for the move, `self-update.sh` can
neither reproduce nor detect it, and it produced no audit event. (Change-management finding — see
the related note in the 2026-07-22 VPS responsibility audit.)

## The fix shipped here (AC1 + AC2)

`orchestrator/council.py::adopt_legacy_council(cfg, audit)`, called once on boot from
`main._main` right after `backend_pref.migrate` (same live-entrypoint-only discipline). When the
new `state/council/` has no `index.jsonl` yet **and** the legacy `council/` sibling holds one, it
**moves** (not copies) the legacy archive — `index.jsonl` + transcripts, byte-for-byte so append
ordering and thus `history()` are preserved — into the new location, records a
`council_archive_adopted` audit event, and is done. Idempotent + no-clobber: a new dir that already
holds an `index.jsonl` is never touched. Covered by `tests/eu431_council_archive_adoption_test.py`.

### The `cron.log` decision (AC1)

**`cron.log` stays at `~/General/council/cron.log`.** Evidence — the crontab and the self-updater
hardcode that path as a redirect *relative to `$HOME/General`*, independent of `audit_path`:

- `scripts/install-server-cron.sh` — every line: `… >> council/cron.log 2>&1`
- `scripts/self-update.sh:22` — `LOG="council/cron.log"`
- `.gitignore:70` — "the scheduled Scout/Provost/Quartermaster sweep writes cron.log here"

These are Commander-applied operational config (like CI workflows). Moving `cron.log` would orphan
the crontab redirect; keeping it requires **no** crontab change. So `adopt_legacy_council` skips
`cron.log`, and `~/General/council/` is left in place holding just `cron.log`.

## AC3 — every `Path(cfg.audit_path).with_name(...)` derivation

Classified by the impact of orphaning. "Orphaned?" = would the pre-migration `~/General/<name>`
hold real data the code now can't see. (`.parent`-based derivations — `qa-reports/`, `logs/`,
`shared/`, `config.yaml` lookup — resolve to the `state/` dir itself, the *new* location, so they
are **not** orphaned and are excluded.)

### A — Archive / accumulated history (irreplaceable)

| sidecar | site | orphaned? | impact | treatment |
|---|---|---|---|---|
| `council/` (dir: `index.jsonl` + transcripts) | `council.py:175` | **yes** (31 transcripts, 23-row index to 2026-06-20) | archive unreachable; `history()` would reset to 1 row at next ceremony | **FIXED** — adopted on boot |
| `postmortems/` (dir: `*.md`) | `forensics.py:145` | likely (written on every repeated ticket failure) | postmortem history unreachable; `postmortem_path` starts fresh | **recommend** same `adopt_legacy_*` pattern (follow-up) |

### B — Dedup / idempotency state (orphan → duplicate work or tickets)

| sidecar | site | orphaned? | impact | treatment |
|---|---|---|---|---|
| `signature_filed.json` | `forensics.py:378` | likely | filed-signature ledger lost → a recurring crash class re-files a **duplicate** Jira ticket | **recommend** adopt (follow-up) |
| `dismissed.json` | `dashboard.py:347` | possible | dismissed items could re-surface in the dashboard | list; low severity |
| `red_base_cache.json` | `gate.py:823,861` | possible | recomputed on next gate run (cache only) | no action — regenerated |

### C — Operational in-flight state (orphan → lost pending items)

| sidecar | site | orphaned? | impact | treatment |
|---|---|---|---|---|
| `pending_decisions.json` | `decisions.py:49` | possible | a Commander decision pending at 10:55 would vanish from `/needs` | **recommend** verify on box; adopt if non-empty |
| `blocked_tickets.json` | `autopilot.py:791`, `needs.py:167`, `warroom.py:196` | possible | blocked-run registry reset | verify on box |
| `autopilot_intent.json` | `autopilot.py:875` | possible | active drain intent lost (self-heals on next poll) | low severity |
| `proposals.json` | `approvals.py:60` | possible | queued ticket proposals lost from the approval inbox | verify on box |
| `telegram_offset.txt` | `decisions.py:53` | possible | Telegram getUpdates offset reset → may re-deliver old messages once | low severity (offset advances on first poll) |
| `self_restart_pending.json` | `autopilot.py:78` | unlikely | one-shot flag | no action |
| `squad_mode.json` | `squad_pref.py:31` | possible | squad-mode pref reset to default | low severity |
| `error_counts.json` | `autopilot.py:1440` | possible | crash-breaker counts reset | low severity |
| `turn_retries.json` | `builder.py:244` | possible | retry counters reset | low severity |
| `pending_specialist_approvals.json` | `clear_needs.py:40` | possible | specialist approvals lost | verify on box |

### D — Chat / transcript history (orphan → lost conversation context)

| sidecar | site | orphaned? | impact | treatment |
|---|---|---|---|---|
| `usage_ledger.jsonl` | `usage.py:73,80` | yes (appended every agent call) | historical token usage frozen at `~/General/`; KPI sparklines restart from migration | list; current operation unaffected |
| `commander_notes.md` | `council.py:240` | possible | standing-guidance notes lost from the prompt tail | low severity (rolling) |
| `commander_chat.md` | `council.py:268` | possible | cockpit 1:1 transcript lost | low severity |
| `group_chat.jsonl` | `council.py:1132` | possible | group-room thread lost | low severity |
| `last-standup.md` | `council.py:1244` | possible | cockpit shows no stand-up until the next daily (same day) | **no action — regenerated daily** |
| `ROSTER.md` | `roster.py:107` | possible | living roster overwritten by next `roster.refresh` | **no action — regenerated** |

### E — Regenerated report artefacts (orphan → stale one cycle, rewritten next run)

`scout-report.md` (`main.py:725`, `loop.py:3286`), `provost-report.md` (`main.py:735`),
`quartermaster-report.md` (`main.py:745`), `dashboard.html` (`main.py:793`), patrol reports
(`patrol.py:60`), needs-digest reports (`needs.py:105`), `recent_projects.json` (`projects.py:23`).

All overwritten on their next run. **No data of lasting value is lost** — at worst the cockpit
shows a stale report until the next sweep. **No action.**

## Recommended follow-ups (out of this ticket's scope)

The class fix is one cheap, reusable move per sidecar (the `adopt_legacy_council` template). In
priority order, worth their own tickets:

1. **`postmortems/`** — irreplaceable archive, identical shape to `council/` (dir of files). One
   call at the boot hook.
2. **`signature_filed.json`** — orphaning files **duplicate Jira tickets** (active board harm).
3. **`pending_decisions.json` / `proposals.json` / `blocked_tickets.json`** — verify on the box
   whether they held in-flight items at 10:55; adopt if non-empty.

The B/C/D items that are merely caches or counters (`red_base_cache.json`, `error_counts.json`,
`turn_retries.json`, `telegram_offset.txt`, …) need no adoption — they self-heal on first use.

## On-box verification (operator — not runnable from the worktree)

The worktree cannot see the live `~/General/`. To confirm exactly which siblings were left behind
and feed any follow-up adoptions:

```sh
cd ~/General
for f in council/index.jsonl postmortems signature_filed.json pending_decisions.json \
         proposals.json blocked_tickets.json dismissed.json commander_chat.md group_chat.jsonl \
         usage_ledger.jsonl ROSTER.md last-standup.md recent_projects.json; do
  [ -e "$f" ] && echo "LEGACY PRESENT: $f"
done
```

Anything printed is orphaned (the code now reads its `state/` sibling). After the next boot the
`council/` archive is adopted automatically; the rest await the follow-ups above.
