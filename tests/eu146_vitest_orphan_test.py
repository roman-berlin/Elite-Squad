"""EU-146 regression suite — vitest orphaned-process prevention.

Four layers of defence must ALL stay wired:

  1. Guard denylist (guard.py): `vitest` without --run is BLOCKED at the PreToolUse
     hook level before an agent can even launch the process.

  2. Process-group cleanup (gate.py / squad.py): every subprocess spawned by the
     orchestrator runs in its own session (start_new_session=True) and the entire
     process group is SIGKILL-ed in the finally block so vitest worker forks can't
     outlive the parent.

  3. Prompt instructions (builder.py / squad.py): the BUILDER_SYSTEM and soldier
     system prompts mandate `vitest run` / `--run` and bound workers — the agent is
     told the correct incantation before it types a single command.

  4. Cockpit terminal endpoint (server.py `/api/terminal`): a command typed directly
     into the cockpit's terminal panel is ALSO run in its own process group and the
     whole group is SIGKILL-ed on timeout, so a backgrounded/forked child (e.g. a
     bare `vitest` typed by hand) can't survive as an orphan either.

This file pins all four so a future edit that undoes any part flips a test FAIL
instead of silently re-opening the memory leak.
"""
import sys
import types
import asyncio
import os
import tempfile
import time
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

# ── stub requests so orchestrator.server imports cleanly without the real dep ──
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules.setdefault("requests", req)

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
# 5. Cockpit terminal endpoint (/api/terminal) — process-group cleanup
# ════════════════════════════════════════════════════════════

from orchestrator import server  # noqa: E402
from orchestrator.config import AppConfig, Config  # noqa: E402

_TMP = Path(tempfile.mkdtemp())
_AUDIT = _TMP / "audit.jsonl"
_TCFG = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(_TMP), base_branch="DEV",
                     protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(_AUDIT),
    use_worktree=False,
)
_TCFG.detected_auth = lambda: "test"
_TCLIENT = server.create_app(_TCFG).test_client()

# Source-level check: terminal_api must use the same process-group pattern as gate.py
# (Popen + start_new_session=True + os.killpg on timeout + a finally-block sweep).
server_src = Path("./orchestrator/server.py").read_text()

chk(
    "terminal_api uses subprocess.Popen with start_new_session=True (EU-146)",
    "start_new_session=True" in server_src,
    "start_new_session=True not found in server.py",
)
chk(
    "terminal_api calls os.killpg on timeout (EU-146)",
    "os.killpg" in server_src,
    "os.killpg not found in server.py",
)
chk(
    "terminal_api has a finally cleanup block killing the group (EU-146)",
    "finally:" in server_src and "os.killpg(os.getpgid(proc.pid), 9)" in server_src,
    "finally + killpg(getpgid) not found in server.py",
)

# Fast command: 200 + exact JSON shape.
resp = _TCLIENT.post("/api/terminal", data={"cmd": "echo hi"})
chk("fast command: HTTP 200", resp.status_code == 200, f"status={resp.status_code}")
j = resp.get_json()
chk("fast command: output == 'hi\\n'", j.get("output") == "hi\n", str(j))
chk("fast command: error is null", j.get("error") is None, str(j))

# Existing guards preserved.
resp = _TCLIENT.post("/api/terminal", data={"cmd": ""})
chk("empty cmd: HTTP 400", resp.status_code == 400, f"status={resp.status_code}")
chk("empty cmd: error message", resp.get_json().get("error") == "No command provided",
    str(resp.get_json()))

resp = _TCLIENT.post("/api/terminal", data={"cmd": "echo bad\nrm -rf /"})
chk("newline in cmd: HTTP 400", resp.status_code == 400, f"status={resp.status_code}")
chk("newline in cmd: error message",
    resp.get_json().get("error") == "Invalid characters in command", str(resp.get_json()))

resp = _TCLIENT.post("/api/terminal", data={"cmd": f"python3 -c \"print('A' * 70000)\""})
chk("large output: HTTP 200", resp.status_code == 200, f"status={resp.status_code}")
j = resp.get_json()
chk("large output: truncated to <= 65536 + marker", len(j.get("output", "")) <= 65536 + len(
    "\n... (output truncated)"), f"len={len(j.get('output', ''))}")
chk("large output: truncation marker present",
    "... (output truncated)" in j.get("output", ""), str(j)[:200])

# AC-1/AC-2: a backgrounded child that outlives the 10s timeout must NOT survive as an
# orphan — the whole process group (shell + backgrounded child) must be SIGKILL-ed, not
# just the top shell PID. Uses a real 10s wait against the endpoint's hardcoded timeout.
_pidfile = _TMP / "child.pid"
if _pidfile.exists():
    _pidfile.unlink()
_cmd = f"sleep 30 & echo $! > {_pidfile}; sleep 20"
_t0 = time.time()
resp = _TCLIENT.post("/api/terminal", data={"cmd": _cmd})
_elapsed = time.time() - _t0
chk("timeout command: HTTP 408", resp.status_code == 408, f"status={resp.status_code}")
chk("timeout command: error message",
    resp.get_json().get("error") == "Command timed out (10s limit)", str(resp.get_json()))
chk("timeout command: returns close to the 10s limit (not the shell's full 20s)",
    _elapsed < 15, f"elapsed={_elapsed:.1f}s")

_child_pid = None
for _ in range(20):
    if _pidfile.exists():
        try:
            _child_pid = int(_pidfile.read_text().strip())
        except ValueError:
            pass
        if _child_pid:
            break
    time.sleep(0.1)

if _child_pid is None:
    chk("orphan check: backgrounded child pid captured", False, "pidfile never appeared")
else:
    _alive = True
    try:
        os.kill(_child_pid, 0)
    except ProcessLookupError:
        _alive = False
    chk(
        "orphan check: backgrounded 'sleep 30' child is DEAD after the request returns (EU-146)",
        not _alive,
        f"pid {_child_pid} still alive — orphaned process leaked",
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
