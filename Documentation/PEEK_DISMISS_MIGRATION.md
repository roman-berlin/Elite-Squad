# Peek + Dismiss Migration (EU-648 epic)

How the cockpit's one-shot action result (the "✓ QA finished…" / "✓ Closed EU-…" strip) moved
from a destructive pop-on-render banner to a persistent, dismissible strip on the live board.

## Before

`cockpit_views._result_banner` **popped** `state["last_result"]` on every render. The full-page
`GET /` consumed the message, so the 5s board poll / SSE stream — which render AFTER the page —
saw nothing, and a page refresh made the result vanish.

## After (landed pieces)

| Ticket | Change |
| ------ | ------ |
| EU-653 | `server.set_last_result(app, tone, text)` — dual write: plain `last_result` string (byte-compatible with old readers) + structured `last_result_record = {tone, text, timestamp}`. |
| EU-656 | Same pattern for the sticky control-bar note (`set_last_msg` / `last_msg_record`). |
| EU-660–667 | Failure/success write sites (standup, council, scribe, report intake, answer_api, QA verdict) migrated to the structured writers. |
| EU-669 | `cockpit_views._peek_last_result` (non-destructive read) + `POST /api/dismiss-result` (clears both keys, both scopes, idempotent). |
| EU-670 | `cockpit_views._result_strip` — tone-styled strip with a `[data-dismiss-result]` button; prepended to the board by `warroom.render_board`. |
| EU-675 | Delegated document-level click handler in the board's inline JS calls the dismiss endpoint and removes the strip from the DOM on success. |
| EU-676 | `index()` moved off `_result_banner`'s pop. |
| EU-677 | `_result_banner` deleted — no live caller needs pop semantics. |
| EU-672 | Regression guard: per-app result visible on `/api/board` before any `GET /`. |
| EU-673 | Integration close: `_view_state` overlays the **unit-wide** record onto every tab's board view (newest timestamp wins) — QA verdict / standup / council write with `app=None`, so without the overlay they never reached the live board. The duplicate bar strip left in `index()` by EU-676 was removed (the board carries it). Also converted the `/memory` confirmation banner from pop to peek — it was the last destructive reader, silently consuming the stored result on every page visit. |

## Semantics now

- **Persistent until dismissed**: a result renders on every board frame (SSE tick / 5s poll /
  full `GET /`) until `POST /api/dismiss-result` clears it. Nothing pops on read.
- **Two scopes**: per-project writers (`set_last_result(app_name, …)` — directive answers, run
  start/refusal) write the tab's own state; unit-level writers (`app=None` — QA verdict,
  standup, council, scribe) write the unit-wide `_state`. `_view_state` shows the newest of the
  two on every tab; dismiss clears both, so a dismissed result cannot resurrect.
- **Legacy fallback**: `_result_strip` still renders a plain `last_result` string when no
  record exists (pre-EU-653 writers), red-toned since the tone is unknown.
