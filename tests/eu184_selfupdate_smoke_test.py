"""EU-184 (Wave 0) — the self-update smoke-test backstop.

On 2026-07-05 a `main` deploy that failed to import crash-looped the VPS ~864× in one minute
(Restart=always, no start-limit). The backstop is two-layered:
  • scripts/self-update.sh smoke-tests the new HEAD (imports the service's entry modules) BEFORE
    `systemctl restart`, and REVERTS the tree to the last-good HEAD if the import fails — so a
    broken push never deploys and never restarts into a loop;
  • the systemd unit gains StartLimitIntervalSec/StartLimitBurst (VPS_DEPLOYMENT.md) to bound a
    RUNTIME crash-loop the import test can't catch.

Pinned here:
  1. The invariant the smoke-test enforces — the service's entry modules import cleanly — is
     checked IN the suite too, so the dev gate catches a bad entry-module import before it reaches
     main (the exact crash this backstop exists for).
  2. self-update.sh actually contains the smoke-test + revert-on-failure (grep tripwire — the
     backstop can't be silently removed).
  3. VPS_DEPLOYMENT.md documents the StartLimitBurst backstop.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


# 1. the service's entry modules import cleanly (the exact smoke-test self-update.sh runs).
ENTRY = "import orchestrator.main, orchestrator.server, orchestrator.autopilot, orchestrator.loop, orchestrator.decisions"
r = subprocess.run([sys.executable, "-c", ENTRY], capture_output=True, text=True, cwd=str(ROOT))
chk("service entry modules import cleanly (self-update smoke-test invariant)",
    r.returncode == 0, (r.stderr or "").strip()[-400:])

# 2. self-update.sh carries the backstop logic.
su = (ROOT / "scripts" / "self-update.sh").read_text(encoding="utf-8")
chk("self-update.sh smoke-tests the new HEAD before restart",
    "SMOKE-TEST" in su and ENTRY in su, "smoke-test import line missing")
chk("self-update.sh reverts to the old HEAD when the smoke-test fails",
    'git reset --hard "$OLD"' in su, "revert-on-failure missing")
chk("self-update.sh only restarts AFTER a passing smoke-test",
    su.rindex("sudo systemctl restart general.service") > su.index("SMOKE-TEST FAILED"),
    "restart command not gated behind the smoke-test")

# 3. the systemd unit's start-limit backstop is documented.
vps = (ROOT / "VPS_DEPLOYMENT.md").read_text(encoding="utf-8")
chk("VPS_DEPLOYMENT.md documents StartLimitBurst / StartLimitIntervalSec",
    "StartLimitBurst" in vps and "StartLimitIntervalSec" in vps)
chk("VPS_DEPLOYMENT.md reminds to daemon-reload after editing the unit",
    "daemon-reload" in vps)

passed = sum(1 for _, ok, _ in results if ok)
print(f"\neu184_selfupdate_smoke_test: {passed}/{len(results)} passed")
for n, ok, det in results:
    if not ok:
        print(f"  FAIL: {n}" + (f" — {det}" if det else ""))
sys.exit(0 if passed == len(results) else 1)
