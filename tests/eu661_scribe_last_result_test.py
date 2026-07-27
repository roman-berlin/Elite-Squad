"""Tests for EU-661: scribe handler writes via set_last_result (tone-based).

Verifies that the /api/scribe handler uses set_last_result(None, tone, text) instead of
bare _state["last_msg"] = ..., and that GET /memory reads from last_result+record with
tone-based styling (green ok, red error).
"""
import sys, types, tempfile, time
from pathlib import Path

sys.path.insert(0, ".")

# ── shim modules needed by server imports ────────────────────────────────
sdk_mod = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk_mod.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk_mod)

req_mod = types.ModuleType("requests")
req_mod.Session = lambda: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules.setdefault("requests", req_mod)

from orchestrator import server, memory, sync
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# ── helpers ──────────────────────────────────────────────────────────────
def make_cfg():
    tmp = Path(tempfile.mkdtemp())
    cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp / "app"),
                     base_branch="DEV", protected_branch="MAIN", backlog_backend="none")],
                 audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
    cfg.detected_auth = lambda: "test"
    return cfg

def make_client(cfg):
    return server.create_app(cfg).test_client()

def reset_state():
    """Clear ALL banner/result state (other tests depend on clean slate)."""
    server._state.pop("last_msg", None)
    server._state.pop("last_result", None)
    server._state.pop("last_result_record", None)
    server._state.pop("scribing", None)

def wait_scribe(timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not server._state.get("scribing"):
            return True
        time.sleep(0.05)
    return False

async def _scribe_ok(_cfg):
    return "folded 2 lessons"

async def _scribe_fail(_cfg):
    raise RuntimeError("boom")

async def _scribe_empty(_cfg):
    return ""

def run_scribe(client, coro_fn):
    orig = memory.scribe
    try:
        memory.scribe = coro_fn
        reset_state()
        resp = client.post("/api/scribe")
        assert resp.status_code in (301, 302, 303)
        assert wait_scribe(), "scribe did not finish within timeout"
    finally:
        memory.scribe = orig

# ══════════════════════════════════════════════════════════════════════════
# AC1: failure → last_result_record.tone == "error"
# ══════════════════════════════════════════════════════════════════════════
print("\n--- AC1 ---")
run_scribe(make_client(make_cfg()), _scribe_fail)
st = server._state
chk("AC1a: last_result_record exists", isinstance(st.get("last_result_record"), dict))
rec = st.get("last_result_record") or {}
chk("AC1b: tone is 'error'", rec.get("tone") == "error")
chk("AC1c: text contains 'scribe failed'", "scribe failed" in str(rec.get("text", "")))
chk("AC1d: last_result matches", st.get("last_result") == "scribe failed: boom")

# ══════════════════════════════════════════════════════════════════════════
# AC2: success → last_result_record.tone == "ok"
# ══════════════════════════════════════════════════════════════════════════
print("--- AC2 ---")
run_scribe(make_client(make_cfg()), _scribe_ok)
st2 = server._state
chk("AC2a: tone is 'ok'", st2.get("last_result_record", {}).get("tone") == "ok")
chk("AC2b: text has ✓ prefix", "✓ folded 2 lessons" == st2.get("last_result"))

print("--- AC2b: empty fallback ---")
run_scribe(make_client(make_cfg()), _scribe_empty)
chk("AC2b: fallback text", "Squad memory updated by the Technical Writer"
    in str(server._state.get("last_result", "")))

# ══════════════════════════════════════════════════════════════════════════
# AC3: sequence preserves independently (overwrite semantics)
# ══════════════════════════════════════════════════════════════════════════
print("--- AC3 ---")
client3 = make_client(make_cfg())
run_scribe(client3, _scribe_fail)
chk("AC3a: after fail, tone=error", server._state.get("last_result_record", {}).get("tone") == "error")
fail_text = server._state.get("last_result", "")

run_scribe(client3, _scribe_ok)
chk("AC3b: after success, tone=ok", server._state.get("last_result_record", {}).get("tone") == "ok")
chk("AC3c: text replaced", "folded 2 lessons" in server._state.get("last_result", ""))
chk("AC3d: no 'boom' leak", "boom" not in str(server._state.get("last_result_record", {}).get("text", "")))

# ══════════════════════════════════════════════════════════════════════════
# AC4: no bare _state["last_msg"] written by this handler
# ══════════════════════════════════════════════════════════════════════════
print("--- AC4 ---")
cfg4 = make_cfg()
# Use a dedicated helper that preserves last_msg (set_seed reseeds it after reset clears it).
def run_scribe_preserve_last_msg(client, coro_fn):
    """Like run_scribe but does NOT pop last_msg in reset."""
    orig = memory.scribe
    try:
        memory.scribe = coro_fn
        server._state.pop("last_result", None)
        server._state.pop("last_result_record", None)
        server._state.pop("scribing", None)
        # Don't pop last_msg — the caller seeds it and expects it preserved.
        resp = client.post("/api/scribe")
        assert resp.status_code in (301, 302, 303)
        assert wait_scribe(), "scribe did not finish within timeout"
    finally:
        memory.scribe = orig

server._state["last_msg"] = "PRE-EXISTING"
client4 = make_client(cfg4)
run_scribe_preserve_last_msg(client4, _scribe_fail)
chk("AC4a: last_msg unchanged post-fail", server._state.get("last_msg") == "PRE-EXISTING")
run_scribe_preserve_last_msg(client4, _scribe_ok)
chk("AC4b: last_msg unchanged post-success", server._state.get("last_msg") == "PRE-EXISTING")

# ══════════════════════════════════════════════════════════════════════════
# AC5: GET /memory renders error banner (red), persistent until dismissed
# (EU-673: the page peeks the stored result now — the old one-shot pop was the last
# destructive reader and it erased the live board's strip on every visit)
# (EU-701: the banner is now the shared cockpit_views._result_strip — the same
# renderer the live board splices in — so the tone pins assert the THEME TOKENS
# (var(--bad*)/var(--ok*)) instead of the retired hand-rolled banner's raw hex.)
# ══════════════════════════════════════════════════════════════════════════
print("--- AC5 ---")
cfg5 = make_cfg()
# Clear stale last_msg from other test sections (the new memory_page reads from last_result).
server._state.pop("last_msg", None)
client5 = make_client(cfg5)
run_scribe(client5, _scribe_fail)
h_fail = client5.get("/memory").get_data(as_text=True)
chk("AC5a: failure text visible", "scribe failed: boom" in h_fail)
chk("AC5b: green success absent (tone=error uses red)", "var(--okbg)" not in h_fail)
chk("AC5c: red error styling present", "var(--badbg)" in h_fail and "var(--bad)" in h_fail)
h_again = client5.get("/memory").get_data(as_text=True)
chk("AC5c: banner persists on reload (EU-673 peek, not pop)", "scribe failed: boom" in h_again)
chk("AC5d: dismiss-result clears the banner",
    client5.post("/api/dismiss-result?app=automatixy").status_code == 200
    and "scribe failed: boom" not in client5.get("/memory").get_data(as_text=True))

# ══════════════════════════════════════════════════════════════════════════
# REPORT
# ══════════════════════════════════════════════════════════════════════════
print("\n============ EU-661 Scribe last_result QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------")
print(f"  {passed}/{len(results)} passed")
if passed != len(results):
    print(f"  RESULT: {len(results)-passed} FAIL")
else:
    print("  RESULT: ALL GREEN")
sys.exit(0 if passed == len(results) else 1)
