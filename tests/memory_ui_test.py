"""Unit Memory UI QA: the Update-memory (Scribe) and Consolidate actions show a one-shot confirmation
banner when they finish — instead of silently returning to the same page with no sign it worked — and
the banner clears after a single view. While the Scribe is mid-fold the page shows the working
indicator and must NOT pop the pending message early (so the confirmation survives to the next refresh)."""
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

from orchestrator import server, memory, consolidate
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp / "app"), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"

# Don't touch the real Unit Memory file or audit during the test.
memory.ensure = lambda *a, **k: None
memory.load = lambda *a, **k: "(stub memory)"
consolidate.rejection_patterns = lambda *a, **k: []

client = server.create_app(cfg).test_client()

# --- success path: Scribe finished (last_msg set, not scribing) -> banner shows once, then clears ---
server._state["scribing"] = False
server._state["last_msg"] = "✓ Scribe: Unit Memory updated — folded in 2 lessons."
h1 = client.get("/memory").get_data(as_text=True)
chk("scribe confirmation shows after the fold", "folded in 2 lessons" in h1)
chk("buttons are back (not stuck on the working spinner)", "Update memory" in h1)
h2 = client.get("/memory").get_data(as_text=True)
chk("banner is one-shot (cleared on the next view)", "folded in 2 lessons" not in h2)

# --- mid-fold: scribing True -> working indicator, and the pending message is NOT popped early ---
server._state["scribing"] = True
server._state["last_msg"] = "✓ Scribe: Unit Memory updated — folded in 2 lessons."
hw = client.get("/memory").get_data(as_text=True)
chk("mid-fold shows the working indicator", "folding recent lessons" in hw)
chk("mid-fold hides the Update button (it's running)", "Update memory" not in hw)
chk("mid-fold preserves last_msg (survives to the next refresh)",
    server._state.get("last_msg") == "✓ Scribe: Unit Memory updated — folded in 2 lessons.")
server._state["scribing"] = False
server._state.pop("last_msg", None)

# --- error path is surfaced too (a silent failure was the original bug) ---
server._state["last_msg"] = "scribe failed: boom"
he = client.get("/memory").get_data(as_text=True)
chk("scribe failure is surfaced, not swallowed", "scribe failed: boom" in he)

# --- no message -> no banner, page still renders fine ---
server._state.pop("last_msg", None)
hn = client.get("/memory").get_data(as_text=True)
chk("no pending message -> page renders without a banner", "Update memory" in hn and "scribe failed" not in hn)

print("\n============ UNIT MEMORY UI QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
