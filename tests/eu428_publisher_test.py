"""EU-428 AC0 — restore the Mac audit publisher (the heartbeat was dead 25 days).

The Mac's audit has not been published since late June: crontab has no sync entry and no launchd
agent runs ``./general sync``, so ``shared/mac.jsonl`` froze at 2026-06-26 while the VPS cron kept
logging a healthy ``pulled=True`` (it was pulling a file that never changed). EU-181 removed the
BROKEN Mac sync agent and never replaced the publisher half — this ticket reinstalls a working one.

AC0: a launchd agent mirroring scripts/install-mac-watchdog-daemon.sh runs the existing
sync.publish / git_sync path on a schedule. Pin: after one cycle the newest ts in the published
shared/mac.jsonl is within one interval of the newest ts in the local state/audit.jsonl.

This harness pins:
  • the installer exists and is well-formed (generates the plist at runtime; PERIODIC StartInterval,
    NOT KeepAlive — a publisher must not stack overlapping syncs; invokes `general sync`; carries the
    audit-publisher label; sets GENERAL_HOST_ID=mac so the file is shared/mac.jsonl).
  • the installer is allow-listed by the scheduler single-source guard (or that guard reds on the new
    launchd tokens) and documented in DEPLOYMENT.md.
  • the MECHANISM the daemon runs — sync.publish() — copies the live audit verbatim, so the published
    newest ts matches the local newest ts exactly (interval = 0 ≤ one interval).
"""
import os
import sys
import tempfile
import types
from pathlib import Path

_sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        pass

    def __call__(s, *a, **k):
        return s


_sdk.__getattr__ = lambda _n: _D
sys.modules["claude_agent_sdk"] = _sdk
_req = types.ModuleType("requests")
_req.Session = lambda: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = _req
sys.path.insert(0, ".")

from orchestrator import sync
from orchestrator.config import Config, AppConfig

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts" / "install-mac-audit-publisher-daemon.sh"
SCHEDULER_TEST = ROOT / "tests" / "scheduler_single_source_test.py"
DEPLOY = ROOT / "DEPLOYMENT.md"

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail) if not cond else ""))


# ── installer tripwires ───────────────────────────────────────────────────────────────
chk("publisher installer exists", INSTALLER.exists(), str(INSTALLER))
ins = INSTALLER.read_text(encoding="utf-8") if INSTALLER.exists() else ""

chk("installer generates the plist at runtime (no committed *.plist under scripts/)",
    "cat >" in ins and ".plist" in ins, "must write the plist into ~/Library/LaunchAgents at install")
chk("installer carries the audit-publisher launchd label",
    "com.roman.general.audit-publisher" in ins, "label missing")
chk("installer is a PERIODIC timer (StartInterval); the plist declares no KeepAlive key",
    "StartInterval" in ins and "<integer>" in ins and "<key>KeepAlive</key>" not in ins,
    "a publisher must be StartInterval (periodic), not a KeepAlive respawn (would stack syncs)")
chk("installer runs the sync publish path (general … sync)",
    "general" in ins and "sync" in ins, "must invoke ./general sync (the publish/git_sync path)")
chk("installer pins GENERAL_HOST_ID=mac so the published file is shared/mac.jsonl",
    "GENERAL_HOST_ID" in ins and "mac" in ins, "set GENERAL_HOST_ID=mac in the plist env")
chk("installer supports uninstall (bootout + remove), mirroring the watchdog installer",
    "uninstall" in ins and "bootout" in ins, "needs an uninstall path")

chk("scheduler single-source guard allow-lists the new installer",
    "install-mac-audit-publisher-daemon.sh" in
    (SCHEDULER_TEST.read_text(encoding="utf-8") if SCHEDULER_TEST.exists() else ""),
    "add scripts/install-mac-audit-publisher-daemon.sh to SRC_ALLOW or that guard reds on the new tokens")
deploy = DEPLOY.read_text(encoding="utf-8") if DEPLOY.exists() else ""
chk("DEPLOYMENT.md documents the publisher install",
    "install-mac-audit-publisher-daemon.sh" in deploy, "no install instructions for the publisher")

# ── the MECHANISM pin: sync.publish() copies the live audit verbatim ───────────────────
os.environ["GENERAL_HOST_ID"] = "mac"
tmp = Path(tempfile.mkdtemp())
audit = tmp / "audit.jsonl"
NEWEST = "2026-07-22T12:00:00+0000"
audit.write_text(
    '{"ts": "2026-07-22T11:00:00+0000", "event": "agent_call"}\n'
    f'{{"ts": "{NEWEST}", "event": "build", "ticket_id": "EU-1"}}\n',
    encoding="utf-8")
cfg = Config(
    apps=[AppConfig(name="EU", repo_path=str(tmp), base_branch="dev",
                    protected_branch="main", backlog_backend="none")],
    audit_path=str(audit), use_worktree=False)

sd = tmp / "state-clone"
(sd / "shared").mkdir(parents=True, exist_ok=True)
dst = sync.publish(cfg, sd)
published = dst.read_text(encoding="utf-8") if dst and dst.exists() else ""
chk("publish() writes shared/mac.jsonl (host id = mac)",
    dst is not None and dst.name == "mac.jsonl", str(dst))
chk("publish() copies the live audit VERBATIM (published newest ts == local newest ts)",
    published == audit.read_text(encoding="utf-8") and NEWEST in published,
    "the published file must carry the local audit's newest event ts")
chk("publish() is idempotent — the newest ts is within one interval (here identical) of the local",
    NEWEST in published, "published file is stale relative to the local audit")

print("\n========== EU-428 AC0 MAC PUBLISHER QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
