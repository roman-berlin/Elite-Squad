"""Unit Memory UI QA: the Update-memory (Scribe) action shows a confirmation banner when it
finishes — instead of silently returning to the same page with no sign it worked. EU-673: the
banner PEEKS the stored result (it used to pop it — the last destructive reader the EU-677
retirement missed), so the confirmation persists across reloads and — crucially — visiting
/memory no longer erases the live board's strip; only POST /api/dismiss-result clears it.
While the Scribe is mid-fold the page shows the working indicator and must NOT read the pending
message as a banner (so the confirmation survives to the next refresh)."""
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

from orchestrator import server, memory
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
memory._live_log = lambda *a, **k: "(stub lessons log)"

client = server.create_app(cfg).test_client()

# --- success path: Scribe finished (last_result+record set, not scribing) -> banner persists until dismissed ---
server._state["scribing"] = False
server._state["last_result"] = "✓ Scribe: Unit Memory updated — folded in 2 lessons."
server._state["last_result_record"] = {"tone": "ok", "text": server._state["last_result"], "timestamp": 0}
h1 = client.get("/memory").get_data(as_text=True)
chk("scribe confirmation shows after the fold", "folded in 2 lessons" in h1)
chk("buttons are back (not stuck on the working spinner)", "Update memory" in h1)
h2 = client.get("/memory").get_data(as_text=True)
chk("EU-673: banner persists on reload (peek, not pop)", "folded in 2 lessons" in h2)
chk("EU-673: visiting /memory leaves the stored record intact for the live board",
    server._state.get("last_result_record", {}).get("text", "").endswith("folded in 2 lessons."))
chk("EU-673: POST /api/dismiss-result clears it",
    client.post("/api/dismiss-result?app=automatixy").status_code == 200)
h3 = client.get("/memory").get_data(as_text=True)
chk("EU-673: banner gone after dismiss", "folded in 2 lessons" not in h3)

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
server._state.pop("last_result", None)
server._state.pop("last_result_record", None)

# --- error path is surfaced too (a silent failure was the original bug) ---
server._state["last_result"] = "scribe failed: boom"
server._state["last_result_record"] = {"tone": "error", "text": "scribe failed: boom", "timestamp": 0}
he = client.get("/memory").get_data(as_text=True)
chk("scribe failure is surfaced, not swallowed", "scribe failed: boom" in he)

# --- 2026-07-19: one manual action only — the Consolidate button and the never-clearing
# "Reviewer keeps rejecting these" panel were removed (consolidation is automatic) ---
hp = client.get("/memory").get_data(as_text=True)
chk("no Consolidate button on the page", "/api/consolidate" not in hp)
chk("no 'Reviewer keeps rejecting' panel on the page", "keeps rejecting" not in hp)
chk("the removed /api/consolidate route 404s", client.post("/api/consolidate").status_code == 404)

# --- no message -> no banner, page still renders fine ---
server._state.pop("last_msg", None)
server._state.pop("last_result", None)
server._state.pop("last_result_record", None)
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
