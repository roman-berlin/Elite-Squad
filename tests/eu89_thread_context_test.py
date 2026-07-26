"""EU-89 — recent_thread_context() helper + respond_to_commander() injection.

Tests:
  1. recent_thread_context: no chat / no proposals -> returns ''
  2. recent_thread_context: includes last N chat turns from commander_chat.md
  3. recent_thread_context: filters pending proposals by ticket_ref (match)
  4. recent_thread_context: ignores pending proposals that don't mention ticket_ref
  5. respond_to_commander: thread_ctx is injected into the LLM prompt
"""
import asyncio
import json
import sys
import tempfile
import types
from pathlib import Path

# ---------------------------------------------------------------------------
# Stub the Claude Agent SDK so we can import council without network access.
# ---------------------------------------------------------------------------
sdk = types.ModuleType("claude_agent_sdk")

class _Dummy:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self

sdk.__getattr__ = lambda n: _Dummy
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import council, approvals
from orchestrator.config import Config, AppConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
results: list[tuple[str, bool, str]] = []


def check(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def _make_cfg(tmp: Path) -> Config:
    audit = tmp / "audit.jsonl"
    audit.write_text("")
    return Config(
        apps=[AppConfig(name="automatixy", repo_path=str(tmp),
                        base_branch="DEV", protected_branch="MAIN",
                        backlog_backend="none")],
        audit_path=str(audit),
        use_worktree=False,
    )


# ---------------------------------------------------------------------------
# 1. Empty state: no chat file, no proposals -> ''
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _td:
    cfg = _make_cfg(Path(_td))
    result = council.recent_thread_context(cfg, ticket_ref="AUTO-99")
    check("empty state returns empty string", result == "")

# ---------------------------------------------------------------------------
# 2. Chat file with turns -> included in output
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _td:
    cfg = _make_cfg(Path(_td))
    # Write 5 chat turns (Q and A alternating).
    chat_path = Path(cfg.audit_path).with_name("commander_chat.md")
    chat_path.write_text(
        "Q: Can you look at AUTO-10?\n"
        "A (General): On it — the Builder is queued.\n"
        "Q: What's the status of AUTO-10?\n"
        "A (General): Still in-progress, ETA ~30 min.\n"
        "Q: create it\n",
        encoding="utf-8",
    )
    result = council.recent_thread_context(cfg, n_turns=12)
    check("thread includes recent chat turns", "create it" in result, repr(result[:200]))
    check("thread includes earlier Q/A pairs", "AUTO-10" in result, repr(result[:200]))
    check("section labelled clearly",
          "Recent conversation thread" in result, repr(result[:100]))

# ---------------------------------------------------------------------------
# 3. Pending proposals matching ticket_ref -> included
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _td:
    cfg = _make_cfg(Path(_td))
    # Enqueue a proposal batch that mentions AUTO-42 in the source.
    approvals.enqueue_proposals(
        cfg,
        app_name="automatixy",
        officer_label="council",
        source="council/daily AUTO-42 follow-up",
        report=[{
            "title": "Add rate-limiting to AUTH-42 endpoint",
            "type": "Task",
            "severity": "high",
            "body": "Rate-limiting prevents brute-force on the login route.",
        }],
    )
    result = council.recent_thread_context(cfg, ticket_ref="AUTO-42")
    check("matching proposal batch is included",
          "Add rate-limiting" in result, repr(result[:300]))
    check("proposal section is labelled",
          "Pending proposals" in result, repr(result[:200]))

# ---------------------------------------------------------------------------
# 4. Pending proposals NOT matching ticket_ref -> excluded
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _td:
    cfg = _make_cfg(Path(_td))
    approvals.enqueue_proposals(
        cfg,
        app_name="automatixy",
        officer_label="council",
        source="council/daily AUTO-99 unrelated",
        report=[{"title": "Unrelated proposal", "type": "Task", "severity": "low", "body": ""}],
    )
    result = council.recent_thread_context(cfg, ticket_ref="AUTO-7")
    check("non-matching proposal is excluded", "Unrelated proposal" not in result,
          repr(result[:200]))

# ---------------------------------------------------------------------------
# 5. respond_to_commander: thread_ctx injected into the LLM prompt
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as _td:
    cfg = _make_cfg(Path(_td))

    # Plant a chat turn so thread_ctx returns something.
    chat_path = Path(cfg.audit_path).with_name("commander_chat.md")
    chat_path.write_text("Q: What about AUTO-5?\nA (General): Looking into it.\n",
                         encoding="utf-8")

    # Plant a pending proposal mentioning AUTO-5.
    approvals.enqueue_proposals(
        cfg,
        app_name="automatixy",
        officer_label="council",
        source="daily AUTO-5",
        report=[{"title": "Fix AUTO-5 null check", "type": "Bug",
                 "severity": "high", "body": "Null ptr in login flow."}],
    )

    captured_prompts: list[str] = []

    async def _fake_run_agent(prompt, options, tag=None):
        captured_prompts.append(prompt)
        ns = types.SimpleNamespace
        return ns(final="Acknowledged.", text="Acknowledged.", is_error=False,
                  cost_usd=0.0, num_turns=1, tools=[])

    council.run_agent = _fake_run_agent
    council.notify.send = lambda m: None  # suppress Telegram

    asyncio.run(council.respond_to_commander(cfg, "approve AUTO-5"))

    # EU-602: _needs_specialist also calls run_agent (triage), so use LAST captured prompt (main CTO call).
    injected_prompt = captured_prompts[-1] if captured_prompts else ""
    check("thread context appears in the LLM prompt",
          "Thread context" in injected_prompt, repr(injected_prompt[:400]))
    check("recent chat turns are injected",
          "Looking into it" in injected_prompt, repr(injected_prompt[:600]))
    check("matching pending proposal is injected",
          "Fix AUTO-5 null check" in injected_prompt, repr(injected_prompt[:800]))

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
print("\n========= EU-89 THREAD CONTEXT QA =========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  ({detail})" if detail and not ok else ""))
print(f"  {passed}/{len(results)} passed",
      "✅" if passed == len(results) else "❌")
assert passed == len(results), f"{len(results) - passed} test(s) failed"
