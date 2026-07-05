"""CLI entry smoke — `python -m orchestrator.main <cmd>` must survive _main's shared preamble.

Pins the 2026-07-05 launch crash: _main gained a module-level-import use of AuditLog at its shared
preamble (the QW4 agent-instrumentation wiring) while five subcommand branches below still did
their own inner `from .audit import AuditLog` — Python scoping made the name function-local, so
EVERY `./general <cmd>` died with UnboundLocalError before doing anything. None of the 258
harnesses drove the real CLI entry, so the suite was green while the binary was dead.

This harness runs the actual module entry as a subprocess (offline `status` command, tmp config)
— any exception in the shared preamble (arg parsing → Config.load → usage/agent wiring → dispatch)
fails it.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
subprocess.run(["git", "init", "-q", str(tmp)], check=True)   # AppConfig validates repo_path is a git repo

# The subprocess runs the REAL orchestrator.main, whose import chain pulls claude_agent_sdk at
# module level — installed in the dev venv but deliberately absent in CI. Inject a stub via
# PYTHONPATH so the smoke exercises the CLI preamble identically in both environments (PYTHONPATH
# prepends, so the stub wins even where the real SDK exists — CI parity by construction).
stubdir = tmp / "stubs"
stubdir.mkdir()
(stubdir / "claude_agent_sdk.py").write_text(
    "class _D:\n"
    "    def __init__(self, *a, **k): pass\n"
    "    def __call__(self, *a, **k): return self\n"
    "def __getattr__(name):\n"
    "    return _D\n")
env = dict(os.environ, PYTHONPATH=str(stubdir))
(tmp / "audit.jsonl").write_text(
    '{"event":"ticket_start","ticket_id":"AUTO-1","ts":"2026-06-18T10:00:00"}\n'
    '{"event":"merged","ticket_id":"AUTO-1","ts":"2026-06-18T10:05:00"}\n')
(tmp / "config.yaml").write_text(
    f'audit_path: "{tmp}/audit.jsonl"\n'
    'apps:\n'
    f'  - name: smoke\n'
    f'    repo_path: "{tmp}"\n'
    '    base_branch: "dev"\n'
    '    backlog_backend: "none"\n')

r = subprocess.run([sys.executable, "-m", "orchestrator.main",
                    "--config", str(tmp / "config.yaml"), "status"],
                   capture_output=True, text=True, timeout=120, env=env)

chk("`general status` exits 0 through the real _main preamble (no UnboundLocalError)",
    r.returncode == 0, (r.stderr or r.stdout)[-400:])
chk("no traceback on stderr", "Traceback" not in (r.stderr or ""), r.stderr[-400:])
chk("the task table rendered from the audit", "AUTO-1" in r.stdout, r.stdout[:200])

print("\n============ CLI ENTRY SMOKE ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
