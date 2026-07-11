"""EU-187 — POST /api/terminal must never execute free-form shell.

Covers the ticket's four testable acceptance criteria:
  1. an un-allowlisted base command -> HTTP 403, command not executed
  2. a shell metacharacter / chaining attempt -> HTTP 403, no shell interpretation occurs
  3. an allowlisted command -> HTTP 200 with stdout/stderr in `output`, executed via shell=False
  4. existing guards remain: empty cmd -> 400; embedded NUL/newline -> 400; output >64KB truncated;
     10s timeout -> 408
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

# ── minimal SDK / requests stubs so the orchestrator can import without real deps ──
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self


sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)

req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules.setdefault("requests", req)

sys.path.insert(0, ".")

from orchestrator import server
from orchestrator.config import AppConfig, Config

# ── shared config / Flask test client ───────────────────────────────────────────
_TMP = Path(tempfile.mkdtemp())
_AUDIT = _TMP / "audit.jsonl"
_CFG = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(_TMP), base_branch="DEV",
                     protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(_AUDIT),
    use_worktree=False,
)
_CFG.detected_auth = lambda: "test"

_APP = server.create_app(_CFG)
_CLIENT = _APP.test_client()

# ── result accumulator ──────────────────────────────────────────────────────────
results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


# =============================================================================
# (1) un-allowlisted base command -> 403, never reaches subprocess
# =============================================================================

def _test_unallowlisted_command_rejected() -> None:
    with mock.patch("subprocess.run") as run_mock:
        resp = _CLIENT.post("/api/terminal", data={"cmd": "rm -rf /tmp/x"})
    chk("unallowlisted: HTTP 403", resp.status_code == 403, f"status={resp.status_code}")
    j = json.loads(resp.data)
    chk("unallowlisted: error names rejection", bool(j.get("error")), str(j))
    chk("unallowlisted: subprocess never invoked", not run_mock.called, str(run_mock.call_args))


_test_unallowlisted_command_rejected()

# =============================================================================
# (2) shell metacharacters / chaining -> 403, no shell interpretation
# =============================================================================

def _test_metacharacter_commands_rejected() -> None:
    for bad_cmd in ["ls; whoami", "echo hi | cat", "cat $(id)", "git status && rm -rf /",
                    "echo `id`", "ls > /tmp/out", "ls < /tmp/in", "echo *"]:
        with mock.patch("subprocess.run") as run_mock:
            resp = _CLIENT.post("/api/terminal", data={"cmd": bad_cmd})
        chk(f"metachar '{bad_cmd}': HTTP 403", resp.status_code == 403,
            f"status={resp.status_code}")
        chk(f"metachar '{bad_cmd}': subprocess never invoked", not run_mock.called, bad_cmd)


_test_metacharacter_commands_rejected()

# =============================================================================
# (2b) INTERPRETER-based RCE paths -> 403, never reaches subprocess (iteration-2)
#      Interpreters (python/python3/bun/node/…) are equivalent to arbitrary code execution and
#      MUST NOT be on a 'vetted no-shell' allowlist. Each of these must be rejected before running.
# =============================================================================

def _test_interpreter_rce_paths_rejected() -> None:
    rce_cmds = [
        "python3 -m http.server",
        "python3 -m pip install x",
        "python3 /tmp/x.py",
        "python -c import os",
        "bun x pkg",
        "node -e 1",
        "sh -c id",
        "bash -c id",
    ]
    for bad_cmd in rce_cmds:
        with mock.patch("subprocess.run") as run_mock:
            resp = _CLIENT.post("/api/terminal", data={"cmd": bad_cmd})
        chk(f"interpreter '{bad_cmd}': HTTP 403", resp.status_code == 403,
            f"status={resp.status_code}")
        chk(f"interpreter '{bad_cmd}': subprocess never invoked", not run_mock.called,
            str(run_mock.call_args))


def _test_interpreters_absent_from_allowlist() -> None:
    for interp in ("python", "python3", "bun", "node", "sh", "bash"):
        chk(f"allowlist excludes '{interp}'", interp not in server.TERMINAL_ALLOWED_COMMANDS,
            sorted(server.TERMINAL_ALLOWED_COMMANDS))


_test_interpreter_rce_paths_rejected()
_test_interpreters_absent_from_allowlist()

# =============================================================================
# (2c) file-reading commands are confined to the working directory -> 403 for out-of-tree paths
#      so cat/tail/grep cannot exfiltrate arbitrary absolute paths / secrets.
# =============================================================================

def _test_file_read_confined_to_cwd() -> None:
    out_of_tree = [
        "cat /etc/passwd",
        "tail /etc/hosts",
        "grep secret /etc/shadow",
        "cat ../../../../etc/passwd",
        "head ~/.ssh/id_rsa",
    ]
    for bad_cmd in out_of_tree:
        with mock.patch("subprocess.run") as run_mock:
            resp = _CLIENT.post("/api/terminal", data={"cmd": bad_cmd})
        chk(f"confined '{bad_cmd}': HTTP 403", resp.status_code == 403,
            f"status={resp.status_code}")
        chk(f"confined '{bad_cmd}': subprocess never invoked", not run_mock.called,
            str(run_mock.call_args))


def _test_file_read_inside_cwd_allowed() -> None:
    # A relative path that stays inside the working directory is fine (reaches subprocess).
    fake_result = types.SimpleNamespace(stdout="hello\n", stderr="")
    with mock.patch("subprocess.run", return_value=fake_result) as run_mock:
        resp = _CLIENT.post("/api/terminal", data={"cmd": "cat notes.txt"})
    chk("in-cwd cat: HTTP 200", resp.status_code == 200, f"status={resp.status_code}")
    chk("in-cwd cat: subprocess invoked", run_mock.called, "")


_test_file_read_confined_to_cwd()
_test_file_read_inside_cwd_allowed()

# =============================================================================
# (3) allowlisted command -> 200, executed via shell=False argv list
# =============================================================================

def _test_allowlisted_command_executes_without_shell() -> None:
    fake_result = types.SimpleNamespace(stdout="On branch DEV\n", stderr="")
    with mock.patch("subprocess.run", return_value=fake_result) as run_mock:
        resp = _CLIENT.post("/api/terminal", data={"cmd": "git status"})
    chk("allowlisted: HTTP 200", resp.status_code == 200, f"status={resp.status_code}")
    j = json.loads(resp.data)
    chk("allowlisted: output present", "On branch DEV" in j.get("output", ""), str(j))
    chk("allowlisted: subprocess invoked", run_mock.called, "")
    args, kwargs = run_mock.call_args
    chk("allowlisted: argv list (not string)", isinstance(args[0], list) and args[0] == ["git", "status"],
        str(args))
    chk("allowlisted: shell=False", kwargs.get("shell") is False, str(kwargs))


_test_allowlisted_command_executes_without_shell()


def _test_pwd_allowlisted() -> None:
    fake_result = types.SimpleNamespace(stdout="/tmp\n", stderr="")
    with mock.patch("subprocess.run", return_value=fake_result) as run_mock:
        resp = _CLIENT.post("/api/terminal", data={"cmd": "pwd"})
    chk("pwd: HTTP 200", resp.status_code == 200, f"status={resp.status_code}")
    args, kwargs = run_mock.call_args
    chk("pwd: argv == ['pwd']", args[0] == ["pwd"], str(args))


_test_pwd_allowlisted()

# =============================================================================
# (4) existing guards remain
# =============================================================================

def _test_empty_cmd_rejected() -> None:
    resp = _CLIENT.post("/api/terminal", data={"cmd": ""})
    chk("empty cmd: HTTP 400", resp.status_code == 400, f"status={resp.status_code}")


def _test_nul_newline_rejected() -> None:
    resp = _CLIENT.post("/api/terminal", data={"cmd": "git status\nwhoami"})
    chk("embedded newline: HTTP 400", resp.status_code == 400, f"status={resp.status_code}")

    resp2 = _CLIENT.post("/api/terminal", data={"cmd": "git status\x00whoami"})
    chk("embedded NUL: HTTP 400", resp2.status_code == 400, f"status={resp2.status_code}")


def _test_output_truncated_at_64kb() -> None:
    big = "a" * 70000
    fake_result = types.SimpleNamespace(stdout=big, stderr="")
    with mock.patch("subprocess.run", return_value=fake_result):
        resp = _CLIENT.post("/api/terminal", data={"cmd": "git status"})
    j = json.loads(resp.data)
    chk("output truncated: <= 65536 + marker", len(j.get("output", "")) <= 65536 + 32, str(len(j.get("output", ""))))
    chk("output truncated: marker present", "truncated" in j.get("output", ""), str(j)[:200])


def _test_timeout_returns_408() -> None:
    import subprocess as _sp
    with mock.patch("subprocess.run", side_effect=_sp.TimeoutExpired(cmd="git", timeout=10)):
        resp = _CLIENT.post("/api/terminal", data={"cmd": "git status"})
    chk("timeout: HTTP 408", resp.status_code == 408, f"status={resp.status_code}")


_test_empty_cmd_rejected()
_test_nul_newline_rejected()
_test_output_truncated_at_64kb()
_test_timeout_returns_408()

# =============================================================================
# Summary
# =============================================================================
passed_n = sum(1 for _, ok, _ in results if ok)
print(f"\n========= EU-187 terminal allowlist tests =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("------------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed_n == len(results) else f"{len(results) - passed_n} FAIL ❌")
sys.exit(0 if passed_n == len(results) else 1)
