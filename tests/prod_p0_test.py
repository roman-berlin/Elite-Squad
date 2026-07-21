"""2026-07-21 production audit — the five P0 fixes, pinned.

  (1) decisions.add NEVER voids a park over formatting: a markdown-headed PM brief is sanitized
      and stored (truly-empty still returns None);
  (2) the changelog committer uses the changelog repo's OWN branch for origin ops, never the
      landed app's base name (source pin — behavior covered by eu335);
  (3) a durable `land_pushed` audit event is recorded the moment the push succeeds, and
      _already_landed accepts it as the first key when the changelog line is missing;
  (4) the GLM budget preflight also arms when HYBRID routes builds to a GLM secondary;
  (5) the concurrent drain off-loads gate runs and the (explicitly lock-serialized) land to a
      worker thread; the serial drain keeps the inline path.
"""
import sys, types, tempfile
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

from orchestrator import decisions
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="A", repo_path=str(tmp), base_branch="dev",
                             protected_branch="main", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
tk = Ticket(id="A-1", key="A-1", summary="s", description="d", app="A")

# (1) sanitize-not-void
eid = decisions.add(cfg, tk, "A", "## Triage\nBased on the analysis, choose:\n1. keep\n2. split",
                    block=False)
pend = decisions.load(cfg)
chk("(1a) a markdown-headed PM brief is PARKED, not voided", eid is not None and len(pend) == 1)
chk("(1b) stored text is sanitized and self-describing",
    pend and "auto-sanitized" in pend[0]["question"] and "Triage" in pend[0]["question"])
chk("(1c) truly-empty still returns None", decisions.add(cfg, tk, "A", "   ", block=False) is None)

src = Path("orchestrator/loop.py").read_text(encoding="utf-8")
# (2) changelog branch
chk("(2) origin ops use the changelog repo's own branch", "base = branch" in src)
# (3) durable land marker
chk("(3a) land_pushed recorded at the push", 'audit.record("land_pushed", ticket_id=ticket.id' in src)
chk("(3b) _already_landed accepts the land_pushed key", '\'"land_pushed"\' in _ln' in src)
# (4) hybrid GLM governance — refined same day into the SYMMETRIC fallback: an exhausted GLM
# secondary makes hybrid stand down (builds on the Main, run proceeds); the hard preflight block
# remains only when the MAIN itself is GLM (nothing left to fall back to).
chk("(4a) exhausted GLM secondary → hybrid stands down to the Main (audited + notified)",
    'audit.record("hybrid_fallback_main"' in src and "hybrid stands down" in src)
chk("(4b) the hard block stays for a GLM MAIN only",
    'backends.normalize(getattr(cfg, "model_backend", "opus")) != backends.GLM' in src)
# (5) off-loop
chk("(5a) gate runs off-loop under concurrency", src.count("_off_loop(cfg, run_gate") == 2)
chk("(5b) the land is lock-serialized and off-loop",
    "_LAND_SERIAL_LOCK" in src and "_off_loop(cfg, _land_serialized)" in src)

print("\n========== PROD P0 QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
