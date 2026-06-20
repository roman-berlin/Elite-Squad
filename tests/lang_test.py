"""Regression: the unit is ENGLISH-ONLY — the EN/HE toggle + RTL machinery stay removed."""
import sys, tempfile, types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))

from orchestrator import memory, server
from orchestrator.config import Config, AppConfig

# 1) the language machinery is gone from memory; preamble never orders Hebrew
check("memory.set_language is removed", not hasattr(memory, "set_language"))
check("memory.language() is removed", not hasattr(memory, "language"))
pre = memory.preamble()
check("preamble carries NO Hebrew directive", "עברית" not in pre and "Respond ENTIRELY in Hebrew" not in pre)

# 2) the cockpit + sub-pages have no toggle, no /api/language route, no RTL bidi
d = Path(tempfile.mkdtemp())
(d / "audit.jsonl").write_text("")
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV",
                             protected_branch="MAIN", backlog_backend="none")],
             audit_path=str(d / "audit.jsonl"), use_worktree=False)
server.health.summary = lambda c: {"healthy": True, "checks": [{"name": "x", "status": "ok", "detail": ""}]}
app = server.create_app(cfg)
c = app.test_client()

body = c.get("/?app=automatixy").get_data(as_text=True)
check("cockpit has no EN/עב toggle", "/api/language" not in body and "עב" not in body)
r = c.post("/api/language", data={"lang": "he"})
check("/api/language route is gone (404)", r.status_code == 404, str(r.status_code))
chat = c.get("/chat").get_data(as_text=True)
check("sub-pages carry no RTL bidi rule", "unicode-bidi:plaintext" not in chat)
check("sub-pages have no toggle", "/api/language" not in chat)

print("\n================ ENGLISH-ONLY QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
