# EU-710: Verify & Close — Stop Controls Epic Integration

## Context

EU-710 is the integration verification ticket for the stop-controls epic (original EU-686). All sibling pieces (EU-692, EU-693, EU-695, EU-705, EU-706, EU-689, EU-709) have landed on dev. This ticket must:

1. **Verify every sibling test passes** — they were written per-piece; some may be stale or failing
2. **Identify any remaining gap** between the 4 acceptance criteria and what's actually implemented
3. **Fix the identified gap** — specifically AC(2): run cards still show "Working · Phase" during an autopilot drain instead of "Stopping — finishing the current step"
4. **Produce the integration verification suite** that confirms the full chain end-to-end

---

## Gap Analysis

### AC(1): Each card stops its own run + visible "no run" message
**Status: VERIFIED ✓** — EU-692 (form hidden inputs), EU-693 (exact lookup), EU-695/EU-706 (visible message), EU-705 (per-card targeting), EU-709 (drain keeps hard-stop) all cover the sub-pieces.

### AC(2): Card shows "Stopping..." when stop fires ✗ GAP
**Current behavior:** Run cards always render `"Working · <b>{phase}</b>"` in `<div class="runsub">` (warroom.py:1366) regardless of whether autopilot is draining.
**Expected:** When a stop_event is fired (autopilot drain in progress), the card should show `"Stopping — finishing the current step · <b>{phase}</b>"` with an amber tint. Cleared automatically when release_run() resets active/stop_event.
**Root cause:** `_run_html()` has no awareness of the cockpit's `stopping` state. It doesn't receive a `stopping` parameter, and `render_board()` never computes it before calling `_run_html`.

**Fix needed:** Two files.

#### Change 1: orchestrator/warroom.py — `_run_html` signature + rendering
- Add `stopping: bool = False` parameter to `_run_html`
- In the live runhead section (~line 1366): when `stopping` is True, change the text from `"Working · {phase}"` to `"Stopping — finishing the current step · {phase}"`
- Add an amber dot/tone indicator inline

#### Change 2: orchestrator/warroom.py — `render_board` wiring
- Before calling `_run_html`, compute the stopping state for the current app:
  ```python
  from .cockpit_state import get_autopilot_status
  ap_st = get_autopilot_status(app) if app else {"stopping": False}
  stopping = bool(ap_st.get("stopping"))
  ```
- Pass `stopping=stopping` to ALL `_run_html` calls (both the single-run path at line 2007 and the multi-card loop at line 2031)

### AC(3): Toolbar "Stop" → real hard-stop
**Status: VERIFIED ✓** — EU-689 added the Stop button back. EU-709 added the Hard-stop alongside it. Both post to real endpoints (`/api/autopilot action=stop` and `/api/stop-run`).

### AC(4): RUN cluster doesn't collapse during drain
**Status: VERIFIED ✓** — EU-709 explicitly checks that the RUN cluster keeps its hard-stop control during a drain (AC1a/b/d). The "Stopping…" chip is rendered alongside actionable buttons.

---

## Implementation Plan

### Step 1: Run all sibling tests to establish baseline
```bash
for f in tests/eu692_stop_form_hidden_inputs_test.py \
         tests/eu693_stop_run_ticket_lookup_test.py \
         tests/eu695_stop_integration_test.py \
         tests/eu705_per_card_stop_test.py \
         tests/eu706_stop_no_event_message_test.py \
         tests/eu689_drain_stop_button_test.py \
         tests/eu709_drain_stop_run_card_test.py; do
    python "$f"; echo "--- $f exit=$?"
done
```

### Step 2: Implement the gap fix (warroom.py changes described above)

### Step 3: Re-run sibling tests to confirm no regressions

### Step 4: Write the EU-710 integration verification test
A new test file `tests/eu710_stop_epic_integration_test.py` that verifies the full chain:
1. Start a manual run → card says "Working · Build"
2. Fire the stop_event (simulating autopilot drain) → card shows "Stopping — finishing the current step · Build"
3. Click Stop on the card → POST to /api/stop-run → the run's event IS set → card reflects the stop
4. Two concurrent runs on different apps → card A click stops ONLY A
5. No live run → clicking Stop shows visible warning message in control bar
6. Drain path: autopilot enters drain state → control bar shows "Stopping…" + both Stop and Hard-stop buttons
7. Stale card form → stops nothing, shows warning message instead

### Step 5: Run linter/type-check on changed files

---

## Files Modified

| File | Changes |
|------|---------|
| `orchestrator/warroom.py` | 1. Add `stopping` param to `_run_html` (signature line 1250). 2. Conditional "Stopping" text in runhead (~1366). 3. Compute `stopping` in `render_board` (~1986). 4. Pass `stopping=` to both `_run_html` call sites (lines 2007, 2031). |
| `tests/eu710_stop_epic_integration_test.py` | NEW — full integration verification |

No other files modified. Lockfiles untouched. CLI workflow files untouched.

---

## Verification

After implementation:
1. All sibling tests pass (from Step 3)
2. New EU-710 integration test passes
3. `ruff check orchestrator/warroom.py` clean
4. No new lint/type errors

TEST: / (the warroom/home page — verify the Active Run panel shows the correct stop state transitions when autopilot enters a drain)
