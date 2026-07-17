"""EU-331 regression: snapshot_doctrine() lives in orchestrator/doctrine.py — the mandatory
prerequisite (alongside EU-323's signals.py extraction) before drillmaster.py can be deleted
(EU-327). It is the reversibility guard every applied doctrine change (officer files / squad
agents) relies on, so it must survive independently of the LLM drill/apply code that remains
in drillmaster.py.

This harness pins:
  1. `orchestrator.doctrine` exists and exports `snapshot_doctrine`, callable, with the same
     behavior (backs up officers/ into a timestamped backups/doctrine-* dir next to audit_path).
  2. drillmaster.py no longer DEFINES snapshot_doctrine (only re-imports it), so its own
     apply() code keeps resolving without drillmaster.py being deleted yet.
  3. NOTHING in orchestrator/ imports snapshot_doctrine from `.drillmaster` (belt-and-suspenders
     whole-tree scan) — the surviving import path is `.doctrine`.
  4. `grep -r drillmaster orchestrator/` (the EU-331 acceptance check) has no import references
     left anywhere in the tree.
"""
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

# Pure source-scan + import checks — never runs an agent. Stub the Agent SDK so importing an
# officer module works WITHOUT the SDK installed (as every other test does).
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


# === 1. orchestrator.doctrine exists and exports snapshot_doctrine, callable, correct behavior ==
try:
    from orchestrator.doctrine import snapshot_doctrine
    _import_ok = True
    _import_err = ""
except Exception as e:  # noqa: BLE001
    _import_ok = False
    _import_err = repr(e)
check("orchestrator.doctrine exists and exports snapshot_doctrine", _import_ok, _import_err)

if _import_ok:
    check("snapshot_doctrine is callable", callable(snapshot_doctrine))

    from orchestrator.config import AppConfig, Config
    _tmp = tempfile.mkdtemp()
    # A fake officers/ dir at the repo root the function walks (Path(__file__)/../.. from the
    # module) already exists for real in this checkout, so just exercise it against the real tree.
    app = AppConfig(name="automatixy", repo_path=_tmp, base_branch="DEV",
                     protected_branch="MAIN", backlog_backend="none")
    cfg = Config(apps=[app], audit_path=str(Path(_tmp) / "audit.jsonl"), use_worktree=False)
    dest = snapshot_doctrine(cfg)
    check("snapshot_doctrine(cfg) returns a Path that exists", isinstance(dest, Path) and dest.exists(),
          f"got: {dest!r}")
    check("snapshot_doctrine backs up officers/ into <dest>/officers",
          (dest / "officers").exists() and any((dest / "officers").iterdir()),
          f"dest contents: {list(dest.iterdir()) if dest.exists() else '(missing)'}")
    check("the backup lands under backups/doctrine-* next to audit_path",
          dest.parent.name == "backups" and dest.name.startswith("doctrine-"),
          f"got: {dest}")

# === 2. drillmaster.py no longer DEFINES snapshot_doctrine, but still resolves it ===============
dm_src = (ROOT / "orchestrator/drillmaster.py").read_text(encoding="utf-8")
check("drillmaster.py no longer defines snapshot_doctrine", "def snapshot_doctrine" not in dm_src)
check("drillmaster.py imports snapshot_doctrine from .doctrine (apply() keeps resolving)",
      re.search(r"from\s+\.doctrine\s+import\s+.*snapshot_doctrine", dm_src) is not None,
      "missing re-import — apply() would NameError")

if _import_ok:
    from orchestrator import drillmaster
    check("drillmaster.snapshot_doctrine resolves at runtime (attribute still present)",
          callable(getattr(drillmaster, "snapshot_doctrine", None)))

# === 3. no importer anywhere resolves snapshot_doctrine via .drillmaster anymore ================
BAD_IMPORT = re.compile(r"from\s+\.drillmaster\s+import\s+.*snapshot_doctrine")
stray = []
for py in (ROOT / "orchestrator").glob("*.py"):
    if BAD_IMPORT.search(py.read_text(encoding="utf-8")):
        stray.append(str(py.relative_to(ROOT)))
check("no file anywhere in orchestrator/ imports snapshot_doctrine from .drillmaster",
      not stray, f"still found in: {stray}")

# === 4. EU-331 AC(3): grep -r drillmaster orchestrator/ has no IMPORT references left ============
IMPORT_PATTERN = re.compile(r"^\s*(from\s+\.drillmaster\s+import|import\s+.*\bdrillmaster\b)", re.M)
bad_importers = []
for py in (ROOT / "orchestrator").glob("*.py"):
    if py.name == "drillmaster.py":
        continue
    if IMPORT_PATTERN.search(py.read_text(encoding="utf-8")):
        bad_importers.append(str(py.relative_to(ROOT)))
check("no module in orchestrator/ (other than drillmaster.py itself) imports drillmaster",
      not bad_importers, f"still found in: {bad_importers}")

# --------------------------------------------------------------------------------------------- #
print("\n=============== EU-331 DOCTRINE MODULE EXTRACTION ===============")
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
passed = sum(1 for _, ok, _ in results if ok)
print("-" * 62)
print(f"  {passed}/{len(results)} passed", "✅" if passed == len(results) else "❌")
sys.exit(0 if passed == len(results) else 1)
