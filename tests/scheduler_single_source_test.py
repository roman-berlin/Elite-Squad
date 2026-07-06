"""Scheduler single-source QA (EU-56 part a): the retired Mac launchd `*.plist` launch agents and their
`run-*.sh` wrappers were loaded by nothing (the live scheduler is `scripts/install-server-cron.sh`).
They were a drift trap — editing one schedule silently missed the other. This guard fails if the dead
plists/wrappers ever drift back, if any source file resurrects a launchctl/LaunchAgents/.plist reference,
if the surviving single source loses the council/small-talk/patrol cadence it now solely owns, or if any
repo doc re-acquires a stale `com.roman.general.*` / `launchctl` instruction (EU-56 iter-3 doc-reality)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# 1) The retired launchd plists and their run-*.sh wrappers must be gone (can't drift back).
RETIRED = [
    "com.roman.general.autopilot.plist", "com.roman.general.council.plist",
    "com.roman.general.patrol.plist", "com.roman.general.smalltalk.plist",
    "com.roman.general.sync.plist",
    "run-autopilot.sh", "run-council.sh", "run-patrol.sh", "run-smalltalk.sh", "run-sync.sh",
]
for name in RETIRED:
    chk(f"retired scheduler file deleted: scripts/{name}", not (SCRIPTS / name).exists())

# 2) No leftover *.plist anywhere under scripts/ (catches a renamed/forgotten copy).
stray_plists = sorted(p.name for p in SCRIPTS.glob("*.plist"))
chk("no stray *.plist remains under scripts/", not stray_plists, f"found={stray_plists}")

# 3) No source file resurrects a launchctl / LaunchAgents / .plist reference (a dangling loader).
#    The NEW keepalive install script (install-mac-autopilot-daemon.sh) legitimately writes the
#    com.roman.general.autopilot-keepalive plist and calls launchctl — it is explicitly allow-listed
#    here because it is the CURRENT daemon installer, not a retired scheduler.
#    EU-120: autopilot.py's _stop_launchd_daemon() legitimately uses launchctl to stop the keepalive
#    daemon (Finish & stop graceful shutdown). server.py has a comment about launchctl failure.
NEEDLES = ("launchctl", "LaunchAgents", "com.roman.general", ".plist")
SRC_ALLOW = {"scripts/install-mac-autopilot-daemon.sh", "orchestrator/autopilot.py", "orchestrator/server.py"}
offenders = []
for p in list(ROOT.glob("orchestrator/**/*.py")) + list(SCRIPTS.glob("*.sh")):
    rel = p.relative_to(ROOT).as_posix()
    if rel in SRC_ALLOW:
        continue
    text = p.read_text(encoding="utf-8", errors="ignore")
    if any(n in text for n in NEEDLES):
        offenders.append(rel)
chk("no orchestrator/scripts source references the retired launchd scheduler",
    not offenders, f"offenders={offenders}")

# 4) The single surviving source still owns the full cadence it absorbed.
cron = (SCRIPTS / "install-server-cron.sh")
chk("live scheduler scripts/install-server-cron.sh still present", cron.exists())
cron_text = cron.read_text(encoding="utf-8") if cron.exists() else ""
# Phase-2 §2 (2026-07-06): the ceremony crons were RETIRED — councils/small-talk are
# on-demand only. The single source must NOT quietly re-schedule them.
chk("ceremonies de-cronned: no scheduled council muster", "./general council" not in cron_text)
chk("ceremonies de-cronned: no scheduled corridor small-talk", "./general smalltalk" not in cron_text)
chk("single source still schedules the weekly patrol (Mon)", "0 9 * * 1" in cron_text
    and "./general patrol" in cron_text)

# 5) Doc-reality (EU-56 iter-3): no repo doc may carry an *actionable* reference to the retired Mac
#    launchd scheduler — a `com.roman.general.*` agent name, a `run-*.sh` wrapper, or a
#    `launchctl`/`LaunchAgents` load step. VPS_DEPLOYMENT / QA_MANUAL / ROADMAP / ORG were corrected to
#    point at the cron single source (`scripts/install-server-cron.sh`); this fails if that wording drifts
#    back. The two docs that *document the retirement itself* are allow-listed — they legitimately describe
#    the agents as removed (SYSTEM_OVERVIEW = the corrected map; UNIT_REVIEW = the point-in-time finding).
DOC_DIRS = [ROOT, ROOT / "Documentation"]
#    Development_Status.md is the machine-written changelog (loop._record_changelog appends every
#    land's verify hint verbatim) — historical entries legitimately mention launchctl work such as
#    EU-119/EU-120 (the allow-listed keepalive daemon), and a changelog entry is a record, not an
#    actionable scheduler instruction. Without this allowance the unit poisons its own gate on the
#    next launchd-adjacent land (this exact failure burned EU-173/EU-174 on 2026-07-01).
#    RESTRUCTURE_PROPOSAL_2026-07-05.md is a point-in-time audit/postmortem (same class as
#    UNIT_REVIEW): it documents the launchctl-needle incident itself and names the keepalive
#    log files — records, not scheduler instructions.
DOC_ALLOW = {"Documentation/SYSTEM_OVERVIEW.md", "Documentation/UNIT_REVIEW_2026-06-25.md",
             "Documentation/Development_Status.md",
             "Documentation/RESTRUCTURE_PROPOSAL_2026-07-05.md",
             # Point-in-time audit/postmortem (same class as UNIT_REVIEW / RESTRUCTURE_PROPOSAL):
             # it DOCUMENTS the EU-181 dead-launchd-agent finding and recommends `launchctl bootout`
             # to REMOVE them — a record of the retirement, not an instruction to run the scheduler.
             "Documentation/SYSTEM_AUDIT_2026-07-06.md"}
DOC_NEEDLES = ("com.roman.general", "launchctl", "LaunchAgents",
               "run-autopilot.sh", "run-council.sh", "run-patrol.sh", "run-smalltalk.sh", "run-sync.sh")
doc_offenders = []
for d in DOC_DIRS:
    for p in sorted(d.glob("*.md")):
        rel = p.relative_to(ROOT).as_posix()
        if rel in DOC_ALLOW:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        hits = sorted(n for n in DOC_NEEDLES if n in text)
        if hits:
            doc_offenders.append(f"{rel} → {','.join(hits)}")
chk("no repo doc references the retired launchd plists/run-*.sh (corrected schedule can't drift back)",
    not doc_offenders, f"offenders={doc_offenders}")

print("\n========= SCHEDULER SINGLE-SOURCE QA =========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
