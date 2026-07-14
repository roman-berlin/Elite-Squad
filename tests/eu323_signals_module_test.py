"""EU-323 regression: collect_signals/format_signals live in orchestrator/signals.py — the
mandatory prerequisite before drillmaster.py can be deleted (both functions are pure/no-LLM and
load-bearing; council/roster/adjutant all depend on them, and the launchd cockpit daemon can't
self-restart if that import chain bricks).

This harness pins:
  1. `orchestrator.signals` exists and exports both names, callable, with the same behavior
     (same dict keys from collect_signals; format_signals renders the same digest text).
  2. NOTHING in orchestrator/ imports these two names from `.drillmaster` anymore — council.py,
     roster.py, and adjutant.py must all resolve them via `.signals`.
  3. drillmaster.py no longer DEFINES the two functions (only re-imports them), so its own
     drill()/_prompt() code keeps resolving without drillmaster.py being deleted yet.
"""
import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, ".")

# Pure source-scan + string-build/import checks — never runs an agent. Stub the Agent SDK so
# importing an officer module works WITHOUT the SDK installed (as every other test does).
import types as _types
_sdk = _types.ModuleType("claude_agent_sdk")
class _SDKStub:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
_sdk.__getattr__ = lambda n: _SDKStub
sys.modules["claude_agent_sdk"] = _sdk

ROOT = Path(__file__).resolve().parent.parent
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


# === 1. orchestrator.signals exists and exports both names, callable, correct behavior ========
try:
    from orchestrator.signals import collect_signals, format_signals
    _import_ok = True
    _import_err = ""
except Exception as e:  # noqa: BLE001
    _import_ok = False
    _import_err = repr(e)
check("orchestrator.signals exists and exports collect_signals/format_signals", _import_ok, _import_err)

if _import_ok:
    check("collect_signals is callable", callable(collect_signals))
    check("format_signals is callable", callable(format_signals))

    import tempfile
    from orchestrator.config import AppConfig, Config
    _tmp = tempfile.mkdtemp()
    app = AppConfig(name="automatixy", repo_path=_tmp, base_branch="DEV",
                     protected_branch="MAIN", backlog_backend="none")
    cfg = Config(apps=[app], audit_path=str(Path(_tmp) / "audit.jsonl"), use_worktree=False)
    sig = collect_signals(cfg)
    expected_keys = {
        "tasks", "outcomes", "retried_tasks", "max_effort_hits",
        "issue_areas", "gate_fails", "needs_human", "avg_passes",
    }
    check("collect_signals(cfg) returns a dict with the expected keys",
          isinstance(sig, dict) and expected_keys.issubset(sig.keys()),
          f"got keys: {sorted(sig.keys()) if isinstance(sig, dict) else type(sig)}")

    # format_signals is a pure string-builder; exercise it directly (same fixture shape used by
    # the pre-existing officer_rename_regression_test.py) and confirm the exact expected digest.
    fixture = {
        "tasks": 5,
        "outcomes": collections.Counter({"landed": 3, "errored": 2}),
        "issue_areas": collections.Counter({"security": 4, "tests": 2}),
        "avg_passes": 1.4, "retried_tasks": 2, "max_effort_hits": 1, "gate_fails": 1, "needs_human": 0,
    }
    out = format_signals(fixture)
    expected = (
        "Tasks run: 5\n"
        "Outcomes: landed: 3, errored: 2\n"
        "Avg passes/ticket: 1.4 | retried: 2 | hit max effort: 1\n"
        "Gate failures: 1 | decisions needed: 0\n"
        "Recurring Code Reviewer issue areas: security ×4, tests ×2"
    )
    check("format_signals(fixture) renders the expected digest verbatim", out == expected, f"got: {out!r}")

# === 2. no importer resolves these names via .drillmaster anymore =============================
BAD_IMPORT = re.compile(r"from\s+\.drillmaster\s+import\s+.*(collect_signals|format_signals)")
for rel in ("orchestrator/council.py", "orchestrator/roster.py", "orchestrator/adjutant.py"):
    src = (ROOT / rel).read_text(encoding="utf-8")
    check(f"{rel}: no longer imports collect_signals/format_signals from .drillmaster",
          not BAD_IMPORT.search(src), "still imports from .drillmaster")
    check(f"{rel}: imports collect_signals/format_signals from .signals",
          re.search(r"from\s+\.signals\s+import\s+.*(collect_signals|format_signals)", src) is not None,
          "no .signals import found")

# Belt-and-suspenders: scan the WHOLE orchestrator/ tree for the retired import pattern, so a
# fourth importer nobody named can't sneak back in unnoticed.
stray = []
for py in (ROOT / "orchestrator").glob("*.py"):
    if BAD_IMPORT.search(py.read_text(encoding="utf-8")):
        stray.append(str(py.relative_to(ROOT)))
check("no file anywhere in orchestrator/ imports collect_signals/format_signals from .drillmaster",
      not stray, f"still found in: {stray}")

# === 3. drillmaster.py no longer DEFINES the two functions, but still resolves them ============
dm_src = (ROOT / "orchestrator/drillmaster.py").read_text(encoding="utf-8")
check("drillmaster.py no longer defines collect_signals", "def collect_signals" not in dm_src)
check("drillmaster.py no longer defines format_signals", "def format_signals" not in dm_src)
check("drillmaster.py imports collect_signals/format_signals from .signals (drill()/_prompt keep resolving)",
      re.search(r"from\s+\.signals\s+import\s+.*collect_signals.*format_signals|"
                r"from\s+\.signals\s+import\s+.*format_signals.*collect_signals", dm_src) is not None,
      "missing re-import — drill()/_prompt would NameError")

if _import_ok:
    from orchestrator import drillmaster
    check("drillmaster.collect_signals resolves at runtime (attribute still present)",
          callable(getattr(drillmaster, "collect_signals", None)))
    check("drillmaster.format_signals resolves at runtime (attribute still present)",
          callable(getattr(drillmaster, "format_signals", None)))

# --------------------------------------------------------------------------------------------- #
print("\n=============== EU-323 SIGNALS MODULE EXTRACTION ===============")
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
passed = sum(1 for _, ok, _ in results if ok)
print("-" * 62)
print(f"  {passed}/{len(results)} passed", "✅" if passed == len(results) else "❌")
sys.exit(0 if passed == len(results) else 1)
