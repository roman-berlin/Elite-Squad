# EU-465: Add Return Annotations & Replace Any Across Orchestrator

## Context

EU-465 names `orchestrator/reviewer.py` for missing return annotations and explicit `Any`. Scanning finds **zero literal violations** in reviewer.py — every match was inside comments/docstrings *describing* the pattern, not actual code. The real gap is broader: **~38 public functions** across the orchestrator package lack return annotations, and **~22 public functions** use `Any`. All files already use `from __future__ import annotations` (PEP 563), so runtime type erasure isn't an issue.

This ticket treats reviewer.py as the canonical exemplar and sweeps the full `orchestrator/` package for the same pattern.

## Approach

For each affected file, systematically add `-> <ReturnType>` to every public `def`/`async def` that lacks one, and replace explicit `Any` with concrete types where possible. If a genuine dynamic type is needed, prefer `Protocol`, `TypeVar`, or `Generic` over bare `Any`.

### Priority Order (smallest first, highest ROI first)

**Tier 1 — Small modules:**

| File | Functions to fix | Strategy |
|------|-----------------|----------|
| `orchestrator/approvals.py` | `approve_proposals()` → `FilingResult \| None` | Docstring says it returns FilingResult or None |
| `orchestrator/backends.py` | `set_hybrid()`, `set_backend()` | Both return `contextvars.Token[str]` |
| `orchestrator/guard.py` | `hooks_config()` | Returns dict; inspect body |
| `orchestrator/decisions.py` | `to_worklist()` | Returns list of dicts; inspect body |

**Tier 2 — Medium modules:**

| File | Functions to fix | Strategy |
|------|-----------------|----------|
| `orchestrator/cockpit_state.py` | `set_run_app()`, `write()`, `flush()`, `isatty()` | Inspect body; `None` or `bool` likely |
| `orchestrator/run_logger.py` | `log_root()`, `_prepare_log_path()`, `open_run_log()`, `write_note_log()` | Already returns `Path` or `Path`; just annotate |
| `orchestrator/config.py` | `normalize_effort()` | Returns `str` |

**Tier 3 — Replace Any with concrete types (smaller files):**

| File | Function | Current | Fix |
|------|----------|---------|-----|
| `orchestrator/architect.py` | `to_dict()` | `dict[str, Any]` | Inspect keys, use TypedDict or keep if truly dynamic |
| `orchestrator/planner.py` | `to_dict()` | `dict[str, Any]` | Same approach |
| `orchestrator/locking.py` | `locked_call()`, `locked_rmw()` | `Any` | Use `TypeVar("R")` for generic callable |
| `orchestrator/transcript.py` | `_redact_secrets()` param + return | `Any` | Use `object` instead (private, less critical but cleaner) |
| `orchestrator/backlog/jira.py` | `comments()` | `list[dict[str, Any]]` | Inspect Jira comment keys; use TypedDict if consistent |

**Tier 4 — Dashboard / Health / Sync (bigger modules with `dict[str, Any]`):**

These return dynamically-keyed data from Jira API / audit logs. Strategy: create lightweight `TypedDict` classes for known structures. Where keys vary widely, keep `dict[str, Any]` but document why.

| File | Functions | Notes |
|------|-----------|-------|
| `orchestrator/dashboard.py` | `load_tasks()`, `latest_needs_you()`, `latest_parked()` (~3 private helpers too) | Create TypedDicts per structure |
| `orchestrator/health.py` | `summary()` | Single dict with known fields |
| `orchestrator/sync.py` | `git_sync()`, `pull_server_state()`, `pull_server_audit()`, `promote()`, `app_promote_status()`, `promote_app()`, `app_promote_status()` (~7 total) | Each may need own TypedDict |

**Tier 5 — server.py Flask route handlers (~45 functions):**

Flask `@app.get`/`@app.post` decorated functions. Most return `Response`, `jsonify(...)`, or template strings. Grouped by pattern:
- JSON responses → `Response`
- Template renders → `str`
- Redirects → `Response`

Apply blanket `-> Response | str`, then refine per-handler after inspection. Key sub-groups:
- `/api/*` routes returning `jsonify` → `Response`
- `*_page` routes returning HTML templates → `str`
- `*_api` routes returning plain strings/errors → `str`
- `gen(...)` generators → should use `Generator[str, None, None]` or similar
- `val(...)` validator → returns bool

## Replacement strategy for `Any`

Where feasible, replace `Any` with concrete types. Observed patterns:

1. **`dict[str, Any]`** from dashboard/helper functions → check if keys are fixed; create `TypedDict` where they are
2. **`locked_call()` and `locked_rmw()`** → `TypeVar("R")` for generic return type
3. **Audit `audit.py: _coerce()`** → `object` instead of `Any` (private)
4. **`config.normalize_effort(value: Any, ...)`** → parameter typed as `object` (accepts anything coerced to str)

## Files to Modify

1. `orchestrator/approvals.py`
2. `orchestrator/backends.py`
3. `orchestrator/guard.py`
4. `orchestrator/decisions.py`
5. `orchestrator/cockpit_state.py`
6. `orchestrator/run_logger.py`
7. `orchestrator/config.py`
8. `orchestrator/dashboard.py`
9. `orchestrator/health.py`
10. `orchestrator/sync.py`
11. `orchestrator/architect.py`
12. `orchestrator/planner.py`
13. `orchestrator/backlog/jira.py`
14. `orchestrator/locking.py`
15. `orchestrator/server.py`
16. `orchestrator/transcript.py` (minor)
17. `orchestrator/audit.py` (minor)

## Verification

1. Re-run AST scanner after changes — zero public functions missing return annotations, zero `Any` in signatures
2. Run existing reviewer tests: `python3 tests/eu463_typing_gate_current_diff_test.py`
3. Run existing reviewer hardening tests: `python3 tests/eu441_reviewer_hardening_test.py`
4. `python3 -m py_compile orchestrator/*.py` — syntax check
5. No behavior change — only adding type hints

## Manual Test

TEST: (no UI — static analysis pass: re-run AST scanner, confirm zero violations)
