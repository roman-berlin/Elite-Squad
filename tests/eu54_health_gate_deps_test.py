"""EU-54 regression: the doctor (`health.checks`) reports a "gate deps" health check.

The ticket's third fix is "Add a health check that the EU gate interpreter resolves the deps."
That health check is wired into the cockpit/CLI doctor (`orchestrator/health.checks`): for any app
that declares `gate_preflight`, the doctor must add a `<app> · gate deps` line — ok when the gate
interpreter imports the declared modules, bad (with the venv hint) when one is missing. An app with
no `gate_preflight` (a Bun product) must NOT get that line — backward compatible.

These pin the doctor wiring so it can't silently regress to never surfacing the EU self-build's most
fragile point (a venv-blind gate that dies on `import requests`).
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

from orchestrator import health
from orchestrator.config import AppConfig, Config

# The doctor skips (`continue`) any app whose repo isn't a git dir, so give it a real one. A worktree's
# own `.git` is a FILE (so it would be skipped); a hermetic temp dir with a `.git` SUBDIR passes the
# `isdir(.git)` check and keeps the test independent of the checkout layout.
ROOT = tempfile.mkdtemp()
(Path(ROOT) / ".git").mkdir()

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

PY = sys.executable

def _line(out, needle):
    for row in out:
        if row["name"].endswith(needle):
            return row
    return None

def _cfg(app):
    return Config(apps=[app], audit_path="/tmp/eu54_health.jsonl", use_worktree=False)


# --- an app whose preflight deps resolve -> a green "gate deps" line ---
ok_app = AppConfig(name="Elite-Unit", repo_path=ROOT, base_branch="dev", workdir=ROOT,
                   gate_commands=[f"{PY} tests/run_all.py"],
                   gate_preflight=["os", "sys", "json"], backlog_backend="none")
ok_out = health.checks(_cfg(ok_app))
ok_row = _line(ok_out, "· gate deps")
chk("doctor adds a 'gate deps' line for an app with gate_preflight", ok_row is not None)
chk("resolvable deps -> status ok", ok_row and ok_row["status"] == "ok",
    ok_row and f"{ok_row['status']}: {ok_row['detail']}")


# --- an app with a missing preflight dep -> a bad line carrying the venv hint ---
bad_app = AppConfig(name="Elite-Unit", repo_path=ROOT, base_branch="dev", workdir=ROOT,
                    gate_commands=[f"{PY} tests/run_all.py"],
                    gate_preflight=["os", "eu54_definitely_absent_module"], backlog_backend="none")
bad_out = health.checks(_cfg(bad_app))
bad_row = _line(bad_out, "· gate deps")
chk("missing dep -> 'gate deps' status bad", bad_row and bad_row["status"] == "bad",
    bad_row and f"{bad_row['status']}: {bad_row['detail']}")
chk("bad line surfaces the health-check failure", bad_row and "health check" in (bad_row["detail"] or "").lower(),
    bad_row and bad_row["detail"])


# --- an app WITHOUT gate_preflight (a Bun product) gets NO 'gate deps' line (backward compatible) ---
none_app = AppConfig(name="crm", repo_path=ROOT, base_branch="dev", workdir=ROOT,
                     gate_commands=["bunx tsc -b --noEmit"], backlog_backend="none")
none_out = health.checks(_cfg(none_app))
chk("no gate_preflight -> no 'gate deps' line (Bun app unaffected)", _line(none_out, "· gate deps") is None)


print("\n============== EU-54 DOCTOR 'GATE DEPS' HEALTH CHECK QA ==============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
