"""EU-146 regression suite — vitest orphaned-process prevention.

Three layers of defence must ALL stay wired:

  1. Guard denylist (guard.py): `vitest` without --run is BLOCKED at the PreToolUse
     hook level before an agent can even launch the process.

  2. Process-group cleanup (gate.py / squad.py): every subprocess spawned by the
     orchestrator runs in its own session (start_new_session=True) and the entire
     process group is SIGKILL-ed in the finally block so vitest worker forks can't
     outlive the parent.

  3. Prompt instructions (builder.py / squad.py): the BUILDER_SYSTEM and soldier
     system prompts mandate `vitest run` / `--run` and bound workers — the agent is
     told the correct incantation before it types a single command.

This file pins all three so a future edit that undoes any part flips a test FAIL
instead of silently re-opening the memory leak.
"""
import sys
import types
import asyncio
from pathlib import Path

# ── stub claude_agent_sdk so the orchestrator imports cleanly without the real SDK ──
sdk = types.ModuleType("claude_agent_sdk")
class _HookMatcher:
    def __init__(self, **kw): self.__dict__.update(kw)
class _GenericStub:
    def __init__(self, *a, **kw): pass
    def __call__(self, *a, **kw): return self
sdk.HookMatcher = _HookMatcher
sdk.ClaudeAgentOptions = _GenericStub
sdk.__getattr__ = lambda n: _GenericStub
sys.modules.setdefault("claude_agent_sdk", sdk)
sys.path.insert(0, ".")

from orchestrator import guard  # noqa: E402


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────

results: list[tuple[str, bool, str]] = []

def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))

def blocked(tool: str, **inp) -> bool:
    return guard.is_dangerous(tool, inp)[0]

def why(tool: str, **inp) -> str:
    return guard.is_dangerous(tool, inp)[1]


# ════════════════════════════════════════════════════════════
# 1. Guard denylist — vitest watch-mode blocking
# ════════════════════════════════════════════════════════════

# EU-146 AC-1: `vitest` (bare) must be BLOCKED — default mode IS watch mode.
WATCH_MODE_CMDS = [
    "npx vitest",
    "vitest",
    "vitest watch",
    "npx vitest watch",
    "cd apps/web && npx vitest",            # chained — vitest segment has no --run
    "npx vitest --reporter=verbose",        # extra flags but still watch mode
    "vitest --coverage",                    # coverage flag doesn't imply run
    "npx vitest --coverage --reporter dot", # multi-flag, still watch
]
for cmd in WATCH_MODE_CMDS:
    chk(
        f"BLOCK vitest watch: {cmd[:50]}",
        blocked("Bash", command=cmd),
        cmd,
    )

# EU-146 AC-1: `vitest run` and `vitest --run` MUST be allowed — these exit cleanly.
SAFE_VITEST_CMDS = [
    "npx vitest run",
    "vitest run",
    "npx vitest run src/foo.test.ts",
    "npx vitest run --pool=forks --poolOptions.forks.maxForks=2",
    "npx vitest run src/ --pool=forks --poolOptions.forks.maxForks=2",
    "vitest --run",
    "npx vitest --run",
    "npx vitest --run --coverage",
    "cd apps/web && npx vitest run src/auth.test.ts --pool=forks",
    "bun run test:a11y",                   # unrelated bun command — must not false-positive
    "bun test --coverage",                 # bun test runner — no vitest involved
    "python3 tests/run_all.py",            # EU-repo test runner — no vitest
]
for cmd in SAFE_VITEST_CMDS:
    chk(
        f"allow vitest run: {cmd[:50]}",
        not blocked("Bash", command=cmd),
        cmd,
    )

# The denial reason must mention the right fix so agents know what to do.
reason_bare = why("Bash", command="npx vitest")
chk(
    "denial reason mentions 'vitest run' or '--run'",
    "vitest run" in reason_bare or "--run" in reason_bare,
    reason_bare,
)
chk(
    "denial reason mentions orphaned processes / memory",
    "orphan" in reason_bare.lower() or "memory" in reason_bare.lower() or "watch" in reason_bare.lower(),
    reason_bare,
)


# ════════════════════════════════════════════════════════════
# 2. guard.py internals — _vitest_watch_mode() edge cases
# ════════════════════════════════════════════════════════════

_vwm = guard._vitest_watch_mode  # type: ignore[attr-defined]

# Watch-mode detects across shell separators
chk("_vitest_watch_mode bare", _vwm("npx vitest"))
chk("_vitest_watch_mode watch keyword", _vwm("vitest watch"))
chk("_vitest_watch_mode chained &&", _vwm("bun run build && npx vitest"))
chk("_vitest_watch_mode chained |", _vwm("echo ok | npx vitest"))
chk("_vitest_watch_mode chained ;", _vwm("rm -rf dist; npx vitest"))

# Safe forms → False
chk("_vitest_watch_mode run subcommand → safe", not _vwm("npx vitest run"))
chk("_vitest_watch_mode --run flag → safe", not _vwm("npx vitest --run"))
chk("_vitest_watch_mode run with path → safe", not _vwm("npx vitest run src/"))
chk("_vitest_watch_mode --run with coverage → safe", not _vwm("vitest --run --coverage"))
chk("_vitest_watch_mode chained safe", not _vwm("bun install --frozen-lockfile && npx vitest run"))
# Non-vitest commands → False (no false positives)
chk("_vitest_watch_mode bun test → not vitest", not _vwm("bun test --coverage"))
chk("_vitest_watch_mode pytest → not vitest", not _vwm("pytest -q tests/"))
chk("_vitest_watch_mode tsc → not vitest", not _vwm("tsc --noEmit"))


# ════════════════════════════════════════════════════════════
# 3. PreToolUse hook fires end-to-end for vitest watch
# ════════════════════════════════════════════════════════════

def _hook(tool: str, **inp) -> dict:
    return asyncio.run(guard._pretooluse({"tool_name": tool, "tool_input": inp}, "id", {}))

def _denied(out: dict) -> bool:
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

deny_vitest = _hook("Bash", command="npx vitest")
chk("hook DENIES bare vitest (EU-146)", _denied(deny_vitest), str(deny_vitest))
chk(
    "hook deny reason mentions fix (EU-146)",
    "vitest run" in deny_vitest.get("hookSpecificOutput", {}).get("permissionDecisionReason", "")
    or "--run" in deny_vitest.get("hookSpecificOutput", {}).get("permissionDecisionReason", ""),
    str(deny_vitest),
)

allow_vitest_run = _hook("Bash", command="npx vitest run src/ --pool=forks")
chk("hook ALLOWS `vitest run` (EU-146)", allow_vitest_run == {}, str(allow_vitest_run))

allow_vitest_flag = _hook("Bash", command="vitest --run --coverage")
chk("hook ALLOWS `vitest --run` (EU-146)", allow_vitest_flag == {}, str(allow_vitest_flag))


# ════════════════════════════════════════════════════════════
# 4. Process-group cleanup wired in gate.py
# ════════════════════════════════════════════════════════════

gate_src = Path("./orchestrator/gate.py").read_text()

chk(
    "gate.run_commands uses start_new_session=True (EU-146)",
    "start_new_session=True" in gate_src,
    "start_new_session=True not found in gate.py",
)
chk(
    "gate.run_commands calls os.killpg on timeout (EU-146)",
    "os.killpg" in gate_src,
    "os.killpg not found in gate.py",
)
chk(
    "gate.run_commands has a finally cleanup block (EU-146)",
    "finally:" in gate_src and "proc.poll() is None" in gate_src,
    "finally+poll() not found in gate.py",
)
# The finally block must cover BOTH the timeout path AND normal completion path
chk(
    "gate finally block kills the group (not just proc.kill) (EU-146)",
    "os.killpg(os.getpgid(proc.pid), 9)" in gate_src,
    "killpg(getpgid) not found in gate.py finally",
)


# ════════════════════════════════════════════════════════════
# 5. Process-group cleanup wired in squad.py (_run_gate)
# ════════════════════════════════════════════════════════════

squad_src = Path("./orchestrator/squad.py").read_text()

chk(
    "squad._run_gate uses start_new_session=True (EU-146)",
    "start_new_session=True" in squad_src,
    "start_new_session=True not found in squad.py",
)
chk(
    "squad._run_gate calls os.killpg on TimeoutError (EU-146)",
    "os.killpg" in squad_src,
    "os.killpg not found in squad.py",
)
chk(
    "squad._run_gate has a finally cleanup block (EU-146)",
    "finally:" in squad_src and "proc.returncode is None" in squad_src,
    "finally+returncode is None not found in squad.py",
)


# ════════════════════════════════════════════════════════════
# 6. Builder system prompt mandates vitest run / bounded workers
# ════════════════════════════════════════════════════════════

builder_src = Path("./orchestrator/builder.py").read_text()

chk(
    "BUILDER_SYSTEM tells builder to use 'vitest run' (EU-146)",
    "vitest run" in builder_src,
    "'vitest run' not found in BUILDER_SYSTEM",
)
chk(
    "BUILDER_SYSTEM bans watch mode (EU-146)",
    "watch" in builder_src and ("no `vitest`" in builder_src or "Never start watch" in builder_src
                                or "no vitest" in builder_src.lower()),
    "watch-mode ban not found in builder.py",
)
chk(
    "BUILDER_SYSTEM mentions --pool=forks or bounded workers (EU-146)",
    "--pool=forks" in builder_src or "maxForks" in builder_src,
    "--pool=forks / maxForks not found in builder.py",
)


# ════════════════════════════════════════════════════════════
# 7. Soldier system prompts (squad.py) also mandate vitest run
# ════════════════════════════════════════════════════════════

chk(
    "_SOLDIER_SYSTEM mandates 'vitest run' (EU-146)",
    "vitest run" in squad_src,
    "'vitest run' not found in _SOLDIER_SYSTEM",
)
chk(
    "_SOLDIER_SYSTEM bans watch mode (EU-146)",
    "Never start watch mode" in squad_src or "never watch mode" in squad_src.lower(),
    "watch-mode ban not found in squad.py",
)
chk(
    "squad.py mentions maxForks or --pool=forks (EU-146)",
    "--pool=forks" in squad_src or "maxForks" in squad_src,
    "--pool=forks / maxForks not found in squad.py",
)


# ════════════════════════════════════════════════════════════
# Report
# ════════════════════════════════════════════════════════════

print("\n================ EU-146 VITEST ORPHAN REGRESSION ================")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok else ""))
print("-" * 64)
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
