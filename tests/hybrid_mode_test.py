"""HYBRID MODE (2026-07-19, Commander order): the Main model does the heavy thinking —
Planner/Architect PRD, Reviewer judgment, PM/debug investigation — and the Secondary model does
the regular building against that plan.

Pins:
  (1) backend_pref.set_hybrid_mode/get_hybrid persist + clear the flag;
  (2) backends.current_for_tag: hybrid pinned → the 'builder' tag routes to the secondary while
      planner/reviewer/pm/blank tags stay on the run's main backend;
  (3) hybrid NOT pinned → every tag uses the main backend (single mode, byte-identical);
  (4) the SDK seam resolves per-tag (source pins: apply(..., backend=current_for_tag(tag)) and
      the GLM routing bypass keyed on current_for_tag);
  (5) loop.run pins the hybrid secondary run-scoped (set_hybrid) and resets it in finally,
      auditing hybrid_mode — and skips the pin when secondary == main or hybrid is off;
  (6) POST /api/model mode=hybrid arms it (refused without a secondary); mode=single disarms;
      clearing the secondary drops hybrid too;
  (7) the toolbar renders the Mode select (disabled until a Secondary exists, with the
      plan-on-main/build-on-secondary note when armed)."""
import sys
import tempfile
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import backend_pref, backends
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev",
                             protected_branch="main", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)

# ── (1) pref round-trip ──
chk("(1a) hybrid off by default", backend_pref.get_hybrid(cfg) is False)
backend_pref.set_hybrid_mode(True, cfg)
chk("(1b) hybrid persists", backend_pref.get_hybrid(cfg) is True)
backend_pref.set_hybrid_mode(False, cfg)
chk("(1c) hybrid clears", backend_pref.get_hybrid(cfg) is False)

# ── (2)+(3) per-tag routing ──
bk_tok = backends.set_backend("opus")
hy_tok = backends.set_hybrid("glm")
try:
    chk("(2a) builder tag routes to the secondary", backends.current_for_tag("builder") == "glm")
    chk("(2b) planner stays on main", backends.current_for_tag("planner") == "opus")
    chk("(2c) reviewer stays on main", backends.current_for_tag("reviewer") == "opus")
    chk("(2d) pm stays on main", backends.current_for_tag("pm") == "opus")
    chk("(2e) blank tag stays on main", backends.current_for_tag("") == "opus")
finally:
    backends.reset_hybrid(hy_tok)
chk("(3) hybrid unpinned → builder uses main", backends.current_for_tag("builder") == "opus")
backends.reset_backend(bk_tok)

# ── (4) the seam resolves per-tag (source pins) ──
agent_src = Path("orchestrator/agent.py").read_text(encoding="utf-8")
chk("(4a) apply() is fed the per-tag backend at the SDK seam",
    "apply(options, backend=_backends.current_for_tag(tag))" in agent_src)
chk("(4b) the GLM tier-routing bypass keys on the per-tag backend",
    "current_for_tag(tag) == _backends.GLM" in agent_src)

# ── (5) loop pins run-scoped (source pins) ──
loop_src = Path("orchestrator/loop.py").read_text(encoding="utf-8")
chk("(5a) loop.run pins the hybrid secondary", "backends.set_hybrid(_hy_sec)" in loop_src)
chk("(5b) …audits hybrid_mode", '"hybrid_mode"' in loop_src)
chk("(5c) …resets in finally", "backends.reset_hybrid(_hy_token)" in loop_src)
chk("(5d) …skips the pin when secondary == main",
    '_hy_sec != getattr(cfg, "model_backend"' in loop_src)

# ── (6) /api/model mode= ──
from orchestrator import server
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()
backend_pref.set_secondary(None, cfg)
client.post("/api/model", data={"mode": "hybrid"})
chk("(6a) hybrid refused without a secondary", backend_pref.get_hybrid(cfg) is False)
backend_pref.set_secondary("glm", cfg)
client.post("/api/model", data={"mode": "hybrid"})
chk("(6b) hybrid arms with a secondary", backend_pref.get_hybrid(cfg) is True)
client.post("/api/model", data={"mode": "single"})
chk("(6c) single disarms", backend_pref.get_hybrid(cfg) is False)
client.post("/api/model", data={"mode": "hybrid"})
client.post("/api/model", data={"secondary": "none"})
chk("(6d) clearing the secondary drops hybrid too",
    backend_pref.get_hybrid(cfg) is False and backend_pref.get_secondary(cfg) is None)

# ── (7) toolbar ──
from orchestrator import cockpit_views
bar_no_sec = cockpit_views.backend_control(cfg, "automatixy")
chk("(7a) Mode select renders, disabled without a Secondary",
    "name=mode" in bar_no_sec and "disabled" in bar_no_sec.split("name=mode")[1][:200])
backend_pref.set_secondary("glm", cfg)
backend_pref.set_hybrid_mode(True, cfg)
bar_hy = cockpit_views.backend_control(cfg, "automatixy")
chk("(7b) armed hybrid shows the plan-on-main note",
    "plan on main" in bar_hy and "value='hybrid' selected" in bar_hy)

print("\n========== HYBRID MODE QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
