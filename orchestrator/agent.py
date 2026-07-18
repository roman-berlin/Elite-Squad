"""Shared Agent SDK helper.

Wraps `claude_agent_sdk.query()`, collects the assistant text + cost/turn metadata,
and (when a `tag` is given) streams a compact line per tool use so you can see the
officer working in real time.

EU-108 Sonnet-cap fallback (EU-214: prose realigned to the per-call, self-clearing design —
the old calendar-reset weekly-pin state machine was deleted in Phase-2 Task 2):
  When a Sonnet call hits a CAP-classified plan-limit error (the message names an exhausted
  usage/plan/weekly quota), the fallback logic retries with Opus once, per call, to distinguish
  between:
    • Sonnet sub-limit hit → Opus succeeds cleanly → return the Opus result for THIS call only;
      nothing is persisted, so the very next call starts cheap on Sonnet again — the fallback
      self-clears the moment Sonnet recovers, with no calendar-based reset of any kind.
    • All-models cap hit → Opus also 429s → pause (existing EU-82 governor behavior)
  A transient per-minute 429 / 529 overload instead gets a bounded backoff retry on Sonnet and
  never probes Opus; a failed Opus probe (auth/network) never changes any state either.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

try:  # tool-use block type name can vary across SDK versions
    from claude_agent_sdk import ToolUseBlock
except Exception:  # pragma: no cover
    ToolUseBlock = None

try:  # EU-248: absent on some SDK-stub test harnesses that don't define every export — degrade
    # gracefully; the isinstance() check below just never matches when ProcessError is None.
    from claude_agent_sdk import ProcessError
except Exception:  # pragma: no cover
    ProcessError = None

# Serialises the LOCAL-tier routing window: the Ollama base-URL/key swap is process-global env,
# so overlapping routed calls must not interleave their save/restore (2026-07-06 review). Held
# for the duration of one routed agent call; acquired via asyncio.to_thread so event loops in
# any thread queue without blocking. Retired when Phase-2 routing moves to per-call options.
_ROUTED_ENV_LOCK = threading.Lock()


@dataclass
class AgentRun:
    text: str            # concatenated assistant text across the run
    final: str           # text of the final message (the verdict / summary)
    cost_usd: float
    num_turns: int
    is_error: bool
    tools: list[str] = field(default_factory=list)   # tool calls made, for the transcript
    input_tokens: int = 0    # prompt + cache tokens this run (for the usage ledger)
    output_tokens: int = 0   # completion tokens this run
    # EU-118: true when this run hit a plan limit (429 / usage-limit) for either Claude or GLM.
    # Detection matches _CAP_PATTERNS + _TRANSIENT_PATTERNS below against the Agent SDK's
    # message.error field; the SDK surfaces Anthropic API errors (HTTP 429 with "rate limit"
    # or "usage limit" details) and GLM/z.ai errors (quota/credit/balance issues) there when
    # a plan/session/weekly quota is exceeded. EU-202: GLM quota errors are also detected.
    is_plan_limit: bool = False
    # EU-108 hardening (2026-07-05 fake cap alert): how the plan-limit error classified —
    #   "cap"       → the message names an exhausted usage/plan/weekly quota (fallback-eligible)
    #   "transient" → per-minute 429 / 529 overload (retry territory; must never trigger the
    #                 one-shot Opus retry, which is a ~1.7x cost amplifier for that single call)
    #   ""          → not a plan-limit error. Additive field: is_plan_limit keeps its
    #                 EU-118 broad meaning for existing consumers.
    plan_limit_kind: str = ""
    # EU-123: provider information for this run — which provider + model was used
    provider: str = ""          # "Anthropic" or "GLM" (z.ai)
    model_version: str = ""     # Clean model identifier (e.g., "claude-opus-4-8", "glm-4")
    # QW4 (2026-07-05): wall-clock duration of this call — the forensic audit found NO duration
    # was recorded anywhere (0 duration fields across 2,555 audit events).
    duration_s: float = 0.0
    # EU-248: true when this run hit the max-turns ceiling — ResultMessage.subtype == "error_max_turns"
    # OR num_turns >= max_turns. Set straight off the structured ResultMessage (never from the SDK's
    # racy _last_error_result_text replacement), so it stays correct even when the CLI's trailing raw
    # ProcessError degrades to a clean is_error return below instead of raising.
    is_turn_limit: bool = False


# QW4: optional audit sink — when a process configures it (main/server startup, beside
# usage.configure), every agent call also lands a structured `agent_call` event in audit.jsonl
# {tag, model, provider, tokens in/out, cost, duration, turns, ticket, pass}. audit.jsonl stays the
# single source of truth for per-call economics; usage_ledger.jsonl remains the compact rollup the
# gauges read. Unconfigured (tests, ad-hoc imports) → no-op.
_AUDIT_SINK = None


def configure_audit(audit) -> None:
    """Point agent_call instrumentation at an AuditLog. Call once at process start."""
    global _AUDIT_SINK
    _AUDIT_SINK = audit


# EU-221: wall-clock budgets for SDK stream consumption. Officer calls had turn caps
# (max_turns) but no wall-clock bound — a stalled provider stream (z.ai/GLM has been observed
# hanging with zero output; AUTO-108's planner call ran 30+ min silent on 2026-07-10, and
# EU-228's build hit the same class live on 2026-07-14) could otherwise pin a drain
# indefinitely. Defaults apply even when configure_timeouts() is never called (tests, ad-hoc
# scripts, any entrypoint that forgets to wire it) so the guard is never silently off.
_OFFICER_TIMEOUT_S = 900     # planner/reviewer/pm/etc. — low-effort officer roles
_BUILDER_TIMEOUT_S = 3600    # builder — real code changes legitimately run long


def configure_timeouts(cfg) -> None:
    """Point wall-clock timeout budgets at the run config. Call once at process start
    (main.py / server.py), alongside configure_audit. `getattr` with the existing default
    as fallback so an older/test Config stand-in that doesn't define these fields is fine."""
    global _OFFICER_TIMEOUT_S, _BUILDER_TIMEOUT_S
    _OFFICER_TIMEOUT_S = int(getattr(cfg, "officer_timeout_s", _OFFICER_TIMEOUT_S) or _OFFICER_TIMEOUT_S)
    _BUILDER_TIMEOUT_S = int(getattr(cfg, "builder_timeout_s", _BUILDER_TIMEOUT_S) or _BUILDER_TIMEOUT_S)


def _timeout_for_tag(tag: str) -> float:
    """Per-role wall-clock budget (EU-221). The builder tag gets the larger budget — real
    code changes legitimately run long; every other officer tag (planner/reviewer/pm/adjutant/
    drillmaster/council/...) gets the shorter default, which is where the observed stalls
    (2026-07-10 GLM planner, 2026-07-08 AUTO-93 75-min review) happened."""
    if tag == "builder":
        return _BUILDER_TIMEOUT_S
    return _OFFICER_TIMEOUT_S


# EU-108 hardening: split the old catch-all plan-limit patterns into two classes. A genuine
# quota exhaustion names the cap ("Claude usage limit reached", "weekly limit", "plan limit");
# a transient per-minute 429 or 529 overload only carries status/rate-limit language. Cap
# patterns win when both match (a real cap error usually also carries a 429 status).
# EU-202: add GLM/z.ai-specific patterns (quota, credit, balance, billing).
# EU-210: the generic terms "limit reached" / "over limit" were DROPPED. They made a transient
# "rate limit reached" (or "429: rate limit reached") match a cap pattern via the substring
# "limit reached", so it took the immediate Opus-probe / weekly-cap path instead of the backoff
# retry — the primary false-alarm defect from the audit. A "cap" now requires EXPLICIT
# plan/weekly/usage/quota (or GLM billing) language; anything that is only status/rate-limit
# language falls through to the transient class and gets a backoff retry, never a weekly fallback.
# EU-220: the EU-202 GLM patterns above were themselves too broad — bare "credit", "balance",
# "billing", "insufficient", "quota" substring-match unrelated errors ("insufficient permissions",
# "load balancer" — "balancer" contains "balance") and would wrongly classify them as a plan cap.
# This is higher-stakes since 95042c2: the classifier also runs over ResultMessage.result text
# (where a GLM/z.ai provider-side error lands), so a false positive there can pause a live drain
# for the wrong reason. Replaced with CONTEXTUAL phrases that still cover real z.ai quota text.
_CAP_PATTERNS = ("usage limit", "usage-limit", "plan limit", "weekly limit",
                 "quota exceeded", "insufficient balance", "insufficient quota",
                 "insufficient credit", "account balance", "billing issue", "payment required")
_TRANSIENT_PATTERNS = ("rate limit", "rate_limit", "too many requests",
                       "overloaded", "429", "529")


def _classify_plan_limit(err: str) -> str:
    """Classify an SDK error string: "cap", "transient", or "" (not a plan-limit error)."""
    e = err.lower()
    if any(p in e for p in _CAP_PATTERNS):
        return "cap"
    if any(p in e for p in _TRANSIENT_PATTERNS):
        return "transient"
    return ""


def _tool_brief(name: str, inp, cwd: str | None = None) -> str:
    """Generate a brief description of a tool call.

    EU-197: Relativize file paths to cwd before truncation, so filenames survive.
    For Task/Agent tools, surface subagent_type + description + first line of prompt.
    """
    if isinstance(inp, dict):
        # Handle Task/Agent sub-agent tools specially
        if name in ("Task", "Agent"):
            parts = [name]
            # Try different field names for subagent type
            subagent_type = inp.get("subagent_type") or inp.get("agentType") or inp.get("type")
            if subagent_type:
                parts.append(str(subagent_type))

            # Add description if available
            description = inp.get("description")
            if description:
                parts.append(str(description).splitlines()[0][:50])

            # Add first line of prompt if available
            prompt = inp.get("prompt")
            if prompt:
                first_line = str(prompt).splitlines()[0][:40]
                parts.append(first_line)

            brief = " ".join(parts)
            return brief[:72] if len(brief) > 72 else brief

        # For other tools, handle file paths with relativization
        for k in ("file_path", "path"):
            v = inp.get(k)
            if v and cwd:
                # Relativize the path to cwd before truncation
                try:
                    from pathlib import Path as _Path
                    abs_path = _Path(v).resolve()
                    base_path = _Path(cwd).resolve()
                    try:
                        rel_path = abs_path.relative_to(base_path)
                        # Use the relative path (more readable)
                        v_str = str(rel_path)
                    except ValueError:
                        # Path is not relative to cwd (different mount, etc.),
                        # or symlink resolution created different canonical paths.
                        # EU-209: Path.resolve() follows symlinks, so if cwd and file_path
                        # are accessed through different symlink structures, relativization
                        # may fail. Fall back to just the filename (acceptable behavior).
                        v_str = abs_path.name
                except Exception:
                    # If relativization fails, use original value
                    v_str = str(v)

                return f"{name} {v_str[:72]}"
            elif v:
                # No cwd provided, use original behavior but truncate
                return f"{name} {str(v).splitlines()[0][:72]}"

        # For non-path fields (command, pattern, url), use original behavior
        for k in ("command", "pattern", "url"):
            v = inp.get(k)
            if v:
                return f"{name} {str(v).splitlines()[0][:72]}"

    return name or "tool"


async def run_agent(prompt: str, options: ClaudeAgentOptions, tag: str = "",
                    ticket_id: str | None = None, pass_number: int | None = None,
                    routing_tier: str | None = None) -> AgentRun:
    # EU-38: `ticket_id` + `pass_number` let a build/soldier pass tag its ledger line so per-pass
    # input tokens are sliceable by ticket (the real cost lever). Optional + keyword-defaulted, so
    # every existing caller (officers/chat that pass only `tag`) is unaffected.

    # EU-174: hybrid routing — tier-based endpoint switching. The mutations are process-global
    # (env base URL/key) plus options.model, so they are scoped with try/finally and restored
    # with unset-aware semantics. The 2026-07-05 audit (§6 defect 2) found the old restore was
    # skipped entirely when ANTHROPIC_BASE_URL was originally unset — one LOCAL-tier call
    # permanently pointed the whole process at Ollama — and no exception path restored anything.
    # EU-189: GLM backend is applied per-call via options.env inside _run_agent_unrouted and is
    # mutually exclusive with EU-174 tier routing (which mutates process-global env). When GLM is
    # the run's backend, bypass routing entirely and go straight to the single seam.
    from . import backends as _backends
    if _backends.current() == _backends.GLM:
        return await _run_agent_unrouted(prompt, options, tag=tag, ticket_id=ticket_id,
                                         pass_number=pass_number)

    if not routing_tier:
        return await _run_agent_unrouted(prompt, options, tag=tag, ticket_id=ticket_id,
                                         pass_number=pass_number)

    import asyncio as _asyncio
    import os as _os
    from . import routing as _routing
    tier = _routing.RoutingTier(routing_tier)

    if tier != _routing.RoutingTier.LOCAL:
        # Cloud tier touches only THIS call's options.model — no process-global state.
        _original_model = getattr(options, "model", None)
        cloud_model = _routing.get_model_for_tier(tier)
        if cloud_model and hasattr(options, "model"):
            options.model = cloud_model
        try:
            return await _run_agent_unrouted(prompt, options, tag=tag, ticket_id=ticket_id,
                                             pass_number=pass_number)
        finally:
            if hasattr(options, "model"):
                options.model = _original_model

    # LOCAL tier mutates process-global env (base URL/key → Ollama). 2026-07-06 review: two
    # overlapping routed calls could interleave save/restore — the later starter snapshots the
    # earlier one's Ollama URL as its "original" and restores it after the pop, re-stranding the
    # whole process (§6 defect 2 reopened via concurrency). The mutation window is therefore
    # serialised process-wide; the Phase-2 routing slice replaces env mutation with per-call
    # option routing, retiring this lock. An UNROUTED call overlapping a LOCAL window is still
    # served by the mutated env — a known limitation until that slice. The blocking acquire runs
    # off-loop (to_thread) so no event loop is ever stalled while queueing.
    await _asyncio.to_thread(_ROUTED_ENV_LOCK.acquire)
    try:
        _original_base_url = _os.environ.get("ANTHROPIC_BASE_URL")
        _original_api_key = _os.environ.get("ANTHROPIC_API_KEY")
        _original_model = getattr(options, "model", None)

        base_url = _routing.get_base_url_for_tier(tier)
        api_key = _routing.get_api_key_for_tier(tier)
        model = _routing.get_model_for_tier(tier)
        if base_url:
            _os.environ["ANTHROPIC_BASE_URL"] = base_url
        if api_key:
            _os.environ["ANTHROPIC_API_KEY"] = api_key
        if hasattr(options, "model"):
            options.model = model

        try:
            # The ledger/audit records inside see the ROUTED model — deliberate: the row must say
            # what was actually served. Restore runs after them, exception or not.
            return await _run_agent_unrouted(prompt, options, tag=tag, ticket_id=ticket_id,
                                             pass_number=pass_number)
        finally:
            if _original_base_url is not None:
                _os.environ["ANTHROPIC_BASE_URL"] = _original_base_url
            else:
                _os.environ.pop("ANTHROPIC_BASE_URL", None)
            if _original_api_key is not None:
                _os.environ["ANTHROPIC_API_KEY"] = _original_api_key
            else:
                _os.environ.pop("ANTHROPIC_API_KEY", None)
            if hasattr(options, "model"):
                options.model = _original_model
    finally:
        _ROUTED_ENV_LOCK.release()


async def _run_agent_unrouted(prompt: str, options: ClaudeAgentOptions, tag: str = "",
                              ticket_id: str | None = None,
                              pass_number: int | None = None) -> AgentRun:
    chunks: list[str] = []
    tools: list[str] = []
    final = ""
    cost = 0.0
    turns = 0
    in_tok = 0
    out_tok = 0
    is_error = False
    is_turn_limit = False
    is_plan_limit = False
    plan_limit_kind = ""

    # EU-189: point THIS call at the run's chosen backend (Opus/GLM) via options.env just before
    # the SDK query. No-op for Opus; for GLM it writes the z.ai endpoint + bearer into options.env
    # (per-subprocess only — zero process-global mutation) and overrides options.model. Returns the
    # backend actually applied (fail-closed to Opus if GLM is unconfigured).
    from . import backends as _backends
    effective_backend = _backends.apply(options)
    # EU-255: credential-minimize THIS officer subprocess (it builds/tests injection-prone,
    # untrusted product code). The SDK builds the child env as {**os.environ, **options.env}
    # (subprocess_cli.py), so blank the orchestrator-only Jira/Telegram creds in options.env — a
    # key merely ABSENT from options.env still inherits from the parent. Applied at this single
    # seam (after apply(), for both backends) rather than inside apply(), which stays a pure
    # model/backend transform. Native model auth (CLAUDE_CODE_OAUTH_TOKEN) is not sensitive by
    # this predicate, so it passes through untouched; the raw GLM_AUTH_TOKEN *is* stripped since
    # EU-371(3) — harmless for GLM runs, because apply() above already read it in the parent and
    # injected it into options.env under ANTHROPIC_AUTH_TOKEN, a key the strip never touches.
    _strip = _backends.secret_strip_overrides()
    if _strip:
        _merged_env = dict(getattr(options, "env", None) or {})
        _merged_env.update(_strip)
        options.env = _merged_env
    # EU-123: capture provider info — label from the backend actually applied, not a global sniff.
    from . import provider as _provider
    model = getattr(options, "model", "") or ""
    provider, model_version = _provider.get_provider_info(model, backend=effective_backend)

    import time as _time
    _t0 = _time.monotonic()

    saw_result = False
    import asyncio as _asyncio
    budget_s = _timeout_for_tag(tag)
    try:
        # EU-221: bound total stream-consumption wall-clock time. A stalled provider stream
        # (zero messages for the whole budget) is the case this exists for, but the budget is a
        # simple overall cap on the loop below — not a per-message idle timer — which also
        # contains a stream that produces a few tokens then wedges. asyncio.timeout() cancels
        # THIS task on expiry; that cancellation is thrown into query()'s __anext__() while it's
        # suspended deep inside the SDK (InternalClient.process_query -> Query.close ->
        # transport.close()), whose own try/finally chain terminates + reaps the CLI subprocess
        # as the cancellation unwinds — before the TimeoutError below ever runs.
        async with _asyncio.timeout(budget_s):
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    parts: list[str] = []
                    for b in message.content:
                        if isinstance(b, TextBlock):
                            parts.append(b.text)
                            # EU-197: write officer reasoning to transcript
                            try:
                                from . import transcript
                                transcript.write_text(b.text)
                            except Exception:  # noqa: BLE001 — best-effort
                                pass
                        elif ToolUseBlock is not None and isinstance(b, ToolUseBlock):
                            brief = _tool_brief(getattr(b, "name", ""), getattr(b, "input", None),
                                             cwd=getattr(options, "cwd", None))
                            tools.append(brief)
                            if tag:
                                print(f"      · {tag}: {brief}", flush=True)
                            # EU-197: write full tool input to transcript
                            try:
                                from . import transcript
                                tool_name = getattr(b, "name", "")
                                tool_input = getattr(b, "input", None)
                                transcript.write_tool_use(tool_name, tool_input)
                            except Exception:  # noqa: BLE001 — best-effort
                                pass
                    text = "".join(parts)
                    if text:
                        chunks.append(text)
                        final = text
                    # EU-118: detect plan-limit errors in message errors
                    if getattr(message, "error", None):
                        is_error = True
                        kind = _classify_plan_limit(str(getattr(message, "error", "")))
                        if kind:
                            is_plan_limit = True
                            if plan_limit_kind != "cap":  # a cap sighting outranks a transient one
                                plan_limit_kind = kind
                elif isinstance(message, ResultMessage):
                    saw_result = True
                    cost = message.total_cost_usd or 0.0
                    turns = message.num_turns
                    is_error = is_error or message.is_error
                    # EU-248: capture the turn-limit signal straight off the structured ResultMessage,
                    # BEFORE any exception handling — the CLI exits 1 by design after error_max_turns and
                    # the SDK's own structured-error replacement (_last_error_result_text) is racy, so this
                    # is the one reliable place to observe it.
                    subtype = getattr(message, "subtype", "") or ""
                    max_turns = getattr(options, "max_turns", None)
                    if subtype == "error_max_turns" or (max_turns and turns >= max_turns):
                        is_turn_limit = True
                    if message.result:
                        final = message.result
                    # A provider-side terminal error rides in ResultMessage.result (NOT message.error) —
                    # that's where GLM/z.ai quota text lands. Classify it here or the EU-82/EU-191 pause
                    # never engages for GLM caps (the EU-202 blind spot, 2026-07-09).
                    if message.is_error and message.result:
                        kind = _classify_plan_limit(str(message.result))
                        if kind:
                            is_plan_limit = True
                            if plan_limit_kind != "cap":
                                plan_limit_kind = kind
                    u = getattr(message, "usage", None)
                    if isinstance(u, dict):
                        in_tok = (int(u.get("input_tokens", 0) or 0)
                                  + int(u.get("cache_read_input_tokens", 0) or 0)
                                  + int(u.get("cache_creation_input_tokens", 0) or 0))
                        out_tok = int(u.get("output_tokens", 0) or 0)
    except TimeoutError:
        # EU-221: the budget elapsed with the stream stalled (partial or zero output) — the SDK
        # has already reaped the subprocess (see the comment above). Classify as a clean,
        # non-exceptional error result (transient/infra, deliberately NOT is_plan_limit — this is
        # not a quota exhaustion) so the existing fail-safe paths handle it exactly like any other
        # error return: planner -> BUILD, reviewer -> retry/fail-safe, builder -> ERRORED + retry.
        is_error = True
        final = f"wall-clock timeout after {budget_s}s"
    except Exception as exc:  # noqa: BLE001 — see below; genuine crashes re-raise
        # SDK quirk (claude_agent_sdk 0.2.x): after a CLI error result whose `errors` array is empty,
        # the SDK raises "Claude Code returned an error result: success" — literally "success" —
        # DISCARDING the real error text (which we already captured in `final` from the ResultMessage)
        # and skipping metering/audit/transcript. 9 runs died that way since 06-21 (EU-136 + AUTO-73 on
        # 07-09 alone). If we already consumed the result, degrade to a normal is_error return so the
        # loop posts the REAL failure and the run ends cleanly. EU-248: narrowed from a broad
        # startswith("...error result") to literally "...error result: success" — a genuine
        # `error_during_execution` result must NOT be silently swallowed here; it still re-raises.
        is_success_quirk = saw_result and str(exc).startswith(
            "Claude Code returned an error result: success")
        # EU-248: max-turns exhaustion surfaces as a raw ProcessError ("Command failed with exit code
        # 1 … Check stderr output for details") trailing a result we already consumed — the CLI exits
        # 1 by design after error_max_turns, and the SDK's structured-error replacement above is racy,
        # so no informative exception ever arrives (claude_agent_sdk/_internal/transport/
        # subprocess_cli.py). Degrade the same way as the quirk above: usage.record + the agent_call
        # audit row below still fire, no exception escapes, and `is_turn_limit` (set from the
        # ResultMessage branch above) survives onto the returned AgentRun. A ProcessError with NO
        # result seen (saw_result False — a genuine mid-stream crash) is unaffected and still re-raises
        # onto the existing infra/crash path.
        is_trailing_process_error = (saw_result and ProcessError is not None
                                     and isinstance(exc, ProcessError))
        if is_success_quirk or is_trailing_process_error:
            is_error = True
        else:
            raise

    duration_s = round(_time.monotonic() - _t0, 2)

    # One choke-point for the token ledger: every officer/builder/soldier/chat call lands here.
    try:
        from . import usage as _usage
        _usage.record(getattr(options, "model", "") or "", in_tok, out_tok, cost, tag,
                      ticket_id=ticket_id, pass_number=pass_number, provider=provider,
                      duration_s=duration_s)
    except Exception:  # noqa: BLE001 — metering must never break a run
        pass

    # QW4: per-call instrumentation into audit.jsonl — model, tokens in/out, duration (the three
    # fields the 2026-07-05 forensics found missing from every one of the 2,555 audit events).
    if _AUDIT_SINK is not None:
        try:
            extra = {}
            if ticket_id:
                extra["ticket_id"] = str(ticket_id)
            if pass_number is not None:
                extra["pass_number"] = pass_number
            _AUDIT_SINK.record("agent_call", tag=tag or "",
                               model=getattr(options, "model", "") or "",
                               provider=provider, model_version=model_version,
                               input_tokens=in_tok, output_tokens=out_tok,
                               cost_usd=round(cost, 6), duration_s=duration_s,
                               turns=turns, **extra)
        except Exception:  # noqa: BLE001 — instrumentation must never break a run
            pass

    # EU-197: write final result to transcript
    try:
        from . import transcript
        transcript.write_result(final)
    except Exception:  # noqa: BLE001 — best-effort
        pass

    return AgentRun(text="\n".join(chunks), final=final, cost_usd=cost,
                    num_turns=turns, is_error=is_error, tools=tools,
                    input_tokens=in_tok, output_tokens=out_tok, is_plan_limit=is_plan_limit,
                    plan_limit_kind=plan_limit_kind,
                    provider=provider, model_version=model_version, duration_s=duration_s,
                    is_turn_limit=is_turn_limit)


# ── Sonnet-cap → one-shot Opus retry ─────────────────────────────────────────────────────────────────
# When Sonnet hits a CAP-classified plan-limit error, we retry the CURRENT work once with Opus to
# distinguish between:
#   • Sonnet sub-limit (Opus succeeds cleanly) → return the Opus result, persist nothing
#   • All-models cap (Opus also fails) → pause (return the is_plan_limit result; EU-82)
# Transient rate limits (per-minute 429, 529 overload) get one backoff retry on Sonnet instead.
# This is a thin wrapper around run_agent that adds the retry logic. The EU-108 weekly-Opus PIN was
# deleted (Phase-2 Task 2): a Sonnet cap must never pin a week of the most expensive tier.

# EU-210: backoff-then-retry a transient Sonnet rate limit up to this many times, spaced this many
# seconds apart, BEFORE ever escalating it into cap handling — a single transient 429 must not be
# misread as the weekly cap. Module-level so tests (and an operator in a pinch) can zero/shrink them.
_TRANSIENT_RETRY_ATTEMPTS = 2
_TRANSIENT_RETRY_BACKOFF_S = 2.0


async def run_agent_with_fallback(prompt: str, options: ClaudeAgentOptions, tag: str = "",
                                   ticket_id: str | None = None, pass_number: int | None = None,
                                   cfg=None, routing_tier: str | None = None) -> AgentRun:
    """Run a Sonnet agent with a per-call, one-shot Opus fallback on a Sonnet weekly cap.

    When a Sonnet call hits a plan-limit:
      1. Transient rate limit (per-minute 429 / 529 overload) → back off and retry Sonnet up to
         `_TRANSIENT_RETRY_ATTEMPTS` times (~`_TRANSIENT_RETRY_BACKOFF_S` apart); the first retry
         that comes back clean returns immediately — never probes Opus, never touches fallback
         state (EU-210). If EVERY retry is still transient, that's no longer just a blip — escalate
         into cap handling below instead of silently returning the still-transient result.
      2. Cap-classified error ("usage limit reached" etc.) — or a transient error that survived
         every backoff retry — → retry once with Opus so the CURRENT unit of work can finish if
         Opus still has headroom.
      3. If Opus succeeds cleanly (is_error False) → return the Opus result, persisting NOTHING.
         The next call starts cheap on Sonnet again. (The EU-108 weekly-Opus pin was deleted in
         Phase-2 Task 2 — a Sonnet cap must never pin a week of the most expensive tier.)
      4. If Opus also hits a limit → it's the All-models cap: return the original is_plan_limit
         result so autopilot's proactive poll pauses (EU-82). If the Opus probe fails for any other
         reason (auth/network), that proves nothing about the cap — return the Sonnet error.

    Args:
        prompt: The agent prompt
        options: ClaudeAgentOptions with model, system_prompt, etc.
        tag: Tag for usage ledger (e.g. "builder", "reviewer")
        ticket_id: Optional ticket ID for per-pass tracking
        pass_number: Optional pass number for per-pass tracking
        cfg: Config object (passed through for ledger/audit context)

    Returns:
        AgentRun with the result (either from Sonnet or the one-shot Opus retry)
    """
    from .models import OPUS
    from . import backends as _backends

    # EU-189: under a GLM run there is no Sonnet-cap → native-Opus fallback (that semantics is
    # Anthropic-only and would burn the Max subscription the operator chose GLM to avoid). Read the
    # run's backend from cfg first (robust — cfg is passed by builder/reviewer) then the contextvar;
    # if GLM, run straight through with no Opus probe. If cfg says GLM but the run-scoped contextvar
    # wasn't pinned (a caller outside loop.run, or across a context-dropping boundary), pin it for
    # this call so the seam in _run_agent_unrouted actually applies GLM instead of silently running
    # Opus — keeps the cfg-view and the contextvar-view of the backend consistent (security review).
    if _backends.is_glm(cfg):
        _tok = None if _backends.current() == _backends.GLM else _backends.set_backend(_backends.GLM)
        try:
            return await run_agent(prompt, options, tag=tag, ticket_id=ticket_id,
                                   pass_number=pass_number, routing_tier=routing_tier)
        finally:
            if _tok is not None:
                _backends.reset_backend(_tok)

    # Get the configured model
    model = getattr(options, "model", "") or ""

    # The Sonnet-cap → one-shot-Opus fallback only applies to Sonnet calls.
    is_sonnet = model and "sonnet" in model.lower()

    if not is_sonnet:
        # Not Sonnet — nothing to fall back from; run normally.
        return await run_agent(prompt, options, tag=tag, ticket_id=ticket_id, pass_number=pass_number, routing_tier=routing_tier)

    # Try Sonnet first
    result = await run_agent(prompt, options, tag=tag, ticket_id=ticket_id, pass_number=pass_number, routing_tier=routing_tier)

    # If Sonnet succeeded or hit a non-plan-limit error, return as-is
    if not result.is_plan_limit:
        return result

    # Transient rate limit (per-minute 429, 529 overload) — not (yet) a quota exhaustion. Back off
    # and retry Sonnet up to _TRANSIENT_RETRY_ATTEMPTS times; probing Opus (or classifying as the
    # weekly cap) over a single blip would burn the expensive tier / raise a false alarm needlessly
    # (EU-210). The first retry that comes back clean returns immediately.
    if result.plan_limit_kind != "cap":
        import asyncio
        for attempt in range(1, _TRANSIENT_RETRY_ATTEMPTS + 1):
            if _TRANSIENT_RETRY_BACKOFF_S > 0:
                print(f"  · transient rate-limit on {model} — retry {attempt}/"
                      f"{_TRANSIENT_RETRY_ATTEMPTS} after {_TRANSIENT_RETRY_BACKOFF_S:.0f}s "
                      f"(no weekly fallback)", flush=True)
                await asyncio.sleep(_TRANSIENT_RETRY_BACKOFF_S)
            result = await run_agent(prompt, options, tag=tag, ticket_id=ticket_id,
                                     pass_number=pass_number, routing_tier=routing_tier)
            if not result.is_plan_limit or result.plan_limit_kind == "cap":
                # Clean → return immediately. Explicit cap language → stop retrying as
                # "transient" and fall straight into cap handling below.
                break

        if not result.is_plan_limit:
            return result

        if result.plan_limit_kind != "cap":
            # Every backoff retry ALSO came back transient — no longer just a blip. Escalate into
            # the cap-handling path below so a persistently-failing transient error still gets the
            # Opus-probe / EU-82 pause semantics instead of being silently returned as-is.
            print(f"  · transient rate-limit on {model} persisted through "
                  f"{_TRANSIENT_RETRY_ATTEMPTS} retries — escalating to cap handling", flush=True)

    # Sonnet hit a cap-classified plan-limit error (or a transient one that survived every backoff
    # retry) — try Opus once to distinguish the limit type.
    # EU-212: audit the activation itself (classification reason, original Sonnet error text, tag)
    # so the cockpit can show WHY fallback is active — without reviving the deleted weekly-pin
    # state machine (still per-call, still auto-exits next call). Best-effort: instrumentation must
    # never break a run. Emitted only here, never on the transient backoff-and-retry-Sonnet path
    # above (that path never reaches this line unless every retry escalated into cap handling).
    if _AUDIT_SINK is not None:
        try:
            extra = {}
            if ticket_id:
                extra["ticket_id"] = str(ticket_id)
            if pass_number is not None:
                extra["pass_number"] = pass_number
            _AUDIT_SINK.record("sonnet_fallback_activated", tag=tag or "", model=model,
                               reason=result.plan_limit_kind, error=result.final, **extra)
        except Exception:  # noqa: BLE001 — instrumentation must never break a run
            pass

    # 2026-07-05 audit §6 defect 1: the old manual rebuild here dropped cwd, hooks (the guard
    # denylist) and disallowed_tools — the Opus probe ran in the orchestrator's own CWD with no
    # guard under the inherited bypassPermissions, and its output was used as the build result.
    # A shallow copy preserves every field by construction; the model is the only difference.
    import copy
    opus_options = copy.copy(options)
    opus_options.model = OPUS

    # Try Opus once. Deliberately NOT passing routing_tier: the retry's whole point is to run on
    # Anthropic Opus — routing it (CLOUD would rewrite the model to glm-5.2) would re-run the routed
    # model instead of the Opus retry we intend (2026-07-06 review).
    opus_result = await run_agent(prompt, opus_options, tag=tag, ticket_id=ticket_id, pass_number=pass_number)

    if opus_result.is_plan_limit:
        # Opus also hit a limit — this is the All-models cap, not just Sonnet.
        # Return the original Sonnet error so autopilot's proactive poll pauses (EU-82).
        return result

    if opus_result.is_error:
        # The retry itself failed (auth, network, …) — that proves nothing about the Sonnet
        # cap. Surface the original Sonnet plan-limit error so upstream handling (EU-82 pause)
        # still sees it.
        return result

    # Opus succeeded where Sonnet was capped — a Sonnet-tier sub-limit, not an All-models cap.
    # Return the Opus result so THIS unit of work completes. Persist NOTHING: the EU-108 weekly-Opus
    # pin was deleted (Phase-2 Task 2) because pinning the most expensive tier for a week is
    # anti-economics. The next call starts cheap on Sonnet again; if Sonnet is still capped it
    # independently retries Opus per-call, and an All-models cap surfaces above as is_plan_limit →
    # EU-82 pause. Never a week of pinned Opus.
    print(f"  · Sonnet cap on {tag or model} — this pass ran on Opus (no weekly pin)", flush=True)
    return opus_result
