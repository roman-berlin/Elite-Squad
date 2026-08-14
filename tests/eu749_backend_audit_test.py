"""EU-842: every backend_pref mutation emits an audit event via the choke-point hook.

Tests that set_active, set_secondary, and set_mode record exactly one 'model_backend_changed'
event each time they fire — without any call-site change (AC3: only backend_pref.py is modified).

Pattern: mirrors eu256 / eu397 harnesses — import → stub → assert → sys.exit(0|1).
"""
import sys
import types
import tempfile
from pathlib import Path

# ── Stub the Agent SDK before any orchestrator import (house convention) ──
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

sys.path.insert(0, ".")

from orchestrator.config import Config, AppConfig
from orchestrator.audit import AuditLog
from orchestrator import backend_pref

# ── Stub infrastructure ────────────────────────────────────────────────────────
# A fake sink that mimics AuditLog.record() but keeps rows in memory.
class _FakeSink:
    """Minimal fake AuditLog — accepts record(event, **fields), stores copies."""
    def __init__(self):
        self.records: list[dict] = []
    def record(self, event: str, **fields: object) -> None:
        row = {"ts": "stub", "event": event}
        row.update(fields)
        self.records.append(dict(row))


def _setup_stub():
    """Attach a fresh fake sink and return the factory wrapper."""
    backend_pref._AUDIT_LOG = _FakeSink()
    return backend_pref


# ── Helpers ────────────────────────────────────────────────────────────────────
def _tmp_cfg(d: Path) -> Config:
    return Config(apps=[AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV",
                                  protected_branch="MAIN", backlog_backend="none")],
                  audit_path=str(d / "state" / "audit.jsonl"), use_worktree=False)


def _last_record():
    """Return the last audit record (None if none emitted)."""
    return backend_pref._AUDIT_LOG.records[-1] if backend_pref._AUDIT_LOG else None


def _count_emitted():
    return len(backend_pref._AUDIT_LOG.records) if backend_pref._AUDIT_LOG else 0

results: list[tuple[str, bool, str]] = []


def chk(n, ok, detail=""):
    results.append((n, bool(ok), detail))


# ── Notify stub for EU-844 ───────────────────────────────────────────────────
class _NotifyStub:
    """Minimal mock for ``notify.send`` — counts calls, stores positional args, can raise."""
    def __init__(self):
        self.call_count = 0
        self.call_args_text: str | None = None
        self.side_effect: BaseException | None = None
    def __call__(self, text: str, chat_id=None) -> bool:
        if self.side_effect:
            raise self.side_effect
        self.call_count += 1
        self.call_args_text = text
        return True


# ============================================================================= #
# Test 1 — set_active globally emits model_backend_changed
# ============================================================================= #
def test_set_active_global():
    _setup_stub()
    d = Path(tempfile.mkdtemp())
    cfg = _tmp_cfg(d)

    # Set initial backend so we have a non-empty FROM value
    backend_pref.set_active("opus", cfg)

    # Capture baseline
    baseline = _count_emitted()

    # Now set a new backend
    backend_pref.set_active("glm", cfg)

    emitted = _count_emitted() - baseline
    chk("emit count == 1 after set_active global", emitted == 1,
        f"emitted {emitted} events")

    rec = _last_record()
    chk("event kind == model_backend_changed",
        rec is not None and rec.get("event") == "model_backend_changed",
        f"got: {rec}")
    chk("field == active", rec is not None and rec.get("field") == "active",
        f"got: {rec}")
    chk("scope == global", rec is not None and rec.get("scope") == "global",
        f"got: {rec}")
    chk("from == opus (the value set just before)",
        rec is not None and rec.get('from') == "opus",
        f"got: {rec}")
    chk("to == glm", rec is not None and rec.get('to') == "glm",
        f"got: {rec}")
    chk("source tag present",
        rec is not None and rec.get("source"),
        f"got source={rec.get('source')}")


# ============================================================================= #
# Test 2 — set_active per-app emits correct scoped event
# ============================================================================= #
def test_set_active_per_app():
    _setup_stub()
    d = Path(tempfile.mkdtemp())
    cfg = _tmp_cfg(d)

    baseline = _count_emitted()
    backend_pref.set_active("glm", cfg, app_name="automatixy")

    emitted = _count_emitted() - baseline
    chk("emit count == 1 after per-app override", emitted == 1)
    rec = _last_record()
    chk("per-app event scope == app name",
        rec is not None and rec.get("scope") == "automatixy",
        f"got: {rec}")
    chk("per-app from == None (no override before)",
        rec is not None and rec.get('from') is None,
        f"got: {rec}")
    chk("to == glm", rec is not None and rec.get('to') == "glm",
        f"got: {rec}")

    # Clear the override (bk=None → inherit)
    baseline = _count_emitted()
    backend_pref.set_active(None, cfg, app_name="automatixy")
    emitted = _count_emitted() - baseline
    chk("emit count == 1 after clear override", emitted == 1)
    rec = _last_record()
    chk("clear scope == automatixy",
        rec is not None and rec.get("scope") == "automatixy",
        f"got: {rec}")

    # Empty-string clear
    baseline = _count_emitted()
    backend_pref.set_active("", cfg, app_name="automatixy")
    emitted = _count_emitted() - baseline
    chk("emit count == 1 after empty-string clear", emitted == 1)

    # "inherit" marker
    baseline = _count_emitted()
    backend_pref.set_active("inherit", cfg, app_name="automatixy")
    emitted = _count_emitted() - baseline
    chk("emit count == 1 after inherit clear", emitted == 1)


# ============================================================================= #
# Test 3 — set_secondary emits model_backend_changed
# ============================================================================= #
def test_set_secondary():
    _setup_stub()
    d = Path(tempfile.mkdtemp())
    cfg = _tmp_cfg(d)

    baseline = _count_emitted()
    backend_pref.set_secondary("glm", cfg)
    emitted = _count_emitted() - baseline
    chk("emit count == 1 after set_secondary", emitted == 1,
        f"events: {backend_pref._AUDIT_LOG.records}")

    rec = _last_record()
    chk("secondary field == secondary",
        rec is not None and rec.get("field") == "secondary",
        f"got: {rec}")
    chk("scope == global", rec is not None and rec.get("scope") == "global",
        f"got: {rec}")
    chk("secondary from == None (never set) and to == glm",
        rec is not None and rec.get('from') is None and rec.get('to') == "glm",
        f"got: {rec}")
    chk("source tag present",
        rec is not None and rec.get("source"),
        f"got: {rec}")


# ============================================================================= #
# Test 4 — set_mode emits model_backend_changed
# ============================================================================= #
def test_set_mode():
    _setup_stub()
    d = Path(tempfile.mkdtemp())
    cfg = _tmp_cfg(d)

    baseline = _count_emitted()
    backend_pref.set_mode("backup", cfg)
    emitted = _count_emitted() - baseline
    chk("emit count == 1 after set_mode", emitted == 1,
        f"events: {backend_pref._AUDIT_LOG.records}")

    rec = _last_record()
    chk("mode field == mode",
        rec is not None and rec.get("field") == "mode",
        f"got: {rec}")
    chk("mode from == hybrid (the default)",
        rec is not None and rec.get('from') == "hybrid",
        f"got: {rec}")
    chk("to == backup", rec is not None and rec.get('to') == "backup",
        f"got: {rec}")
    chk("source tag present",
        rec is not None and rec.get("source"),
        f"got: {rec}")


# ============================================================================= #
# Test 5 — source tag defaults to 'cli', configurable
# ============================================================================= #
def test_source_tag():
    _setup_stub()
    d = Path(tempfile.mkdtemp())
    cfg = _tmp_cfg(d)

    # Default source should be 'cli'
    backend_pref.set_active("glm", cfg)
    rec = _last_record()
    chk("default source == cli",
        rec is not None and rec.get("source") == "cli",
        f"got: {rec}")

    # configure_source override
    backend_pref.configure_source("cockpit")
    backend_pref.set_secondary("opus", cfg)
    rec = _last_record()
    chk("configure_source sets tag",
        rec is not None and rec.get("source") == "cockpit",
        f"got: {rec}")


_setup_stub()
test_set_active_global()
test_set_active_per_app()
test_set_secondary()
test_set_mode()
test_source_tag()


# ============================================================================= #
# EU-844: NEW test helpers — Telegram notification on model-backend change
# ============================================================================= #

def test_telegram_notify_once():
    """Each write triggers exactly ONE notify.send with the correct message shape. (AC1)"""
    _setup_stub()
    d = Path(tempfile.mkdtemp())
    cfg = _tmp_cfg(d)
    from orchestrator import notify
    orig_send = notify.send
    notify.send = _NotifyStub()
    stub = notify.send

    # Reset source to 'cli' in case previous tests clobbered it via configure_source()
    backend_pref.configure_source("cli")
    backend_pref.set_active("glm", cfg)
    chk("global set_active → call_count==1",
        stub.call_count == 1, f"call_count={stub.call_count}")
    text = stub.call_args_text
    expected = "⚙️ Model backend changed [global]: none → glm (via cli)"
    chk("global message exact text", text == expected, f"expected {expected!r} got {text!r}")

    # Per-app
    backend_pref.configure_source("cli")
    stub.call_count = 0
    stub.call_args_text = None
    backend_pref.set_active("glm", cfg, app_name="automatixy")
    chk("per-app → call_count==1", stub.call_count == 1, f"call_count={stub.call_count}")
    chk("per-app scope automatixy", "automatixy" in str(stub.call_args_text), f"text={stub.call_args_text!r}")
    chk("per-app none→glm", "none → glm" in str(stub.call_args_text), f"text={stub.call_args_text!r}")

    # set_secondary
    stub.call_count = 0
    stub.call_args_text = None
    backend_pref.set_secondary("glm", cfg)
    chk("set_secondary → call_count==1", stub.call_count == 1, f"call_count={stub.call_count}")

    notify.send = orig_send


def test_raise_does_not_propagate():
    """A notifier that raises must NOT block the write or audit. (AC2)"""
    _setup_stub()
    d = Path(tempfile.mkdtemp())
    cfg = _tmp_cfg(d)
    from orchestrator import notify
    notify.send = _NotifyStub()
    notify.send.side_effect = RuntimeError("telegram down")
    ok = True
    try:
        backend_pref.set_active("glm", cfg)
    except Exception:
        ok = False
    chk("set_active did NOT raise", ok, "got exception")
    chk("backend persisted", backend_pref.get(cfg) == "glm", f"saved={backend_pref.get(cfg)}")
    rec = _last_record()
    chk("audit recorded",
        rec is not None and rec.get("event") == "model_backend_changed", f"rec={rec}")
    notify.send.side_effect = None


def test_per_app_clear_emits_one():
    """A per-app clear emits exactly ONE notification, not zero, not two. (AC3)"""
    _setup_stub()
    d = Path(tempfile.mkdtemp())
    cfg = _tmp_cfg(d)
    from orchestrator import notify
    orig_send = notify.send
    stub = _NotifyStub()
    notify.send = stub

    backend_pref.configure_source("cli")
    # Set override first
    backend_pref.set_active("opus", cfg, app_name="automatixy")
    events_before = len(backend_pref._AUDIT_LOG.records)
    # Clear it
    backend_pref.set_active(None, cfg, app_name="automatixy")
    emitted = len(backend_pref._AUDIT_LOG.records) - events_before
    chk("emit==1 after per-app clear", emitted == 1, f"emitted={emitted}")
    chk("notify called for both ops", stub.call_count >= 2, f"count={stub.call_count}")
    chk("clear has opus→none", "opus → none" in str(stub.call_args_text), f"text={stub.call_args_text!r}")

    notify.send = orig_send
# Fail-first: call the EU-844 tests now so they FAIL against the unchanged code.
test_telegram_notify_once()
test_raise_does_not_propagate()
test_per_app_clear_emits_one()

# ============================================================================= #
# Results
# ============================================================================= #
print("\n========== EU-842 BACKEND-PREF AUDIT QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
