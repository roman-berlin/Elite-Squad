"""EU-54 regression: the EU self-host config — a venv-blind gate and a Bun setup command on a Python repo.

Proves:
  • gate.preflight_imports() is a real health check: it PASSES when the gate interpreter can import the
    declared deps and FAILS (with a clear venv hint) when one is missing — and run_gate surfaces that
    failure BEFORE running the suite, instead of a cryptic mid-run ModuleNotFoundError.
  • gate.gate_interpreter() pulls the python executable out of a gate command (and ignores a non-python
    gate like a Bun `tsc`).
  • loop._worktree_setup_command() makes worktree_setup_cmd per-app and no-ops a Node-manifest install on
    a repo with no package.json (the Elite-Unit case) — so a fresh EU worktree no longer runs a frozen
    bun install that errors every time.
"""
import sys, types, tempfile
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import gate
import orchestrator.loop as loop
from orchestrator.config import AppConfig, Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

PY = sys.executable  # a real interpreter that definitely has os/sys but not a bogus module


# ---- gate_interpreter: extract the python executable from a gate command ----
chk("gate_interpreter reads the venv python from a gate command",
    gate.gate_interpreter(["/x/.venv/bin/python tests/run_all.py"]) == "/x/.venv/bin/python",
    str(gate.gate_interpreter(["/x/.venv/bin/python tests/run_all.py"])))
chk("gate_interpreter matches a bare python3", gate.gate_interpreter(["python3 tests/run_all.py"]) == "python3")
chk("gate_interpreter ignores a non-python (Bun tsc) gate",
    gate.gate_interpreter(["cd apps/crm && bunx tsc -b --noEmit"]) is None)
chk("gate_interpreter on empty -> None", gate.gate_interpreter([]) is None)


# ---- preflight_imports: the health check itself ----
def app(preflight=None, cmds=None):
    return AppConfig(name="Elite-Unit", repo_path="/tmp", base_branch="dev", workdir="/tmp",
                     gate_commands=list(cmds or [f"{PY} tests/run_all.py"]),
                     gate_preflight=list(preflight or []), gate_timeout_sec=30)

chk("no gate_preflight -> no check (None)", gate.preflight_imports(app()) is None)

ok = gate.preflight_imports(app(preflight=["os", "sys", "json"]))
chk("all deps importable -> passes (None)", ok is None, str(ok))

bad = gate.preflight_imports(app(preflight=["os", "eu54_definitely_absent_module"]))
chk("a missing dep -> FAILED GateResult", bad is not None and not bad.passed)
chk("failure names the missing module", bad and "eu54_definitely_absent_module" in (bad.report or ""), bad and bad.report)
chk("failure carries an actionable venv hint", bad and "venv" in (bad.report or "").lower(), bad and bad.report)

# The interpreter the check uses comes from the gate command, so a bogus interpreter is itself the finding.
broken = gate.preflight_imports(app(preflight=["os"], cmds=["/no/such/python tests/run_all.py"]))
chk("an unrunnable gate interpreter -> FAILED (not a crash)", broken is not None and not broken.passed, str(broken))


# ---- run_gate runs the preflight FIRST and short-circuits on a missing dep ----
# gate_commands here would PASS ('true'), but the missing preflight dep must block before they run.
blocked = gate.run_gate(app(preflight=["eu54_definitely_absent_module"], cmds=["true"]))
chk("run_gate fails on the preflight before running the suite", not blocked.passed)
chk("run_gate's failure is the health-check report", "health check" in (blocked.report or "").lower(), blocked.report)

# With deps present the gate proceeds to (and passes) the real commands.
green = gate.run_gate(app(preflight=["os", "sys"], cmds=["true"]))
chk("run_gate passes when deps resolve and commands pass", green.passed, green.report)

# A Bun-style app (no gate_preflight) is completely unaffected — backward compatible.
chk("no-preflight app is unchanged (passes 'true')", gate.run_gate(app(cmds=["true"])).passed)


# ---- _worktree_setup_command: per-app + no-manifest guard ----
def cfg(setup=None):
    a = AppConfig(name="Elite-Unit", repo_path="/tmp", base_branch="dev", backlog_backend="none")
    c = Config(apps=[a], audit_path="/tmp/x.jsonl")
    if setup is not None:
        c.worktree_setup_cmd = setup
    return a, c

nomanifest = tempfile.mkdtemp()                          # a worktree with NO package.json (the EU case)
withmanifest = tempfile.mkdtemp()
(Path(withmanifest) / "package.json").write_text("{}")

# The unit-wide bun command must NOT run on a Python worktree (no package.json).
a, c = cfg(setup="bun install --frozen-lockfile")
chk("bun install skipped when worktree has no package.json (EU)",
    loop._worktree_setup_command(a, c, nomanifest) is None)
# ...but DOES run for an app whose worktree has a package.json (the Bun product).
chk("bun install runs when package.json is present",
    loop._worktree_setup_command(a, c, withmanifest) == "bun install --frozen-lockfile")

# A per-app override beats the unit-wide default.
a.worktree_setup_cmd = "echo hi"
chk("per-app worktree_setup_cmd overrides the unit-wide default",
    loop._worktree_setup_command(a, c, nomanifest) == "echo hi")
# An explicit empty string disables setup for this app even when a global default exists.
a.worktree_setup_cmd = ""
chk("per-app empty string disables setup (no inherit)",
    loop._worktree_setup_command(a, c, nomanifest) is None)
# No setup configured anywhere -> nothing to run.
a.worktree_setup_cmd = None
a2, c2 = cfg(setup=None)
chk("no setup command anywhere -> None", loop._worktree_setup_command(a2, c2, withmanifest) is None)


print("\n============== GATE PREFLIGHT / WORKTREE SETUP (EU-54) QA ==============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
