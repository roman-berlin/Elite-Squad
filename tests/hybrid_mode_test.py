"""HYBRID MODE (2026-07-19, Commander order): the Main model does the heavy thinking —
Planner/Architect PRD, Reviewer judgment, PM/debug investigation — and the Secondary model does
the regular building against that plan.

Pins (2026-07-19c: with a Secondary configured the Commander chooses HOW the two models work —
'hybrid' (both per task) or 'backup' (Secondary only when the Main hits its limit); the
fallback path (resolve_for_run) is live in BOTH modes; per-tag routing only in hybrid):
  (1) get_mode defaults to 'hybrid'; set_mode persists 'backup'; get_hybrid = secondary AND
      mode=='hybrid' — in backup mode per-tag routing stays off;
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

# ── (1) the hybrid/backup mode ──
chk("(1a) no secondary → hybrid off", backend_pref.get_hybrid(cfg) is False)
backend_pref.set_secondary("glm", cfg)
chk("(1b) secondary + default mode ('hybrid') → per-task routing ON",
    backend_pref.get_mode(cfg) == "hybrid" and backend_pref.get_hybrid(cfg) is True)
backend_pref.set_mode("backup", cfg)
chk("(1c) BACKUP mode → per-tag routing OFF (fallback only)",
    backend_pref.get_mode(cfg) == "backup" and backend_pref.get_hybrid(cfg) is False)
chk("(1d) the fallback resolver still sees the secondary in backup mode",
    backend_pref.get_secondary(cfg) == "glm")
backend_pref.set_mode("hybrid", cfg)
backend_pref.set_secondary(None, cfg)
chk("(1e) secondary cleared → hybrid off regardless of mode",
    backend_pref.get_hybrid(cfg) is False)

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

# ── (6) /api/model — the secondary IS the mode switch now ──
from orchestrator import server
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()
backend_pref.set_secondary(None, cfg)
client.post("/api/model", data={"secondary": "glm"})
chk("(6a) setting a secondary arms hybrid", backend_pref.get_hybrid(cfg) is True)
client.post("/api/model", data={"secondary": "none"})
chk("(6b) clearing the secondary disarms hybrid (single-model mode)",
    backend_pref.get_hybrid(cfg) is False and backend_pref.get_secondary(cfg) is None)
client.post("/api/model", data={"mode": "backup"})
chk("(6c) mode=backup without a secondary is refused (nothing stored)",
    backend_pref.get_mode(cfg) == "hybrid")
client.post("/api/model", data={"secondary": "glm"})
client.post("/api/model", data={"mode": "backup"})
chk("(6d) mode=backup persists with a secondary → per-tag routing off",
    backend_pref.get_mode(cfg) == "backup" and backend_pref.get_hybrid(cfg) is False)
client.post("/api/model", data={"mode": "hybrid"})
chk("(6e) mode=hybrid re-arms per-task routing", backend_pref.get_hybrid(cfg) is True)
client.post("/api/model", data={"secondary": "none"})

# ── (7) toolbar — the Mode select appears only WITH a secondary; notes match the mode ──
from orchestrator import cockpit_views
bar_no_sec = cockpit_views.backend_control(cfg, "automatixy")
chk("(7a) one model → no Mode select (nothing to choose)", "name=mode" not in bar_no_sec)
backend_pref.set_secondary("glm", cfg)
backend_pref.set_mode("hybrid", cfg)
bar_hy = cockpit_views.backend_control(cfg, "automatixy")
chk("(7b) hybrid mode: the select renders with the plan-on-main note",
    "name=mode" in bar_hy and "value='hybrid' selected" in bar_hy
    and "plan on main" in bar_hy)
backend_pref.set_mode("backup", cfg)
bar_bk = cockpit_views.backend_control(cfg, "automatixy")
chk("(7c) backup mode: the standby note replaces the hybrid note",
    "value='backup' selected" in bar_bk and "standby" in bar_bk
    and "plan on main" not in bar_bk)
backend_pref.set_mode("hybrid", cfg)
backend_pref.set_secondary(None, cfg)
chk("(7d) no secondary → neither note",
    "plan on main" not in cockpit_views.backend_control(cfg, "automatixy"))

print("\n========== HYBRID MODE QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
