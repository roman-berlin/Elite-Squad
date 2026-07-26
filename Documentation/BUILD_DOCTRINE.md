# Build Doctrine — fast implementation without bugs

*Distilled 2026-07-19 from the 3-day, ~70-ticket direct-build sprint (EU-301 … EU-387) and its
post-mortem. Empirical basis: every bug that escaped during the sprint traces to exactly one of
these mechanisms being skipped; wave 2 — with all seven applied — landed 399/399 checks first-try
with zero seam leaks. These are mechanisms, not vibes: three of them are enforced by permanent
guard tests, the rest are steps in the build workflow itself.*

## The seven mechanisms

1. **TRIAGE FIRST.** Before building anything, cite the `file:line` where the claimed behaviour
   lives and one measured number (from `state/`, the audit log, or the live system) proving the
   ticket's premise. A ticket whose premise is already satisfied is **folded**, not built
   (EU-388 was folded this way; EU-379 was sized from 292 landed tickets' real timings).

2. **PARTITION MECHANICALLY.** Parallel work is split into **file-disjoint clusters** — no
   judgment-call overlaps. If two clusters need the same file, they are one cluster. Zero merge
   conflicts across both sprint waves came from this rule, not from luck.

3. **FAIL-FIRST or MUTATE — every assertion earns trust by going RED once.** Write the test
   first and watch it fail for the right reason; when the code landed first, mutation-check
   instead (revert the change or tamper the input, confirm RED, restore). This caught eu378's
   compare-after-refresh tautology and eu68's `.find() == -1` vacuous pass — both invisible to a
   green run. Enforced in the Builder's own prompt (builder.py rule 5) and, for the recurring
   static classes, by `tests/vacuous_assertion_guard_test.py`.

4. **FRESH BASE.** Record each worktree's base sha at creation. Before integrating a cluster,
   check `git diff <base>..dev` against the cluster's files — if dev moved under it, rebase and
   re-run before trusting any green (the eu362 cluster was green against a base that predated
   EU-187's terminal allowlist, testing behaviour dev had already rejected).

5. **STUB THE FULL SEAM.** A test double bound to a production seam must accept the seam's full
   signature (`**kwargs` is the cheap way). EU-258 added two kwargs to
   `run_agent_with_fallback` and 6 narrower doubles broke; the manual sweep fixed only 4.
   Enforced by `tests/stub_signature_test.py`.

6. **LOUD HARNESSES.** A test file must exit non-zero on any failed check — "printed FAIL,
   exited 0" shipped three times (EU-244, EU-302, run_all's own verdict) before it was fixed.
   Verify by forcing one check red and running the file standalone. Enforced in the Builder's
   prompt; run_all's `_verdict` is the backstop.

7. **ONE UNION GATE.** Per-cluster greens are necessary, never sufficient: after integration,
   the FULL `python3 tests/run_all.py` runs on the union tree before anything lands on dev.
   Wave 1's per-cluster greens hid 5 seam failures that only the union run exposed; wave 2,
   with mechanisms 3/5/6 already in force, leaked zero.

## The failure ledger (why each rule exists)

| Escaped bug | Skipped mechanism |
|---|---|
| eu68 `.find()` vacuous compare (passed on `-1`) | 3 — never shown red |
| eu378 compare-after-refresh tautology | 3 — never shown red |
| EU-258 stub drift (6 doubles too narrow, sweep found 4) | 5 — partial seam |
| eu362 green against a stale base | 4 — no base-sha check |
| run_default printed "1 FAIL", exited 0 | 6 — quiet harness |
| Wave-1's 5 cross-cluster seam failures | 7 — trusted per-cluster greens |

## Standing guards

### Model-cap fallback doctrine

`run_agent_with_fallback` provides a **one-shot** fallback path that runs on each invocation, per-call —
it never pins any weekly state. The design evolved from two independent tickets working in opposite
directions:

- **EU-118** (main → secondary): a Sonnet call that hits a plan-limit classified as `cap` retries
  once on Opus. If Opus succeeds, its result is returned and nothing is persisted — the next call
  starts cheap on Sonnet again. (The EU-108 weekly-Opus pin was deliberately deleted for this
  exact reason: a Sonnet cap must never consume an entire week of the expensive tier.)
- **EU-475 / EU-511 / EU-512** (secondary → main): a GLM/secondary call that hits a hard cap
  retries once on the Anthropic native/main model using the same one-shot, per-call discipline.

Both directions follow identical rules about **plan-limit classification**:

| Classification | Behaviour |
|---|---|
| `"cap"` (hard quota exhausted) | Retry exactly once on the partner model. If partner succeeds → return partner result. If partner also caps → return original error so autopilot pauses (EU-82). Broken network/auth on partner → return original error (nothing proven about the cap). |
| `"transient"` (rate-limit blip) | Never fail over to the other model. Back off and retry on the SAME model that hit it, or pass through unchanged (for GLM the passthrough is immediate — no backoff needed). The operator chose GLM to avoid burning the native subscription on transient spikes. |

This distinction matters because `"cap"` means "all remaining credits on this model are gone" —
the partner has headroom. `"transient"` means "temporary throttling" — retrying on the same model
is correct; switching tiers would waste capacity and produce noise.

See `tests/eu475_symmetric_fallback_test.py` for regression proofs of both directions side-by-side,
including the transient guarantees. See `tests/eu212_fallback_audit_test.py` and
`tests/eu512_glm_fallback_audit_test.py` for activation-instrumentation audits.

- `tests/vacuous_assertion_guard_test.py` — AST-lints every harness for the two recurring
  vacuous-assertion classes (unguarded `.find()` comparisons; literal-`True` check conditions).
- `tests/stub_signature_test.py` — every double bound to a registered production seam accepts
  the seam's full signature.
- `tests/retired_subsystems_test.py` — a deleted subsystem cannot stay referenced live
  (registry-driven; extended by every deletion land).
- `tests/drain_log_pointer_test.py` — the concurrent drain's per-ticket file must resolve to
  a real path (the pointer note was the old fallback when no handle existed).
- `tests/eu543_live_log_e2e_test.py` — sibling to eu639: asserts stub-signature removal
  (AC4, `[EU-380]`/`[EU-253]` text never leaks into real content), set-based isolation
  (AC3, zero shared lines between concurrent tickets), SSE fallback guard (AC2, no "shared
  drain stream" frame when per-ticket files exist), and warroom.js newest-line-first posture.
- `tests/run_all.py` — the union gate itself; its verdict exits non-zero and its wall-clock
  budget keeps the gate honest.
