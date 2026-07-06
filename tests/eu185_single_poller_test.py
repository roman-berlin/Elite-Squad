"""EU-185 (Wave 0) — single Telegram-poller election.

Telegram getUpdates+offset is single-consumer: two hosts polling one bot token split/lose the
Commander's messages. The hosts share state only via periodic git sync, so a live lock file can't
give real-time mutual exclusion — decisions.should_poll_telegram() elects ONE poller host instead.

Pinned:
  1. not configured (no token) → never polls.
  2. configured + this host == telegram_poller_host → polls.
  3. configured + this host != poller → does NOT poll (cockpit-only).
  4. GENERAL_TELEGRAM_POLLER=1 forces polling on a non-poller host (single-host escape hatch);
     =0 forces it off even on the poller host.
  5. cfg.telegram_poller_host is honoured (not hard-coded to "server").
"""
import os
import sys
import types

sys.path.insert(0, ".")

from orchestrator import decisions, notify, sync   # noqa: E402

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


class _Cfg:
    def __init__(self, poller="server"):
        self.telegram_poller_host = poller
        self.audit_path = "/tmp/x/audit.jsonl"


_orig_configured = notify.configured
_orig_host_id = sync.host_id
_orig_env = os.environ.get("GENERAL_TELEGRAM_POLLER")


def _set(configured=True, host="server", env=None):
    notify.configured = lambda: configured
    sync.host_id = lambda cfg=None: host
    if env is None:
        os.environ.pop("GENERAL_TELEGRAM_POLLER", None)
    else:
        os.environ["GENERAL_TELEGRAM_POLLER"] = env


try:
    # 1. not configured → never polls
    _set(configured=False, host="server")
    poll, why = decisions.should_poll_telegram(_Cfg())
    chk("not configured → no poll", poll is False and "configured" in why, why)

    # 2. configured + this host IS the poller → polls
    _set(configured=True, host="server")
    poll, why = decisions.should_poll_telegram(_Cfg("server"))
    chk("poller host → polls", poll is True and "server" in why, why)

    # 3. configured + this host is NOT the poller → cockpit-only
    _set(configured=True, host="mac")
    poll, why = decisions.should_poll_telegram(_Cfg("server"))
    chk("non-poller host → no poll (cockpit-only)", poll is False and "not the poller" in why, why)

    # 4a. env override: force ON on a non-poller host
    _set(configured=True, host="mac", env="1")
    poll, why = decisions.should_poll_telegram(_Cfg("server"))
    chk("GENERAL_TELEGRAM_POLLER=1 forces polling on a non-poller host", poll is True, why)

    # 4b. env override: force OFF on the poller host
    _set(configured=True, host="server", env="0")
    poll, why = decisions.should_poll_telegram(_Cfg("server"))
    chk("GENERAL_TELEGRAM_POLLER=0 forces OFF even on the poller host", poll is False, why)

    # 4c. override never overrides "not configured"
    _set(configured=False, host="server", env="1")
    poll, why = decisions.should_poll_telegram(_Cfg("server"))
    chk("override does not force polling when unconfigured", poll is False, why)

    # 5. custom poller host is honoured
    _set(configured=True, host="mac")
    poll, why = decisions.should_poll_telegram(_Cfg("mac"))
    chk("custom telegram_poller_host honoured (mac elected)", poll is True and "mac" in why, why)
finally:
    notify.configured = _orig_configured
    sync.host_id = _orig_host_id
    if _orig_env is None:
        os.environ.pop("GENERAL_TELEGRAM_POLLER", None)
    else:
        os.environ["GENERAL_TELEGRAM_POLLER"] = _orig_env

passed = sum(1 for _, ok, _ in results if ok)
print(f"\neu185_single_poller_test: {passed}/{len(results)} passed")
for n, ok, det in results:
    if not ok:
        print(f"  FAIL: {n}" + (f" — {det}" if det else ""))
sys.exit(0 if passed == len(results) else 1)
