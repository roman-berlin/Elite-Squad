"""EU-452 post-merge dev-HEAD re-verification runner QA.

The engine's full behaviour surface: should_run gating (master + per-app + gate_commands — inert
unless armed both ways), the green/red verdict mapping, the WRONG-TREE guard (a sha mismatch is a
green-skip that NEVER runs the suite — the #1 false-red-base escalation class), and the never-raises /
best-effort contract (a runner or git hiccup returns a tuple instead of corrupting a land that already
succeeded). Contrast with sentinel (which reverts on red) and smoke (which flags on red): this module
has NO revert responsibility, so on any doubt it leans GREEN-skip, never red, never raises.
"""
import inspect
import sys
import types

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import postmerge_verify as pmv
from orchestrator.config import Config
from orchestrator.contracts import GateResult
from orchestrator import gate

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

ns = types.SimpleNamespace


class Audit:
    def __init__(s): s.events = []
    def record(s, kind, **kw): s.events.append((kind, kw))
    def kinds(s): return [k for k, _ in s.events]


def app(gate_cmds, app_flag=True):
    """A bare app namespace with the attrs the gate runner reads (workdir/repo_path/timeout/env)."""
    return ns(name="eu", base_branch="dev", gate_commands=list(gate_cmds),
              postmerge_verify=app_flag, workdir="/tmp", repo_path="/tmp",
              gate_timeout_sec=30, gate_env={})


class FakeGit:
    """current_sha() returns the sha it was built with; never touches the real repo."""
    def __init__(s, sha): s._sha = sha
    def current_sha(s): return s._sha


class BoomGit:
    """current_sha() raises — proves a git hiccup never propagates out of verify()."""
    def current_sha(s): raise RuntimeError("git rev-parse exploded")


MERGE = "abc123def4567890abcdef1234567890abcdef12"

cfg_on = Config(apps=[], audit_path="/tmp/x.jsonl", postmerge_verify=True)
cfg_off = Config(apps=[], audit_path="/tmp/x.jsonl", postmerge_verify=False)

# call sentinel: which app/gate_commands run_commands was last invoked with
_calls = []


def _capture(app_arg, cmds_arg):
    _calls.append((app_arg, list(cmds_arg)))


# ============================================================================ #
# AC#1 — should_run gating (master AND per-app AND gate_commands)
# ============================================================================ #
chk("should_run OFF when master flag off (gate+app armed)",
    not pmv.should_run(cfg_off, app(["run suite"], app_flag=True)))
chk("should_run OFF when no gate_commands (master+app armed)",
    not pmv.should_run(cfg_on, app([], app_flag=True)))
chk("should_run OFF when per-app flag off (master on + gate armed)",
    not pmv.should_run(cfg_on, app(["run suite"], app_flag=False)))
chk("should_run ON only when master+app+gate all armed",
    pmv.should_run(cfg_on, app(["run suite"], app_flag=True)))


# ============================================================================ #
# AC#2 — green verdict: sha matched, run_commands passed -> True + green audit + invoked
# ============================================================================ #
_calls.clear()
def _green(a, c):
    _capture(a, c)
    return GateResult(passed=True, report="all commands passed")
pmv.gate.run_commands = _green
au = Audit()
ok, note = pmv.verify(cfg_on, app(["run suite"], app_flag=True), FakeGit(MERGE), MERGE, au)
chk("green -> first element True", ok is True)
chk("green -> note says green", "green" in note.lower())
chk("green -> audit postmerge_verify_green recorded",
    "postmerge_verify_green" in au.kinds())
chk("green -> run_commands WAS invoked", len(_calls) == 1, str(_calls))


# ============================================================================ #
# AC#3 — red verdict NAMES the failing harness (evidence == extract_failure_evidence,
#         not a naive report head slice that would silently drop it under green-suite noise)
# ============================================================================ #
_calls.clear()
# A report >2500 chars whose failure signal sits AFTER a large block of green noise, so a naive
# report[:N] head slice contains NO failure signal while extract_failure_evidence keeps it.
red_report = ("green suite passed\n" * 250)          # ~4400 chars, no failure signal
red_report += "============\n"                        # run_all-style summary banner (pure '=')
red_report += "HARNESSES: 5 / FAILED: 1 / ALL RED\n"
red_report += "FAILED eu452_harness - AssertionError: post-merge broke\n"
expected_ev = gate.extract_failure_evidence(red_report)
pmv.gate.run_commands = lambda a, c: (_capture(a, c), GateResult(passed=False, report=red_report))[1]
au2 = Audit()
ok2, ev = pmv.verify(cfg_on, app(["run suite"], app_flag=True), FakeGit(MERGE), MERGE, au2)
chk("red -> first element False", ok2 is False)
chk("red -> evidence EQUALS gate.extract_failure_evidence(report)", ev == expected_ev)
chk("red -> evidence carries the failing-harness signal", "FAILED" in ev and "eu452_harness" in ev)
chk("red -> NOT a naive head slice (head has no failure signal)",
    "FAILED" not in red_report[:2500])
chk("red -> run_commands WAS invoked", len(_calls) == 1, str(_calls))


# ============================================================================ #
# AC#4 — sha mismatch is a GREEN-SKIP that never runs the suite
# ============================================================================ #
_calls.clear()
suite_ran = {"v": False}
def _must_not_run(a, c):
    _capture(a, c)
    suite_ran["v"] = True
    return GateResult(passed=True, report="SHOULD NOT RUN")
pmv.gate.run_commands = _must_not_run
au4 = Audit()
ok4, note4 = pmv.verify(cfg_on, app(["run suite"], app_flag=True),
                        FakeGit("different_sha_999"), MERGE, au4)
chk("sha-mismatch -> first element True (green-skip)", ok4 is True)
chk("sha-mismatch -> exact skip note", note4 == "dev HEAD ≠ merge_sha; skipped", repr(note4))
chk("sha-mismatch -> audit kind is exactly postmerge_verify_skip",
    au4.kinds() == ["postmerge_verify_skip"], str(au4.kinds()))
chk("sha-mismatch -> suite NEVER ran (run_commands not called)",
    len(_calls) == 0 and not suite_ran["v"], str(_calls))

# A blank sha from a safe-failing git (current_sha -> '') is the same wrong-tree green-skip.
_calls.clear(); suite_ran["v"] = False
au4b = Audit()
ok4b, note4b = pmv.verify(cfg_on, app(["run suite"], app_flag=True), FakeGit(""), MERGE, au4b)
chk("blank sha -> green-skip too", ok4b is True and note4b == "dev HEAD ≠ merge_sha; skipped")
chk("blank sha -> suite NEVER ran", len(_calls) == 0 and not suite_ran["v"])


# ============================================================================ #
# AC#5 — verify NEVER raises (runner exception AND git exception both -> tuple, not propagation)
# ============================================================================ #
def _boom(a, c):
    _capture(a, c)
    raise RuntimeError("runner exploded mid-suite")
pmv.gate.run_commands = _boom
def _no_raise(fn):
    try:
        out = fn()
    except Exception as exc:  # noqa: BLE001 — the WHOLE point is that it must not raise
        return (False, f"raised {exc!r}")
    ok_flag = (isinstance(out, tuple) and len(out) == 2
               and isinstance(out[0], bool) and isinstance(out[1], str))
    return (ok_flag, repr(out))

ok_runner, det_runner = _no_raise(lambda: pmv.verify(
    cfg_on, app(["run suite"], app_flag=True), FakeGit(MERGE), MERGE, Audit()))
chk("run_commands raises -> returns (bool,str), never propagates", ok_runner, det_runner)
# a runner hiccup is a GREEN-SKIP (no revert responsibility, so it leans green on doubt)
_runner_out = pmv.verify(cfg_on, app(["run suite"], app_flag=True), FakeGit(MERGE), MERGE, Audit())
chk("run_commands raises -> green-skip", _runner_out[0] is True and "skipped" in _runner_out[1].lower())

ok_git, det_git = _no_raise(lambda: pmv.verify(
    cfg_on, app(["run suite"], app_flag=True), BoomGit(), MERGE, Audit()))
chk("current_sha raises -> returns (bool,str), never propagates", ok_git, det_git)


# ============================================================================ #
# Bonus — EU-355 isolation env: run_commands sees redirected GENERAL_AUDIT_PATH / GENERAL_PID_FILE,
#         restored (or popped) after verify returns. (The unit tests stub run_commands, so the live
#         subprocess path is covered by code + docstring; this proves the env IS actually wired.)
# ============================================================================ #
_calls.clear()
import os
seen_env = {}
def _capture_env(a, c):
    _capture(a, c)
    seen_env["audit"] = os.environ.get("GENERAL_AUDIT_PATH")
    seen_env["pid"] = os.environ.get("GENERAL_PID_FILE")
    return GateResult(passed=True, report="green")
pmv.gate.run_commands = _capture_env
had_audit = "GENERAL_AUDIT_PATH" in os.environ
had_pid = "GENERAL_PID_FILE" in os.environ
pmv.verify(cfg_on, app(["run suite"], app_flag=True), FakeGit(MERGE), MERGE, Audit())
chk("isolation: run_commands saw a redirected GENERAL_AUDIT_PATH (temp prefix)",
    seen_env.get("audit") is not None and "postmerge_verify_" in (seen_env.get("audit") or ""),
    str(seen_env))
chk("isolation: run_commands saw a redirected GENERAL_PID_FILE (temp prefix)",
    seen_env.get("pid") is not None and "postmerge_verify_" in (seen_env.get("pid") or ""),
    str(seen_env))
chk("isolation: GENERAL_AUDIT_PATH restored after verify",
    ("GENERAL_AUDIT_PATH" in os.environ) == had_audit)
chk("isolation: GENERAL_PID_FILE restored after verify",
    ("GENERAL_PID_FILE" in os.environ) == had_pid)


# ============================================================================ #
# AC#6 — docstrings carry the WHY (pre- vs post-merge, EU-355 isolation env, best-effort/never-raises)
# ============================================================================ #
for _name, _fn in (("should_run", pmv.should_run), ("verify", pmv.verify)):
    _doc = _fn.__doc__ or ""
    chk(f"{_name} has a docstring", bool(_doc))
    chk(f"{_name} docstring states post- vs pre-merge distinction",
        "post-merge" in _doc.lower() and ("pre-land" in _doc.lower() or "base_gate" in _doc.lower()))
    if _name == "verify":
        chk(f"{_name} docstring states the EU-355 isolation env",
            "isolation" in _doc.lower() or "general_audit_path" in _doc.lower()
            or "general_pid_file" in _doc.lower())
        chk(f"{_name} docstring states the best-effort / never-raises contract",
            "never" in _doc.lower() and "raise" in _doc.lower())

# verify's signature matches the ticket exactly: (cfg, app, git, merge_sha, audit=None).
_params = list(inspect.signature(pmv.verify).parameters)
chk("verify signature == (cfg, app, git, merge_sha, audit=None)",
    _params == ["cfg", "app", "git", "merge_sha", "audit"], str(_params))


print("\n================= EU-452 POST-MERGE VERIFY QA =================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
