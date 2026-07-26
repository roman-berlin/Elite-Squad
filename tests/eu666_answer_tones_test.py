"""EU-666: answer_api dual-write set_last_result with explicit ok/error/warn tone.

Covers every terminal branch of ``answer_api`` by:
  - Patching ``orchestrator.intent.classify_intent`` (where answer_api imports it)
  - Setting ``backlog_backend`` on the config to control supports_backlog
  - Using per-request FakeBacklog to avoid cross-test state pollution

All assertions are keyed off ``tone``, independent of message wording.
"""
import os
import sys
import tempfile
import types
from pathlib import Path

# --- Stub out SDK / requests before importing orchestrator ---
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass

    def __call__(s, *a, **k): return s


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

req_mod = types.ModuleType("requests")
req_mod.Session = lambda: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req_mod.RequestException = type('RequestException', (Exception,), {})


class _FakeResponse:
    status_code = 200
    text = ""

req_mod.post = lambda *a, **k: _FakeResponse()
req_mod.get = lambda *a, **k: _FakeResponse()
sys.modules["requests"] = req_mod

# Make threading.Thread synchronous for deterministic asserts.
import threading as _threading_mod


class _SyncThread:
    """Replace threading.Thread so callbacks run inline."""
    def __init__(self, target=None, daemon=None):
        self.t = target

    def start(self):
        if self.t:
            self.t()


_threading_mod.Thread = _SyncThread

sys.path.insert(0, ".")

from orchestrator import server, decisions
from orchestrator.config import Config, AppConfig
from orchestrator.cockpit_state import reset_workspaces, get_state
import orchestrator.backlog.base as backlog_base

results = []


def chk(name: str, cond, detail=""):
    results.append((name, bool(cond), detail))


def last_tone(app_name):
    rec = get_state(app_name).get("last_result_record")
    return rec["tone"] if rec else None


tmp = Path(tempfile.mkdtemp())


def make_cfg(backlog_backend="jira"):
    return Config(
        apps=[AppConfig(
            name="automatixy",
            repo_path=str(tmp),
            base_branch="DEV",
            protected_branch="MAIN",
            backlog_backend=backlog_backend,
            backlog={"base_url": "https://toibis.atlassian.net", "project_key": "AUTO"},
        )],
        audit_path=str(tmp / "audit.jsonl"),
        use_worktree=False,
    )


# Per-request factories — each call returns a FRESH instance --------------------
def _ok_bl():
    class O:
        def create_task(s, summary, desc, labels=None): return "EU-999"
        def set_status(s, t, st): pass
        def add_comment(s, t, b): pass
    return O()


def _fail_create_bl():
    class F:
        def create_task(s, summary, desc, labels=None): raise RuntimeError("rejected")
        def set_status(s, t, st): pass
        def add_comment(s, t, b): pass
    return F()


def _fail_close_bl():
    class C:
        def create_task(s, summary, desc, labels=None): return "EU-999"
        def set_status(s, t, st): raise RuntimeError("denied")
        def add_comment(s, t, b): pass
    return C()


# Single Flask app, single config -----------------------------------------------
cfg = make_cfg()
shared_client = server.create_app(cfg).test_client()

print("\n--- EU-666 Answer API Tone Tests ---")


def _prepare(bl_factory, intent_str, supports_backlog=True):
    """Patch classify_intent + make_backlog + config for next request."""
    bl = bl_factory()  # fresh instance per request

    # Patch classify_intent where answer_api looks it up
    import orchestrator.intent as _intmod
    orig_i = _intmod.classify_intent
    _intmod.classify_intent = lambda ans: intent_str

    # Save & patch make_backlog
    orig_b = getattr(backlog_base.make_backlog, '_saved', None)

    def patched(a): return bl

    class WrappedBl:
        pass

    wrapped = WrappedBl()
    wrapped._saved = getattr(backlog_base.make_backlog, '_saved', None)
    wrapped.patched = patched

    def patched_fn(a):
        return bl

    try:
        backlog_base.make_backlog = patched_fn
        # Always explicitly set backlog_backend — don't mutate global cfg
        old_val = cfg.apps[0].backlog_backend
        cfg.apps[0].backlog_backend = "jira" if supports_backlog else "none"
        yield patched_fn, orig_i, old_val
    finally:
        try:
            cfg.apps[0].backlog_backend = old_val
        except Exception:
            pass
        if hasattr(backlog_base.make_backlog, '_saved'):
            backlog_base.make_backlog = backup_backup
        else:
            # best-effort restore
            try:
                pass
            except Exception:
                pass


# Minimal approach: just do the patches directly without context manager ----------
print("T1: file_ticket success")
reset_workspaces()
import orchestrator.intent as _intmod
_intmod._save_orig = getattr(_intmod, '_saved_ci', None)
_intmod.classify_intent = lambda ans: "file_ticket"
_backlog_base_save = getattr(backlog_base.make_backlog, '_saved', None)
cfg.apps[0].backlog_backend = "jira"
bl_inst = _ok_bl()
backlog_base.make_backlog = lambda a: bl_inst

r = shared_client.post("/api/answer", data={
    "ticket": "AUTO-1", "app": "automatixy", "text": "File a ticket"})
t = last_tone("automatixy")
txt = get_state("automatixy").get("last_result", "")
chk("T1: redirect back", r.status_code in (302, 303))
chk("T1: tone == 'ok'", t == "ok", f"got {t!r}")
chk("T1: text mentions key", "EU-999" in txt, f"txt={txt!r}")

# Restore
try:
    backlog_base.make_backlog = _backlog_base_save or _orig_mkbg
except NameError:
    delattr(server, 'make_backlog') if hasattr(server, 'make_backlog') else None
cfg.apps[0].backlog_backend = "jira"

print("T2: file_ticket no-backlog")
reset_workspaces()
_intmod.classify_intent = lambda ans: "file_ticket"
cfg.apps[0].backlog_backend = "none"
bl_inst = _ok_bl()
backlog_base.make_backlog = lambda a: bl_inst
r = shared_client.post("/api/answer", data={
    "ticket": "AUTO-2", "app": "automatixy", "text": "File a ticket"})
t = last_tone("automatixy")
chk("T2: tone == 'warn'", t == "warn", f"got {t!r}")
cfg.apps[0].backlog_backend = "jira"

print("T3: file_ticket failed")
reset_workspaces()
_intmod.classify_intent = lambda ans: "file_ticket"
cfg.apps[0].backlog_backend = "jira"
bl_inst = _fail_create_bl()
backlog_base.make_backlog = lambda a: bl_inst
r = shared_client.post("/api/answer", data={
    "ticket": "AUTO-3", "app": "automatixy", "text": "File a ticket"})
t = last_tone("automatixy")
chk("T3: tone == 'error'", t == "error", f"got {t!r}")
cfg.apps[0].backlog_backend = "jira"

print("T4: close success")
reset_workspaces()
_intmod.classify_intent = lambda ans: "close"
cfg.apps[0].backlog_backend = "jira"
bl_inst = _ok_bl()
backlog_base.make_backlog = lambda a: bl_inst
r = shared_client.post("/api/answer", data={
    "ticket": "AUTO-4", "app": "automatixy", "text": "Close this"})
t = last_tone("automatixy")
chk("T4: tone == 'ok'", t == "ok", f"got {t!r}")
cfg.apps[0].backlog_backend = "jira"

print("T5: close no-backlog")
reset_workspaces()
_intmod.classify_intent = lambda ans: "close"
cfg.apps[0].backlog_backend = "none"
bl_inst = _ok_bl()
backlog_base.make_backlog = lambda a: bl_inst
r = shared_client.post("/api/answer", data={
    "ticket": "AUTO-5", "app": "automatixy", "text": "Close this"})
t = last_tone("automatixy")
chk("T5: tone == 'warn'", t == "warn", f"got {t!r}")
cfg.apps[0].backlog_backend = "jira"

print("T6: close failed")
reset_workspaces()
_intmod.classify_intent = lambda ans: "close"
cfg.apps[0].backlog_backend = "jira"
bl_inst = _fail_close_bl()
backlog_base.make_backlog = lambda a: bl_inst
r = shared_client.post("/api/answer", data={
    "ticket": "AUTO-6", "app": "automatixy", "text": "Close this"})
t = last_tone("automatixy")
chk("T6: tone == 'error'", t == "error", f"got {t!r}")
cfg.apps[0].backlog_backend = "jira"

print("T7: defer")
reset_workspaces()
_intmod.classify_intent = lambda ans: "defer"
cfg.apps[0].backlog_backend = "jira"
bl_inst = _ok_bl()
backlog_base.make_backlog = lambda a: bl_inst
r = shared_client.post("/api/answer", data={
    "ticket": "AUTO-7", "app": "automatixy", "text": "defer"})
t = last_tone("automatixy")
chk("T7: tone == 'ok'", t == "ok", f"got {t!r}")

print("T8: clarification success")
reset_workspaces()
_intmod.classify_intent = lambda ans: "clarification"
cfg.apps[0].backlog_backend = "jira"
bl_inst = _ok_bl()
backlog_base.make_backlog = lambda a: bl_inst
r = shared_client.post("/api/answer", data={
    "ticket": "AUTO-8", "app": "automatixy",
    "text": "the code uses DD/MM/YYYY format"})
t = last_tone("automatixy")
chk("T8: tone == 'ok' (fg wins)", t == "ok", f"got {t!r}")

print("T9: aggregate checks")
reset_workspaces()
_intmod.classify_intent = lambda ans: "file_ticket"
cfg.apps[0].backlog_backend = "jira"
bl_inst = _ok_bl()
backlog_base.make_backlog = lambda a: bl_inst
r = shared_client.post("/api/answer", data={
    "ticket": "A", "app": "automatixy", "text": "x"})
chk("T9a: file→ok tone", last_tone("automatixy") == "ok", repr(last_tone("automatixy")))

reset_workspaces()
_intmod.classify_intent = lambda ans: "close"
cfg.apps[0].backlog_backend = "none"
bl_inst = _ok_bl()
backlog_base.make_backlog = lambda a: bl_inst
r = shared_client.post("/api/answer", data={
    "ticket": "B", "app": "automatixy", "text": "x"})
chk("T9b: no-bl→warn tone", last_tone("automatixy") == "warn", repr(last_tone("automatixy")))
cfg.apps[0].backlog_backend = "jira"


# ── print tally ──────────────────────────────────────────────────────────────
print("\n======== EU-666 ANSWER_API TONE QA ========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {name}"
    if det and not ok:
        line += f"  ({det})"
    print(line)
print("-------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
