# Qwen as a model backend — setup, findings, and the live experiment (2026-07-24)

Recorded during the GLM weekly-cap outage (GLM exhausted until 2026-07-27). Qwen was wired as the
hybrid **secondary (builder)** while Opus stayed the **main (planner + reviewer)**. This is the
durable note the 30-min watch cron and the model-strategy decision refer to.

## The working setup (what actually connects)

The unit's officers run on Claude Code (the Anthropic Agent SDK) and speak ONLY the Anthropic
Messages API (`/v1/messages`) — a backend is applied by pointing a subprocess at
`ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`, exactly like GLM via z.ai. So a Qwen backend MUST be
reached over an **Anthropic-compatible** endpoint; a pure OpenAI `/chat/completions` endpoint only
powers the cockpit's "Test connection" button, never a real build.

- **Endpoint (Anthropic route):** `https://token-plan.ap-southeast-1.maas.aliyuncs.com/apps/anthropic`
  — this is the **QwenCloud Token-Plan** workspace. It is NOT the same as the direct Alibaba
  `dashscope-intl.aliyuncs.com` endpoint (that one served only a small FREE trial quota that
  exhausted in ~3 hours; the paid Token-Plan key returns 403 there). Same "Qwen" brand, two doors.
- **Auth:** the Token-Plan API key as a bearer token (`.env: QWEN_AUTH_TOKEN`), stored in the
  registry secret store (`state/secrets.json`), referenced as `secret://<record-id>`.
- **Provider field:** `anthropic` (the endpoint speaks `/v1/messages`).

### Models the plans actually serve (probed live)

| Model id | Lite ($6) | Role |
| --- | --- | --- |
| `qwen3.8-max-preview` | ✅ 200 | flagship reasoning — the strongest builder available |
| `qwen3.7-plus` | ✅ 200 | strong general/coder — the first builder tried |
| `qwen3.6-flash` | ✅ 200 | small/fast companion |
| `qwen3.6-plus` | ❌ 403 | higher tier |
| `glm-5`, `kimi-k2.5` | ❌ 403 | Pro-tier bundle (still gated after a Standard upgrade) |
| `qwen3-coder-plus` | ❌ 400 | not served on this plan (the initial wrong guess — caused every build to 400 "Model not exist") |

`GET /v1/models` is **not supported** on this endpoint ("Not support") — enumerate by probing ids.

## How it's wired (live state, not code)

- Registry record (`state/model_registry.json`): provider `anthropic`, the Token-Plan base_url, the
  chosen `model_id`, `small_fast_model_id = qwen3.6-flash`.
- `backend_pref` secondary → that record's id, in **`state/model_backend.json`** (the file the LIVE
  process reads — NOT the `cfg=None` default path, a mistake that cost an hour: a write there does
  not reach the daemon). Restart or per-build resolution picks it up.
- Result confirmed live: builder → the Qwen model; planner/reviewer/pm/council → Opus. Only the
  `builder` tag routes to the secondary (`backends._HYBRID_BUILD_TAGS = {"builder"}`).

## Quality read (Opus reviewer holding the line)

Baseline on **`qwen3.7-plus`** (first 3 completions):

| Ticket | Passes | Reviewer | Outcome |
| --- | --- | --- | --- |
| EU-485 | 1 | PASS | ✅ merged (clean first pass) |
| EU-437 | 2 | FAIL → PASS | ✅ merged (one bounce, fixed) |
| EU-460 | 1 | — | ⚠️ escalated (tests left RED; hard meta-ticket) |

**2/3 merged.** Key point: the Opus **reviewer** FAILed one pass and escalated another rather than
merging weak code — so a cheaper builder cannot quietly lower the bar; the quality gate is Opus-side.

## The live experiment (Commander, 2026-07-24 ~18:15)

Switched the builder to the flagship **`qwen3.8-max-preview`** on the hypothesis that a stronger
builder gets tickets right first-pass, so a higher per-build cost is repaid by **fewer rebuilds**
(no 2–3× reviewer bounces) — net cheaper and faster. A one-hour decision (cron `3c3dd330`, ~19:17)
compares max-preview's passes-per-merge / escalation-rate / quota-burn against the 3.7-plus baseline
above and keeps whichever wins.

## Operational cautions

- **Quota is the new stall risk.** A dead builder quota looks exactly like the GLM cap — the drain
  holds. Watch the 5h / 7-day Token-Plan quota; max-preview burns faster per build.
- **No auto fail-over yet.** A hard-capped secondary does NOT yet build on the main model — that is
  EU-475. Until it lands, a secondary outage needs a manual swap (registry secondary → none, or to
  a working model).
- **RAM caps concurrency.** This Mac (26 GB, ~8.6 GB free vs a 6 GB floor) has no headroom for a 3rd
  builder — the plan upgrade's value here is the flagship model + quota, not more parallel builders.

## Decision — 2026-07-24 19:17 (provisional: KEEP max-preview)

One-hour experiment closed. Sample was thin — the drain produced only ~2 tickets in the hour, and
just ONE is a pure `qwen3.8-max-preview` build:

| Ticket | Builder | Passes | Reviewer | Outcome |
| --- | --- | --- | --- | --- |
| EU-488 | qwen3.8-max-preview | 1 | PASS | clean, no bounce |
| EU-486 | qwen3.7-plus (pre-switch) | — | PASS | merged (transitional) |

**Decision: KEEP `qwen3.8-max-preview` as the builder — but PROVISIONALLY.** The single true data
point is favourable (1 pass, PASS, no escalation vs the 3.7-plus baseline of 1.5 passes/merge and
1/3 escalated), it matches the sound hypothesis (a stronger builder that lands first-pass beats a
cheaper one bounced 2–3×), and the quota stayed healthy (200). But ONE ticket is not a proof — this
is "no evidence against, favourable early signal", not a validated win.

**NOT changing the code default.** The strategy is already implemented by the live registry setting
(secondary → the top-tier Qwen record); hard-coding a "prefer-strongest-builder" rule into the
model-selection algorithm on a single data point would be over-fitting. The 30-min watch cron keeps
accumulating passes-per-merge / escalation-rate; if the favourable signal holds over ~10 tickets,
THEN promote it to a documented/coded default. If escalations climb or the burn spikes, revert to
`qwen3.7-plus` (one registry update).
