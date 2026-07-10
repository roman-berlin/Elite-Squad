"""backend_pref hermeticity (2026-07-09): the sticky model-backend store must be cfg-anchored.

Live incident: the EU-190 store lived at the REPO ROOT (package-relative), so the developer's real
gitignored ``model_backend.json`` ({"backend": "glm"}) leaked into every test process. With no
GLM_AUTH_TOKEN in the test env, ``server._resolve_run_backend`` blocked every simulated run ("GLM is
selected but GLM_AUTH_TOKEN is not configured"), and 12 harnesses went red in the main tree — while
the unit's worktree-isolated gates stayed green (fresh worktrees have no gitignored pref file), so
the breakage was invisible to every land. Verified by bisect: c8874ec green in a clean worktree,
red the moment the pref file is copied in.

The fix anchors the store to ``cfg.audit_path``'s parent (state/ live; a tmp dir in tests) and
migrates the legacy root file ONLY from the live CLI entrypoint. These checks pin:

  1. active(tmp_cfg) ignores a legacy-root pref (the exact leak).
  2. set_active/get round-trip through the cfg-anchored path, not the legacy root.
  3. migrate() moves legacy → anchored once, preserves content, never clobbers an existing target.
  4. (source) server.py's set_active call sites pass cfg; main.py calls migrate().
"""
import sys, types, json, tempfile, re
from pathlib import Path

# ── Stub the Agent SDK before any orchestrator import (house convention) ──
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import backend_pref, backends
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

def _tmp_cfg(d: Path) -> Config:
    return Config(apps=[AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV",
                                  protected_branch="MAIN", backlog_backend="none")],
                  audit_path=str(d / "state" / "audit.jsonl"), use_worktree=False)

# ============ 1) the leak: a legacy-root glm pref must NOT reach a tmp-cfg reader ============ #
d = Path(tempfile.mkdtemp())
fake_root = Path(tempfile.mkdtemp())
(fake_root / "model_backend.json").write_text(json.dumps({"backend": "glm"}), encoding="utf-8")
_orig_legacy = backend_pref._legacy_file
backend_pref._legacy_file = lambda: fake_root / "model_backend.json"   # simulate the dev's real file
try:
    cfg = _tmp_cfg(d)
    chk("active(tmp_cfg) ignores a legacy-root glm pref (the 12-suite leak)",
        backend_pref.active(cfg) == backends.NATIVE, backend_pref.active(cfg))
    chk("get(tmp_cfg) is None — no pref exists in THIS config's state dir",
        backend_pref.get(cfg) is None, backend_pref.get(cfg))

    # ============ 2) round-trip through the cfg-anchored path ============ #
    backend_pref.set_active("glm", cfg)
    anchored = d / "state" / "model_backend.json"
    chk("set_active(cfg) writes into the cfg state dir", anchored.exists(), str(anchored))
    chk("active(cfg) reads back the anchored pref", backend_pref.active(cfg) == backends.GLM)
    chk("the legacy root file was NOT touched by set_active(cfg)",
        json.loads((fake_root / "model_backend.json").read_text())["backend"] == "glm")

    # ============ 3) migrate(): legacy → anchored, once, never clobbering ============ #
    d2 = Path(tempfile.mkdtemp())
    cfg2 = _tmp_cfg(d2)
    backend_pref.migrate(cfg2)
    t2 = d2 / "state" / "model_backend.json"
    chk("migrate moves the legacy file into the anchored store", t2.exists() and not (fake_root / "model_backend.json").exists())
    chk("migrate preserves the pref content", backend_pref.get(cfg2) == backends.GLM)
    backend_pref.migrate(cfg2)   # second call: legacy gone — must be a silent no-op
    chk("second migrate is a no-op", backend_pref.get(cfg2) == backends.GLM)
    (fake_root / "model_backend.json").write_text(json.dumps({"backend": "opus"}), encoding="utf-8")
    backend_pref.migrate(cfg2)   # legacy re-appeared, but target exists — must NOT clobber
    chk("migrate never clobbers an existing anchored pref", backend_pref.get(cfg2) == backends.GLM)
finally:
    backend_pref._legacy_file = _orig_legacy

# ============ 4) source pins: call sites carry cfg; the live entrypoint migrates ============ #
_server_src = Path("orchestrator/server.py").read_text(encoding="utf-8")
_naked = re.findall(r"backend_pref\.set_active\(([^)]*)\)", _server_src)
chk("every server.py set_active call passes cfg (no repo-root fallback writes)",
    _naked and all("cfg" in args for args in _naked), _naked)
_main_src = Path("orchestrator/main.py").read_text(encoding="utf-8")
chk("main.py (live CLI entrypoint) calls backend_pref.migrate(cfg)",
    "backend_pref.migrate(cfg)" in _main_src)
chk("library code never migrates (create_app must not relocate the operator's pref)",
    "backend_pref.migrate" not in _server_src)

print("\n========== BACKEND-PREF HERMETICITY QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
