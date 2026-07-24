# EU-480 — Multi-run Active-run panel: verification & close (2026-07-24)

EU-480 is the auto-split **verify child** of the EU-458 epic (Scrum Master split). It
carries the original feature's acceptance criteria and runs LAST, after the sibling
pieces (EU-485, EU-486, EU-487, EU-488) land. This note is the integration check's
durable record — the per-AC evidence, the gate results, the manual TEST recipe, and
one out-of-scope finding.

## The feature (Commander 2026-07-23)

> "if it implements 2 tickets of the project, it needs to show both of them in the cockpit."

The drain runs `max_concurrent_builders: 2`, so two tickets of the same project build
at once (verified live 2026-07-23: EU-444 in worktree Elite-Unit-s1 and EU-443 in
Elite-Unit, both in flight). Before this feature the board's Active-run panel showed
only ONE card — `active_run()` picked `ts[0]` (the single newest task) and
`render_board` rendered exactly one `_run_html(...)` card — so the second concurrent
ticket was invisible to the operator.

## AC → implementation → test evidence (all on dev tip f2ef0dc)

**AC1 — detect ALL currently-live runs, not just `ts[0]`.**
`orchestrator/warroom.py:885` `live_runs()` generalises the existing `_run_in_flight`
freshness heuristic (warroom.py:860 — no terminal outcome AND audit activity within
~150 s) from "is any run live" to "which runs are live": one pass over the audit
collects the last-activity timestamp per `ticket_id`, then tasks with no terminal
outcome and fresh activity are kept, ordered newest-first. Ground truth is exactly
what the AC names: per-ticket `ticket_start` events with no terminal event.
Tests: `tests/eu486_multi_card_test.py` (14/14), `tests/render_board_multi_run_test.py`
`test_ac1_zero_live_runs` + `test_ac3_two_live_runs_each_card_correct` (EU-488).

**AC2 — one Active-run card per live run, capped at `max_concurrent_builders`.**
`render_board` (warroom.py:1884–1925) loops over `live_runs()` and emits one
`_render_run_card_data` + `_run_html` per run — each card carries its own ticket id,
stage bar, elapsed timer, and pass count. The cap is applied in `live_runs()`
(warroom.py:941: `cap = max(1, int(getattr(cfg, "max_concurrent_builders", 1) or 1))`),
so a burst of stale rows can't flood the panel. EU-487 additionally gives each card a
`data-log-ticket` attribute (warroom.py:1240) so all cards tail the SAME shared drain
log filtered to their OWN ticket.
Tests: `tests/eu485_run_card_data_test.py` (20/20 — the per-card data helper),
`tests/eu487_card_log_filter_test.py` (28/28 — per-card log filtering),
`tests/render_board_multi_run_test.py` `test_ac3_two_live_runs_each_card_correct`
(newest-first order, per-card ticket id, distinct elapsed strings, correctly scoped
`data-log-ticket`) and `test_ac4_cap_at_render_level` (3 live runs with
`max_concurrent_builders=2` → exactly 2 cards, the oldest ticket fully absent).

**AC3 — each card's stage label is ticket-scoped.**
`_live_phase(audit_path, ticket_id)` (warroom.py:1557) takes a `ticket_id` and skips
every audit event that doesn't name it (warroom.py:1588–1589) — under a concurrent
drain the unscoped "newest phase event anywhere" showed the OTHER ticket's stage
(the Commander saw "Gate" over EU-444 while EU-443 was the one at its gate). On the
multi-card path each card's `run_obj` is derived from that ticket's OWN task slice
(warroom.py:1905–1914), so the stage bar and pass-trend sparkline are per-ticket too.
Tests: `tests/render_board_multi_run_test.py` `test_ac3_two_live_runs_each_card_correct`.

**Regression guard — the single-run board is untouched.**
`len(lives) <= 1` takes the exact pre-EU-486 code path (warroom.py:1888–1898): one
`_render_run_card_data`, one `_run_html`, no `log_ticket`. Pinned structurally by
`tests/render_board_multi_run_test.py` `test_ac2_one_live_run_single_path_identity`
(a recording wrapper around `_run_html` asserts the exact call count, the absence of
`log_ticket`, and that the recorded HTML is embedded byte-for-byte in the board), and
by every pre-existing warroom suite staying green.

## Integration pass (re-run 2026-07-24 on this verify branch, dev tip f2ef0dc)

- Full gate `python3 tests/run_all.py`: **463/463 harnesses, 8929 checks, ALL GREEN**
  (exit 0, 243.9 s vs the 1800 s gate timeout).
- The four sibling harnesses individually: EU-485 **20/20**, EU-486 **14/14**,
  EU-487 **28/28**, EU-488 `render_board_multi_run_test.py` **18/18** — 80/80 checks.
- Sibling lands verified on dev: dc3226e (EU-485), f28e0ad (EU-486), 03cd224 (EU-487),
  7769693 (EU-488).

## Why this land is a documentation-only diff

EU-480 is an integration-only ticket; all code shipped with its siblings and the PM
decision (2026-07-24) accepted the integration pass with no code changes. The
pipeline's zero-diff close (EU-396: Reviewer audits the unchanged tree → QA) would
have left the parent epic open: `_maybe_close_epic` (loop.py:259) only fires from the
land path (loop.py:3212), never from the no-changes branch. This note is the
PM-sanctioned Documentation-note diff — it carries the verify child through a real
land so the EU-374 epic auto-close fires on merge, and it keeps the verification
summary as a durable artifact (the builder's shell has no Jira credentials — those
live in the cockpit daemon's environment — so a direct ticket comment was not
available from inside the build).

## MANUAL TEST

Start two concurrent builds in the same project (the drain's `max_concurrent_builders: 2`
does this by itself), then open that project's tab on the cockpit board
(`http://127.0.0.1:8787/board` → `/api/board?app=<project>`). Expected: the Active-run
panel shows **one card per live ticket** — each with its own ticket id, stage bar,
elapsed timer, and pass count — newest first, never more than
`max_concurrent_builders` cards, and each card's live-log tail filtered to its own
ticket. With a single run the panel is byte-identical to the pre-feature board.

## Out-of-scope finding (recommend a follow-up ticket)

The EU-396 no-changes auto-close branch (loop.py:2272–2308) transitions the ticket to
QA but never calls `_maybe_close_epic` — so a zero-diff VERIFY-child land (exactly
this ticket's shape) leaves the parent epic open forever, the same class of bug EU-374
fixed for the land path. Recommended fix: call `_maybe_close_epic(backlog, ticket,
audit)` in the `no_changes_autoclose` success branch too (the function is already
no-op-safe when the ticket isn't a verify child or siblings are still open). Not done
here — EU-480's scope is verification only, and the PM decision ruled out code changes.
