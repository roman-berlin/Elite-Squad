"""EU-360 + EU-366: hang-proofing the runner and the subprocess seams.

EU-360 (tests/run_all.py):
  · every harness runs under a wall-clock timeout (_HARNESS_TIMEOUT_S / GENERAL_TEST_TIMEOUT) — one
    hung harness can no longer stall the whole suite; a timed-out harness is an unconditional FAIL;
  · the child env strips EVERY sensitive credential (is_sensitive_key: JIRA_*, TELEGRAM_*, …), not
    just TELEGRAM_*;
  · decoding is error-tolerant (a harness emitting an invalid byte doesn't crash the runner).

EU-366 (git_ops.py / loop.py / auth_probe.py):
  · git/gh subprocesses carry GIT_TERMINAL_PROMPT=0 + GCM_INTERACTIVE=never and a wall-clock timeout;
    a git timeout surfaces as GitError / a 124 code, never an unhandled hang;
  · the auth probe is single-flighted (_probe_lock) so a burst of callers runs ONE `claude -p`.
"""
import importlib.util
import os
import sys
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---- minimal SDK stub (auth_probe path is clean, but loop pulls the SDK transitively) ---- #
sdk = types.ModuleType("claude_agent_sdk")
for _n in ("AssistantMessage", "ResultMessage", "TextBlock", "ToolUseBlock", "ClaudeAgentOptions"):
    setattr(sdk, _n, type(_n, (), {"__init__": lambda self, **k: self.__dict__.update(k)}))
sdk.ProcessError = type("ProcessError", (Exception,), {})
sdk.CLINotFoundError = type("CLINotFoundError", (Exception,), {})
sys.modules.setdefault("claude_agent_sdk", sdk)

results: list[tuple[str, bool, str]] = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ================= EU-360: run_all.py hardening ================= #
os.environ["JIRA_API_TOKEN"] = "should-be-stripped"
os.environ["JIRA_EMAIL"] = "should-be-stripped"
os.environ["TELEGRAM_BOT_TOKEN"] = "should-be-stripped"
spec = importlib.util.spec_from_file_location("run_all_mod", ROOT / "tests" / "run_all.py")
ra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ra)

chk("run_all strips JIRA_API_TOKEN from the child env",
    "JIRA_API_TOKEN" not in ra._CHILD_ENV)
chk("run_all strips JIRA_EMAIL from the child env", "JIRA_EMAIL" not in ra._CHILD_ENV)
chk("run_all strips TELEGRAM_BOT_TOKEN from the child env", "TELEGRAM_BOT_TOKEN" not in ra._CHILD_ENV)
chk("run_all keeps GENERAL_AUTH_PROBE=0", ra._CHILD_ENV.get("GENERAL_AUTH_PROBE") == "0")
chk("run_all has a per-harness timeout", isinstance(ra._HARNESS_TIMEOUT_S, int) and ra._HARNESS_TIMEOUT_S > 0)

# a genuinely hung harness must be killed and reported as timed_out, not hang the runner
_hang = ROOT / "tests" / "_eu360_hang_fixture.py"
# flush before the hang: block-buffered stdout to a pipe would otherwise be lost on SIGKILL, so this
# tests the honest guarantee — output the child actually FLUSHED before wedging is still captured.
_hang.write_text("import time\nprint('starting hang', flush=True)\ntime.sleep(120)\n", encoding="utf-8")
try:
    os.environ["GENERAL_TEST_TIMEOUT"] = "2"
    # reload so the fixture picks up the 2s override
    ra2 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ra2)
    t0 = time.time()
    stdout, stderr, rc, timed_out = ra2._run_harness(_hang)
    elapsed = time.time() - t0
    chk("a hung harness is killed by the timeout", timed_out, f"timed_out={timed_out}")
    chk("the runner returns promptly (does not hang)", elapsed < 30, f"{elapsed:.1f}s")
    chk("partial output before the hang is preserved", "starting hang" in stdout, repr(stdout[:80]))
finally:
    os.environ.pop("GENERAL_TEST_TIMEOUT", None)
    _hang.unlink(missing_ok=True)

# error-tolerant decoding: a harness emitting a raw invalid byte must not crash the runner
_bad = ROOT / "tests" / "_eu360_badbyte_fixture.py"
_bad.write_text(
    "import sys\nsys.stdout.buffer.write(b'ok \\xff done\\n')\nsys.stdout.flush()\n", encoding="utf-8")
try:
    stdout, stderr, rc, timed_out = ra._run_harness(_bad)
    chk("invalid-byte output does not crash the runner", rc == 0 and not timed_out, f"rc={rc}")
    chk("invalid byte is replaced, output still captured", "ok" in stdout and "done" in stdout, repr(stdout[:80]))
finally:
    _bad.unlink(missing_ok=True)

# ================= EU-366: git_ops env + timeout ================= #
from orchestrator import git_ops  # noqa: E402

env = git_ops._git_env()
chk("git env disables terminal credential prompting", env.get("GIT_TERMINAL_PROMPT") == "0")
chk("git env disables the credential-manager GUI", env.get("GCM_INTERACTIVE") == "never")
chk("git env still inherits PATH", "PATH" in env)
chk("git has a wall-clock timeout constant", isinstance(git_ops._GIT_TIMEOUT_S, int) and git_ops._GIT_TIMEOUT_S > 0)

# _git raises GitError (not TimeoutExpired) when the git subprocess times out
import subprocess as _sp  # noqa: E402


class _FakeGit:
    """Just enough of Git to exercise the low-level helpers."""
    def __init__(self):
        self.repo = ROOT
        self.main = ROOT


_orig_run = git_ops.subprocess.run


def _timeout_run(*a, **k):
    raise _sp.TimeoutExpired(cmd=a[0] if a else "git", timeout=k.get("timeout", 1))


try:
    git_ops.subprocess.run = _timeout_run
    raised = False
    try:
        git_ops.Git._git(_FakeGit(), ROOT, "fetch", "origin", "dev")
    except git_ops.GitError:
        raised = True
    chk("a git timeout surfaces as GitError, never an unhandled TimeoutExpired", raised)
    code, out, err = git_ops.Git._run_code(_FakeGit(), "push", "origin", "dev")
    chk("_run_code returns a non-zero (124) code on timeout, never hangs/raises", code == 124, str(code))
    chk("the timeout code carries an explanatory message", "timed out" in err, err[:80])
finally:
    git_ops.subprocess.run = _orig_run

# ================= EU-366: auth_probe single-flight ================= #
from orchestrator import auth_probe  # noqa: E402

chk("auth_probe has a single-flight lock", isinstance(auth_probe._probe_lock, type(threading.Lock())))

# with the cache cold and probing slow, N concurrent probe() calls must run the probe ONCE
os.environ.pop("GENERAL_AUTH_PROBE", None)   # ensure the probe is not disabled for this check
auth_probe.invalidate()
probe_calls = {"n": 0}
_orig_probe = auth_probe._run_probe


def _slow_probe(timeout=0):
    probe_calls["n"] += 1
    time.sleep(0.3)
    return 0, "ok"


try:
    auth_probe._run_probe = _slow_probe
    threads = [threading.Thread(target=lambda: auth_probe.probe(max_age_s=999)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    chk("8 concurrent probes run the subprocess exactly once (single-flight)",
        probe_calls["n"] == 1, f"ran {probe_calls['n']} probes")
finally:
    auth_probe._run_probe = _orig_probe
    auth_probe.invalidate()

# ================= tally ================= #
print("\n============ EU-360 + EU-366 HANG-PROOFING QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
