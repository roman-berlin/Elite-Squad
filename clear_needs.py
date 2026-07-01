"""One-shot Needs-you inbox cleanup (2026-06-29).

Dismisses the stale pending-decision backlog and declines the stale specialist roster,
using the unit's own locked APIs. Pure inbox cleanup — no builds are triggered. Run once
from the repo root:

    cd ~/Projects/General && .venv/bin/python clear_needs.py

All 17 pending decisions are stale "file this out-of-scope finding?" prompts whose
findings are already on the board as their referenced tickets (the one genuine unfiled
finding — the cockpit liveness heartbeat bug — was filed first as EU-126). The EU-109
specialist roster is moot because EU-109 already merged. Safe to delete this file after.

The 2 parked tickets (AUTO-23, EU-17) are handled separately with `./general unblock`,
because unblocking them makes the autopilot retry-build them.
"""
from __future__ import annotations

import json
import pathlib

from orchestrator.config import Config
from orchestrator import decisions

cfg = Config.load("config.yaml")

# 1. Stale pending decisions -> dismiss each (no Jira echo: comment=False).
pending = decisions.load(cfg) or []
ids = [d["id"] for d in pending]
answer = (
    "Bulk Needs-you cleanup 2026-06-29: stale out-of-scope prompt — the finding is "
    "already on the board as its referenced ticket (or was filed standalone as EU-126). "
    "Dismissed."
)
for tid in ids:
    decisions.resolve(cfg, answer, ticket_id=tid, comment=False)
print(f"cleared {len(ids)} stale decision(s): {ids}")

# 2. Stale specialist roster for EU-109 (already merged) -> decline.
sp_path = pathlib.Path(cfg.audit_path).with_name("pending_specialist_approvals.json")
try:
    sp = json.loads(sp_path.read_text(encoding="utf-8"))
    if isinstance(sp, dict) and sp:
        before = len(sp)
        sp = {
            k: v
            for k, v in sp.items()
            if str((v or {}).get("ticket_id") or k).upper() != "EU-109"
        }
        sp_path.write_text(json.dumps(sp), encoding="utf-8")
        print(f"specialist rosters: {before} -> {len(sp)} (declined stale EU-109)")
except FileNotFoundError:
    pass

print("Needs-you inbox cleared. Parked tickets: run  ./general unblock  to retry them.")
