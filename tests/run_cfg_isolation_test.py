"""EU-3 [F5]: Telegram /run must not mutate the shared Config.

handle_command used to do `cfg.dry_run = not live` on the SAME Config the autopilot loop +
its Telegram poller share. A `/run`/`/drain` without --live flipped the live autopilot into
dry-run -> it re-picked the same ticket forever (SSOT violation + cross-thread race).

Fix: build a per-invocation copy.copy(cfg) and set dry_run on the copy; pass the copy to
intake/_run_bg. These checks assert the shared cfg.dry_run is untouched while the dispatched
run still honours dry-run.
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

from orchestrator import decisions, intake, notify
from orchestrator.config import Config, AppConfig

notify.send = lambda *a, **k: None

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
(tmp / "audit.jsonl").write_text("")
app = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                protected_branch="MAIN", backlog_backend="none")

# The shared cfg the autopilot loop runs on — LIVE (dry_run False).
cfg = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg.dry_run = False

# Capture the cfg that intake + the background run actually receive, without doing real work.
seen = {}
intake.from_text = lambda c, *a, **k: (seen.__setitem__("intake_cfg", c), ["WL"])[1]
intake.from_drain = lambda c, *a, **k: (seen.__setitem__("intake_cfg", c), ["WL"])[1]
decisions._run_bg = lambda c, audit, wl: seen.__setitem__("run_cfg", c)

class FakeAudit:
    def record(s, *a, **k): pass

# ---- /run without --live: dispatched run is dry-run, shared cfg stays LIVE ----
decisions.handle_command(cfg, FakeAudit(), "/run automatixy do a thing")
chk("shared cfg.dry_run UNCHANGED after /run (autopilot still LIVE)", cfg.dry_run is False, str(cfg.dry_run))
chk("dispatched run honours dry-run (run_cfg.dry_run True)",
    seen.get("run_cfg") is not None and seen["run_cfg"].dry_run is True)
chk("intake got the per-invocation copy, not the shared cfg", seen.get("intake_cfg") is not cfg)
chk("dispatched copy is a distinct object from shared cfg", seen.get("run_cfg") is not cfg)

# ---- /run --live: dispatched run is live; shared cfg untouched ----
seen.clear()
cfg.dry_run = False
decisions.handle_command(cfg, FakeAudit(), "/run automatixy do a thing --live")
chk("shared cfg.dry_run UNCHANGED after /run --live", cfg.dry_run is False)
chk("dispatched --live run is live (run_cfg.dry_run False)",
    seen.get("run_cfg") is not None and seen["run_cfg"].dry_run is False)

# ---- the inverse: a dry-run autopilot is NOT flipped live by /run --live ----
seen.clear()
cfg.dry_run = True
decisions.handle_command(cfg, FakeAudit(), "/run automatixy do a thing --live")
chk("shared cfg.dry_run UNCHANGED (stays True) after /run --live", cfg.dry_run is True)
chk("dispatched --live run still honours its own live flag", seen["run_cfg"].dry_run is False)

# ---- /drain without --live: same isolation guarantee ----
seen.clear()
cfg.dry_run = False
decisions.handle_command(cfg, FakeAudit(), "/drain automatixy")
chk("shared cfg.dry_run UNCHANGED after /drain (autopilot still LIVE)", cfg.dry_run is False)
chk("dispatched /drain run honours dry-run", seen["run_cfg"].dry_run is True)

print("\n============ /run CFG ISOLATION QA (EU-3) ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
