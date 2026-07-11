#!/usr/bin/env python3
"""EU-211: Persist alert deduplication state across restarts.

Alert dedup flags (`_plan_limit_alert_sent`, `_dual_low_watermark_alerted`) used to live in
process memory only, so an orchestrator restart re-fired the same Telegram alert. This harness
proves the flags are now persisted to a locked JSON sidecar (`sonnet_alert_dedup.json`) that lands
in the SAME state root the rest of the codebase uses — the parent of ``cfg.audit_path`` (beside
sonnet_fallback_state.json / model_backend.json), NOT a separate env-var dir — and are reloaded on
a simulated restart (fresh module import against the same state dir).

Tests that:
1. plan_limit_alert() fires once and writes the dedup file (in cfg.audit_path's parent) with
   plan_limit_alert_sent=true.
2. A simulated restart (fresh module import, same cfg) loads the persisted flag and does NOT
   re-fire / does NOT call send().
3. dual_low_watermark_alert() persists per-provider keys — 'claude' firing and persisting doesn't
   block 'glm' from firing — and a concurrent process's persisted provider is MERGED, not clobbered.
4. reset_plan_limit_alert()/reset_dual_low_watermark_alert() clear both memory AND the persisted
   file (per-provider), so a later alert can fire again.
5. A missing or corrupt dedup file never raises and is treated as "no flags set".
6. (source) the sidecar path is derived from cfg.audit_path — no invented env-var/relative default.
"""
from __future__ import annotations

import importlib
import json
import shutil
import sys
import tempfile
import types
from pathlib import Path

# ── Stub the Agent SDK before any orchestrator import (house convention) ──
_sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k):
        self.__dict__.update(k)

    def __call__(self, *a, **k):
        return self


_sdk.__getattr__ = lambda n: _D  # type: ignore[attr-defined]
sys.modules.setdefault("claude_agent_sdk", _sdk)
sys.path.insert(0, ".")

from orchestrator.config import AppConfig, Config  # noqa: E402

results = []
_tmp_dirs: list[Path] = []
_ORIG_NOTIFY = sys.modules.get("orchestrator.notify")


def chk(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def _cfg(state_root: Path) -> Config:
    """A Config whose audit_path lives under ``state_root/state/`` — so the dedup sidecar resolves
    to ``state_root/state/sonnet_alert_dedup.json`` (the audit_path-derived state dir)."""
    return Config(apps=[AppConfig(name="automatixy", repo_path=str(state_root), base_branch="DEV",
                                  protected_branch="MAIN", backlog_backend="none")],
                  audit_path=str(state_root / "state" / "audit.jsonl"), use_worktree=False)


def _mkroot() -> Path:
    d = Path(tempfile.mkdtemp())
    _tmp_dirs.append(d)
    return d


def _dedup_file(root: Path) -> Path:
    return root / "state" / "sonnet_alert_dedup.json"


def _fresh_notify():
    """Reload orchestrator.notify from scratch — simulates a process restart: module globals (the
    in-memory flags and the ``_dedup_loaded`` guard) are wiped and re-seeded from disk on the next
    cfg-bearing call."""
    sys.modules.pop("orchestrator.notify", None)
    mod = importlib.import_module("orchestrator.notify")
    mod.configured = lambda: True
    return mod


def test_plan_limit_alert_fires_and_persists():
    root = _mkroot()
    cfg = _cfg(root)
    notify = _fresh_notify()
    sent_calls = []
    notify.send = lambda text, chat_id=None: (sent_calls.append(text) or True)

    ok = notify.plan_limit_alert([{"label": "weekly"}], ["Mon 14:00 UTC"], cfg)
    chk("plan_limit_alert fires and returns True", ok is True)
    chk("plan_limit_alert called send() once", len(sent_calls) == 1)

    dedup_path = _dedup_file(root)
    chk("dedup file written under cfg.audit_path's parent", dedup_path.exists(), str(dedup_path))
    data = json.loads(dedup_path.read_text(encoding="utf-8"))
    chk("dedup file records plan_limit_alert_sent=true", data.get("plan_limit_alert_sent") is True,
        str(data))


def test_restart_does_not_refire_plan_limit_alert():
    root = _mkroot()
    cfg = _cfg(root)
    notify = _fresh_notify()
    sent_calls = []
    notify.send = lambda text, chat_id=None: (sent_calls.append(text) or True)
    notify.plan_limit_alert([{"label": "weekly"}], ["Mon 14:00 UTC"], cfg)
    chk("first fire sent one message", len(sent_calls) == 1)

    # Simulate a restart: fresh module import, same cfg / same state dir on disk.
    notify2 = _fresh_notify()
    sent_calls2 = []
    notify2.send = lambda text, chat_id=None: (sent_calls2.append(text) or True)

    ok = notify2.plan_limit_alert([{"label": "weekly"}], ["Mon 14:00 UTC"], cfg)
    chk("restart: plan_limit_alert returns False (already fired)", ok is False)
    chk("restart: send() was NOT called again", len(sent_calls2) == 0)


def test_dual_low_watermark_persists_per_provider():
    root = _mkroot()
    cfg = _cfg(root)
    notify = _fresh_notify()
    sent_calls = []
    notify.send = lambda text, chat_id=None: (sent_calls.append(text) or True)

    claude_usage = {"limits": [{"label": "weekly", "utilization": 0.92}]}
    ok = notify.dual_low_watermark_alert("claude", claude_usage, cfg)
    chk("claude low-watermark alert fires", ok is True)

    dedup_path = _dedup_file(root)
    data = json.loads(dedup_path.read_text(encoding="utf-8"))
    chk("claude persisted in dedup file", "claude" in (data.get("dual_low_watermark_alerted") or []),
        str(data))

    # Fresh "restart" module, same cfg / state dir.
    notify2 = _fresh_notify()
    sent_calls2 = []
    notify2.send = lambda text, chat_id=None: (sent_calls2.append(text) or True)

    ok_claude_again = notify2.dual_low_watermark_alert("claude", claude_usage, cfg)
    chk("restart: claude does NOT re-fire", ok_claude_again is False)
    chk("restart: send() not called for claude replay", len(sent_calls2) == 0)

    glm_usage = {"used": 900, "cap": 1000, "pct": 0.9}
    ok_glm = notify2.dual_low_watermark_alert("glm", glm_usage, cfg)
    chk("restart: glm still fires once (independent key)", ok_glm is True)
    chk("restart: send() called for glm", len(sent_calls2) == 1)


def test_concurrent_provider_key_is_merged_not_clobbered():
    """A second process persisted 'glm' between our load and our save; when THIS process persists
    'claude', the merge (read fresh under the lock) must keep 'glm' too."""
    root = _mkroot()
    cfg = _cfg(root)
    notify = _fresh_notify()
    notify.send = lambda text, chat_id=None: True

    # Seed 'claude' in memory + on disk.
    notify.dual_low_watermark_alert("claude", {"limits": [{"label": "w", "utilization": 0.9}]}, cfg)

    # Simulate a concurrent process writing 'glm' straight to the sidecar (our in-memory set is stale).
    dedup_path = _dedup_file(root)
    on_disk = json.loads(dedup_path.read_text(encoding="utf-8"))
    on_disk["dual_low_watermark_alerted"] = sorted(set(on_disk.get("dual_low_watermark_alerted") or []) | {"glm"})
    dedup_path.write_text(json.dumps(on_disk), encoding="utf-8")

    # Now reset only 'claude' from THIS process — 'glm' must survive on disk (merge, not overwrite).
    notify.reset_dual_low_watermark_alert("claude", cfg)
    data = json.loads(dedup_path.read_text(encoding="utf-8"))
    persisted = set(data.get("dual_low_watermark_alerted") or [])
    chk("concurrent 'glm' survives our 'claude' reset (merge, not clobber)", persisted == {"glm"},
        str(sorted(persisted)))


def test_reset_clears_memory_and_disk():
    root = _mkroot()
    cfg = _cfg(root)
    notify = _fresh_notify()
    notify.send = lambda text, chat_id=None: True

    notify.plan_limit_alert([{"label": "weekly"}], ["Mon 14:00 UTC"], cfg)
    notify.dual_low_watermark_alert("claude", {"limits": [{"label": "w", "utilization": 0.9}]}, cfg)

    notify.reset_plan_limit_alert(cfg)
    notify.reset_dual_low_watermark_alert("claude", cfg)

    dedup_path = _dedup_file(root)
    data = json.loads(dedup_path.read_text(encoding="utf-8"))
    chk("reset clears plan_limit_alert_sent on disk", data.get("plan_limit_alert_sent") is False,
        str(data))
    chk("reset clears claude from disk", "claude" not in (data.get("dual_low_watermark_alerted") or []),
        str(data))

    sent_calls = []
    notify.send = lambda text, chat_id=None: (sent_calls.append(text) or True)
    ok = notify.plan_limit_alert([{"label": "weekly"}], ["Mon 14:00 UTC"], cfg)
    chk("after reset, plan_limit_alert can fire again", ok is True)
    chk("after reset, send() called again", len(sent_calls) == 1)

    ok2 = notify.dual_low_watermark_alert("claude", {"limits": [{"label": "w", "utilization": 0.9}]}, cfg)
    chk("after reset, claude low-watermark can fire again", ok2 is True)


def test_missing_or_corrupt_dedup_file_never_raises():
    root = _mkroot()
    cfg = _cfg(root)
    # No file at all yet.
    notify = _fresh_notify()
    notify.send = lambda text, chat_id=None: True
    ok = notify.plan_limit_alert([{"label": "weekly"}], ["Mon 14:00 UTC"], cfg)
    chk("missing dedup file: alert still fires", ok is True)

    # Now corrupt the file and reload.
    dedup_path = _dedup_file(root)
    dedup_path.write_text("{not json", encoding="utf-8")
    notify2 = _fresh_notify()
    sent_calls = []
    notify2.send = lambda text, chat_id=None: (sent_calls.append(text) or True)
    try:
        ok2 = notify2.plan_limit_alert([{"label": "weekly"}], ["Mon 14:00 UTC"], cfg)
        raised = False
    except Exception:  # noqa: BLE001
        raised = True
        ok2 = None
    chk("corrupt dedup file never raises", raised is False)
    chk("corrupt dedup file treated as no-flags-set: alert fires", ok2 is True)


def test_source_derives_path_from_audit_path():
    """The sidecar must be anchored to cfg.audit_path's parent — no invented env-var/relative
    default (the iteration-1 GENERAL_STATE_DIR / './state' regression)."""
    src = Path("orchestrator/notify.py").read_text(encoding="utf-8")
    chk("no GENERAL_STATE_DIR env-var default", "GENERAL_STATE_DIR" not in src)
    chk("path derives from cfg.audit_path", 'getattr(cfg, "audit_path"' in src and "_DEDUP_FILENAME" in src)


def _cleanup():
    for d in _tmp_dirs:
        shutil.rmtree(d, ignore_errors=True)
    # Restore the module registry we mutated so later suites see the original import.
    if _ORIG_NOTIFY is not None:
        sys.modules["orchestrator.notify"] = _ORIG_NOTIFY
    else:
        sys.modules.pop("orchestrator.notify", None)


def main():
    print("=" * 60)
    print("EU-211: Persist alert dedup state across restarts")
    print("=" * 60)

    try:
        test_plan_limit_alert_fires_and_persists()
        test_restart_does_not_refire_plan_limit_alert()
        test_dual_low_watermark_persists_per_provider()
        test_concurrent_provider_key_is_merged_not_clobbered()
        test_reset_clears_memory_and_disk()
        test_missing_or_corrupt_dedup_file_never_raises()
        test_source_derives_path_from_audit_path()
    finally:
        _cleanup()

    passed = sum(1 for _, c, _ in results if c)
    total = len(results)
    for name, cond, detail in results:
        mark = "✓" if cond else "✗"
        print(f"  {mark} {name}" + (f" ({detail})" if detail and not cond else ""))

    print(f"\n{passed}/{total} passed")
    print("RESULT:", "ALL GREEN" if passed == total else "FAIL")
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    main()
