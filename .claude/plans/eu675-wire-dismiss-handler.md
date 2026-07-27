# EU-675: Wire dismiss-button click handler in the board's inline JS

## Context
Result-strip UI was added in EU-670: `cockpit_views._result_strip()` renders a colored bar with a
`[data-dismiss-result]` button, prepended to the board HTML by `warroom.render_board`. The ticket
assumed `POST /api/dismiss-result` already existed — it did not, so this ticket adds it too (the
server-side clear is a few lines: pop the result keys from state).

**Two files touched:** `orchestrator/server.py` + `orchestrator/warroom.py`, plus a new regression
test `tests/eu675_dismiss_result_handler_test.py` (live Flask test-client round-trip).

## Iteration history

- **Iteration 1** (rejected): endpoint + handler landed, but the EU-465 typing sweep was red —
  `def dismiss_result_api()` had no return annotation (every public def in `orchestrator/` must
  carry one; all sibling route handlers use `-> Response`).
- **Iteration 2 fixes:**
  1. `dismiss_result_api() -> Response` (sweep back to 24/24).
  2. **Real AC2 gap found while regression-testing:** iteration 1 popped only the unit-wide
     `_state`, but the board renders the strip from the TAB's per-project state
     (`/api/board` → `_view_state(appq)` → `get_state(appq)`, no global merge). Results written by
     `set_last_result(app_name, …)` — answers, directives, run starts, report intake — live in
     per-app state, so the iter-1 clear never removed them: the strip reappeared on the very next
     SSE frame. The handler now passes its tab's `APP` (same as the `/api/board?app=` poll) and the
     endpoint clears the resolved project's state (via `_board_project`, never trusting a raw name)
     PLUS the global `_state` (covers standup/council/QA writers that store with app=None).

## Changes

### 1. `orchestrator/server.py` — POST /api/dismiss-result endpoint (line ~1369)
```python
@app.post("/api/dismiss-result")
def dismiss_result_api() -> Response:
    st = get_state(_board_project(request.args.get("app")) or None)
    st.pop("last_result_record", None)
    st.pop("last_result", "")
    if st is not _state:
        _state.pop("last_result_record", None)
        _state.pop("last_result", "")
    return jsonify({"ok": True})
```

### 2. `orchestrator/warroom.py` — Delegated click handler (inline `<script>`, after `startStream()`)
```javascript
document.addEventListener("click", function(e){
  var btn=e.target.closest("[data-dismiss-result]");
  if(!btn)return;
  fetch("/api/dismiss-result?app="+encodeURIComponent(APP),{method:"POST"}).then(function(r){
    return r.json().then(function(j){if(j.ok){var s=btn.closest("div");if(s)s.remove();}});
  }).catch(function(){console.warn("[eu675] dismiss-result failed, strip left in place");});
});
```
- **Delegated on `document`** — `applyBoard` replaces `#board.innerHTML` wholesale on every SSE
  frame/poll, so a listener bound to the strip itself would die on the first tick; `closest()`
  matches strips rendered after page load.
- **Immediate `.remove()`** — the strip is a single `<div>` wrapping the button
  (`cockpit_views._result_strip`), so `btn.closest("div")` is exactly the strip; no SSE/poll wait.
- **Failure path logs, leaves strip in place** — no silent swallow.
- `applyBoard`/`tick` untouched; coexists with the existing details-closer click listener.

## Verification

| AC | Status | Mechanism |
|----|--------|-----------|
| Strip removed within same interaction | ✅ | `.remove()` in the fetch callback (eu675 test: static JS assertions) |
| Server-side clear confirmed | ✅ | live round-trip: seed via `set_last_result("automatixy", …)` → strip on `/api/board` → POST dismiss → next GET strip-free; global-write path cleared too |
| Delegated (post-load strips) | ✅ | `document` listener + `closest()` |
| No regression to applyBoard/tick | ✅ | eu670/eu549/warroom-triage/result-banner tests green |
| Iter-1 gate failure fixed | ✅ | `-> Response`; eu465 sweep 24/24, pinned by AC5 in the eu675 test |

Run: `python3 tests/eu675_dismiss_result_handler_test.py` (14/14) + `python3 tests/eu465_typing_sweep_test.py`.
