"""EU-211: persist the dual-provider low-watermark alert dedup flag across restarts.

Half of the parent ticket (EU-183 -> auto-split) was already done under EU-213: the plan-limit
alert flag persists to ``state/sonnet_alert_dedup.json``. The gap this ticket closes is
``_dual_low_watermark_alerted`` (notify.py), a module-level ``set[str]`` that reset to empty on
every restart, so a restarted process could re-fire the SAME low-watermark alert per provider.

This harness extends the SAME sidecar file (no second file) with a ``dual_low_watermark_alerted``
list key, read/written via the same ``locking.locked_rmw`` pattern as the plan-limit dedup.

Scenarios:
  1. ``dual_low_watermark_alert('claude', ...)`` with Telegram configured (send stubbed) writes
     the sidecar so it contains 'claude' in the persisted dual-watermark set.
  2. A fresh module state (in-memory set reset to empty, simulating a restart) calling the SAME
     alert for 'claude' again returns False and never calls send — loaded from disk.
  3. A DIFFERENT provider ('glm') still alerts even though 'claude' is already persisted — dedup
     is per-provider, not global.
  4. ``reset_dual_low_watermark_alert('claude')`` removes only 'claude' from the on-disk set,
     leaving 'glm'; ``reset_dual_low_watermark_alert(None)`` empties the on-disk set entirely.
  5. Writing the dual-watermark set does not erase the co-located plan-limit keys in the same
     JSON file, and vice-versa — both dedup domains coexist in one sidecar.
  6. A missing or corrupt ``sonnet_alert_dedup.json`` reads as an empty dual-watermark set
     (fail-open: a real alert must never be silently suppressed by a bad read).
  7. EU-390: a per-provider reset AFTER a restart (in-memory set empty, disk holds both
     providers) removes only the named provider from disk — the other provider's dedup entry
     survives. Fail-first: before EU-390 the reset persisted the (empty) in-memory set verbatim,
     wiping the OTHER provider's on-disk entry too -> RED.

Written fail-first: against notify.py before this ticket, ``_dual_watermark_dedup_read`` /
``_dual_watermark_dedup_write`` do not exist at all (AttributeError), and
``dual_low_watermark_alert`` never consults disk, so scenario 2's "loaded from disk" check
(expecting False after a simulated restart) instead observes True/send-called again -> RED.
"""
from __future__ import annotations

import atexit
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, ".")

from orchestrator import notify

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))


_TMPDIRS: list[Path] = []


def _mkdtemp() -> Path:
    d = Path(tempfile.mkdtemp())
    _TMPDIRS.append(d)
    return d


atexit.register(lambda: [shutil.rmtree(d, ignore_errors=True) for d in _TMPDIRS])

_orig_dedup_file = notify._PLAN_LIMIT_DEDUP_FILE

claude_usage = {"limits": [{"label": "Weekly Sonnet tokens", "utilization": 0.85}]}
glm_usage = {"used": 850_000, "cap": 1_000_000, "pct": 0.85}


# ══════════════════ 1+2+3. persist on send, restart reload, per-provider dedup ══════════════════ #
_dir1 = _mkdtemp()
notify._PLAN_LIMIT_DEDUP_FILE = _dir1 / "sonnet_alert_dedup.json"
notify._dual_low_watermark_alerted = set()

try:
    with patch("orchestrator.notify.configured", return_value=True), \
         patch("orchestrator.notify.send", return_value=True) as mock_send:
        # AC1: first alert for 'claude' sends and persists to disk.
        sent_claude = notify.dual_low_watermark_alert("claude", claude_usage)
        check("first claude low-watermark alert sends", sent_claude is True)
        check("dedup file written after first send", notify._PLAN_LIMIT_DEDUP_FILE.exists())
        on_disk = json.loads(notify._PLAN_LIMIT_DEDUP_FILE.read_text(encoding="utf-8"))
        check("on-disk dual-watermark set contains 'claude'",
              "claude" in (on_disk.get("dual_low_watermark_alerted") or []), on_disk)

        # AC2: simulate a restart — in-memory set resets to empty, disk persists.
        notify._dual_low_watermark_alerted = set()
        sent_claude_again = notify.dual_low_watermark_alert("claude", claude_usage)
        check("post-restart same-provider alert refuses to re-fire (loaded from disk)",
              sent_claude_again is False, sent_claude_again)
        check("post-restart repeat never calls send again", mock_send.call_count == 1,
              mock_send.call_count)

        # AC3: a DIFFERENT provider still alerts even though 'claude' is already persisted.
        sent_glm = notify.dual_low_watermark_alert("glm", glm_usage)
        check("a different provider (glm) still alerts (dedup is per-provider)",
              sent_glm is True, sent_glm)
        check("glm alert actually called send", mock_send.call_count == 2, mock_send.call_count)

        on_disk_both = json.loads(notify._PLAN_LIMIT_DEDUP_FILE.read_text(encoding="utf-8"))
        persisted = set(on_disk_both.get("dual_low_watermark_alerted") or [])
        check("disk now records BOTH providers", persisted == {"claude", "glm"}, persisted)
finally:
    notify._PLAN_LIMIT_DEDUP_FILE = _orig_dedup_file
    notify._dual_low_watermark_alerted = set()


# ══════════════════════════ 4. reset clears per-provider / all-providers on disk ══════════════════ #
_dir2 = _mkdtemp()
notify._PLAN_LIMIT_DEDUP_FILE = _dir2 / "sonnet_alert_dedup.json"
notify._dual_low_watermark_alerted = set()

try:
    with patch("orchestrator.notify.configured", return_value=True), \
         patch("orchestrator.notify.send", return_value=True):
        notify.dual_low_watermark_alert("claude", claude_usage)
        notify.dual_low_watermark_alert("glm", glm_usage)

        notify.reset_dual_low_watermark_alert("claude")
        on_disk = json.loads(notify._PLAN_LIMIT_DEDUP_FILE.read_text(encoding="utf-8"))
        persisted = set(on_disk.get("dual_low_watermark_alerted") or [])
        check("per-provider reset removes only 'claude' from disk", persisted == {"glm"}, persisted)
        check("per-provider reset removes 'claude' from memory too",
              "claude" not in notify._dual_low_watermark_alerted, notify._dual_low_watermark_alerted)

        notify.reset_dual_low_watermark_alert(None)
        on_disk2 = json.loads(notify._PLAN_LIMIT_DEDUP_FILE.read_text(encoding="utf-8"))
        check("reset(None) empties the on-disk dual-watermark set",
              (on_disk2.get("dual_low_watermark_alerted") or []) == [], on_disk2)
        check("reset(None) empties the in-memory set too",
              notify._dual_low_watermark_alerted == set(), notify._dual_low_watermark_alerted)
finally:
    notify._PLAN_LIMIT_DEDUP_FILE = _orig_dedup_file
    notify._dual_low_watermark_alerted = set()


# ══════════════ 5. both dedup domains coexist in the SAME sidecar without clobbering ═══════════════ #
_dir3 = _mkdtemp()
notify._PLAN_LIMIT_DEDUP_FILE = _dir3 / "sonnet_alert_dedup.json"
notify._dual_low_watermark_alerted = set()
notify._plan_limit_alert_sent = False
notify._plan_limit_alert_episode = None

try:
    with patch("orchestrator.notify.configured", return_value=True), \
         patch("orchestrator.notify.send", return_value=True):
        # Write the plan-limit domain first.
        notify.plan_limit_alert(
            [{"key": "weekly_sonnet", "label": "Weekly Sonnet tokens"}],
            ["Fri Jul 18 00:00 UTC"],
        )
        after_plan = json.loads(notify._PLAN_LIMIT_DEDUP_FILE.read_text(encoding="utf-8"))
        check("plan-limit write sets plan_limit_alert_sent",
              after_plan.get("plan_limit_alert_sent") is True, after_plan)

        # Now write the dual-watermark domain and confirm the plan-limit keys survive.
        notify.dual_low_watermark_alert("claude", claude_usage)
        after_dual = json.loads(notify._PLAN_LIMIT_DEDUP_FILE.read_text(encoding="utf-8"))
        check("dual-watermark write preserves the co-located plan_limit_alert_sent key",
              after_dual.get("plan_limit_alert_sent") is True, after_dual)
        check("dual-watermark write preserves the co-located episode key",
              after_dual.get("episode") == after_plan.get("episode"), after_dual)
        check("dual-watermark write also lands its own key",
              "claude" in (after_dual.get("dual_low_watermark_alerted") or []), after_dual)

        # And the reverse: writing plan-limit again must not erase the dual-watermark key.
        notify.reset_plan_limit_alert()
        after_reset = json.loads(notify._PLAN_LIMIT_DEDUP_FILE.read_text(encoding="utf-8"))
        check("plan-limit reset preserves the co-located dual-watermark key",
              "claude" in (after_reset.get("dual_low_watermark_alerted") or []), after_reset)
finally:
    notify._PLAN_LIMIT_DEDUP_FILE = _orig_dedup_file
    notify._dual_low_watermark_alerted = set()
    notify._plan_limit_alert_sent = False
    notify._plan_limit_alert_episode = None


# ══════════════════ 6. missing / corrupt file reads as an empty set (fail-open) ═══════════════════ #
_dir4 = _mkdtemp()
missing_file = _dir4 / "does_not_exist.json"
notify._PLAN_LIMIT_DEDUP_FILE = missing_file
try:
    check("missing file reads as an empty dual-watermark set",
          notify._dual_watermark_dedup_read() == set())

    corrupt_file = _dir4 / "corrupt.json"
    corrupt_file.write_text("{not valid json", encoding="utf-8")
    notify._PLAN_LIMIT_DEDUP_FILE = corrupt_file
    check("corrupt file reads as an empty dual-watermark set (fail-open, never raises)",
          notify._dual_watermark_dedup_read() == set())
finally:
    notify._PLAN_LIMIT_DEDUP_FILE = _orig_dedup_file


# ═══════════ 7. EU-390: per-provider reset after a restart must not wipe the other provider ═══════ #
_dir5 = _mkdtemp()
notify._PLAN_LIMIT_DEDUP_FILE = _dir5 / "sonnet_alert_dedup.json"
notify._dual_low_watermark_alerted = set()

try:
    with patch("orchestrator.notify.configured", return_value=True), \
         patch("orchestrator.notify.send", return_value=True) as mock_send:
        # Persist BOTH providers to disk, then simulate a restart (in-memory set empties).
        notify.dual_low_watermark_alert("claude", claude_usage)
        notify.dual_low_watermark_alert("glm", glm_usage)
        notify._dual_low_watermark_alerted = set()

        # AC: reset('claude') on the fresh process must drop only 'claude' — before EU-390 it
        # persisted the empty in-memory set verbatim, erasing 'glm' from disk as collateral.
        notify.reset_dual_low_watermark_alert("claude")
        on_disk = json.loads(notify._PLAN_LIMIT_DEDUP_FILE.read_text(encoding="utf-8"))
        persisted = set(on_disk.get("dual_low_watermark_alerted") or [])
        check("post-restart per-provider reset: 'glm' survives on disk",
              persisted == {"glm"}, persisted)

        # And the surviving dedup still holds: a glm alert must refuse to re-fire.
        sends_before = mock_send.call_count
        sent_glm = notify.dual_low_watermark_alert("glm", glm_usage)
        check("post-restart-reset glm alert still deduped (no re-fire)",
              sent_glm is False and mock_send.call_count == sends_before,
              (sent_glm, mock_send.call_count))

        # While 'claude' — the provider actually reset — is free to alert again.
        sent_claude = notify.dual_low_watermark_alert("claude", claude_usage)
        check("the reset provider (claude) may alert again", sent_claude is True, sent_claude)
finally:
    notify._PLAN_LIMIT_DEDUP_FILE = _orig_dedup_file
    notify._dual_low_watermark_alerted = set()


print("\n============ EU-211 DUAL-WATERMARK PERSIST QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
