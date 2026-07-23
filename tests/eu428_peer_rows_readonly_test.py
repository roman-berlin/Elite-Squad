"""EU-428 AC3 — peer audit rows are READ-ONLY on the receiving host.

Once the Mac publishes its audit again (AC0), the VPS's ``dashboard.load_tasks`` merges the
synced peer rows (``_audit_paths`` globs ``shared/*.jsonl``), and ``forensics.py:88`` calls
``load_tasks(cfg.audit_path)``. ``forensics.scan`` is the root of every WRITE the forensics
subsystem makes — ``signature_sweep`` (cross-ticket crash-signature auto-filing) and
``_file_postmortem_ticket`` (per-ticket post-mortem filing) both read through it. If scan sees
peer rows, a crash that happened ON THE MAC can auto-file a ticket ON THE SERVER — a write on a
host that merely *received* the row.

AC3 pin: a synthetic peer crash row in ``shared/<peer>.jsonl`` produces ZERO filings on the host
that merely received it. Peer rows are display + liveness only.

This harness plants 3 peer crash rows (the signature_sweep threshold: >=3 occurrences across >=2
distinct tickets) with one stable crash signature, on a host whose OWN audit has no failures, and
proves:
  • display still works — load_tasks() (peer-inclusive default) DOES show the peer's failed run.
  • the write path is sealed — forensics.scan() (LOCAL-ONLY) excludes the peer row entirely.
  • no filing fires — signature_sweep() never calls the filer (_file_one) for peer evidence.
"""
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

# --- stubs so orchestrator.* imports offline (no SDK, no network) ---
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

# This harness plants its peer at shared/mac.jsonl, so it must NOT be running as host "mac" —
# `_audit_paths` deliberately drops shared/<own host_id>.jsonl (a host's own published file is a
# MIRROR of the audit.jsonl it already reads; merging it double-counts every local event). Without
# this pin the harness inherits GENERAL_HOST_ID from the ambient shell: it passed standalone but
# failed under run_all whenever the suite was launched from a shell that had sourced .env
# (GENERAL_HOST_ID=mac), making its "peer" self-referential. The scenario under test is "we are the
# SERVER, receiving the MAC's rows", so say so explicitly rather than depending on the environment.
os.environ["GENERAL_HOST_ID"] = "server"

from orchestrator import dashboard as D
from orchestrator import forensics
from orchestrator.config import Config, AppConfig

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail) if not cond else ""))


# --- a host (the VPS) whose OWN audit is clean: no failed runs locally ---
tmp = Path(tempfile.mkdtemp())
audit = tmp / "audit.jsonl"
# one local SUCCESS run only — the host itself built nothing that failed
audit.write_text(
    json.dumps({"ts": "2026-07-22T08:00:00+0000", "event": "ticket_start",
                "ticket_id": "EU-LOCAL-OK", "app": "EU"}) + "\n"
    + json.dumps({"ts": "2026-07-22T08:01:00+0000", "event": "merged",
                  "ticket_id": "EU-LOCAL-OK", "app": "EU"}) + "\n",
    encoding="utf-8")

cfg = Config(
    apps=[AppConfig(name="EU", repo_path=str(tmp), base_branch="dev",
                    protected_branch="main", backlog_backend="none")],
    audit_path=str(audit), use_worktree=False, postmortem_after=3)

# --- the PEER's published audit (shared/mac.jsonl): 3 crashes, 2 tickets, ONE signature ---
# _audit_paths probes <audit-dir>/shared/*.jsonl, so this is exactly where a synced peer lands.
# Each occurrence gets a DISTINCT ts: audit_lines collapses EXACT-duplicate lines, so identical
# (event,ts,tid) lines would merge and drop the count below signature_sweep's >=3 threshold — a
# vacuous pass. Distinct ts keeps 3 genuine failed runs across 2 distinct tickets alive.
shared = tmp / "shared"
shared.mkdir(parents=True, exist_ok=True)
peer = shared / "mac.jsonl"
base = time.time()
peer_rows = []
for i, tid in enumerate(("EU-PEER-1", "EU-PEER-1", "EU-PEER-2")):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime(base + i))
    peer_rows.append(json.dumps({"ts": ts, "event": "ticket_start",
                                 "ticket_id": tid, "app": "EU"}))
    peer_rows.append(json.dumps({"ts": ts, "event": "ticket_exception",
                                 "ticket_id": tid, "app": "EU",
                                 "error": "Traceback (most recent call last): RuntimeError: boom"}))
peer.write_text("\n".join(peer_rows) + "\n", encoding="utf-8")

# ── 1. DISPLAY still works: the default (peer-inclusive) load_tasks shows the peer crash ──
tasks_all = D.load_tasks(cfg.audit_path)
peer_tids = {t.get("ticket_id") for t in tasks_all}
chk("display: load_tasks (peer-inclusive) still merges the peer's failed runs",
    "EU-PEER-1" in peer_tids and "EU-PEER-2" in peer_tids,
    f"peer rows missing from display view; got {peer_tids}")
peer_run = next((t for t in tasks_all if t.get("ticket_id") == "EU-PEER-1"), None)
chk("display: the peer's run is marked failed (errored)",
    peer_run is not None and peer_run.get("outcome") == "errored",
    f"peer run outcome={peer_run.get('outcome') if peer_run else None}")

# ── 2. THE WRITE PATH IS SEALED: forensics.scan is LOCAL-ONLY — peer row excluded ───────
scan_rows = forensics.scan(cfg)
scan_tids = {r.get("ticket_id") for r in scan_rows}
chk("forensics.scan (local-only) excludes the peer's crash rows entirely",
    not (scan_tids & {"EU-PEER-1", "EU-PEER-2"}),
    f"peer crash rows leaked into scan (would drive filing): {scan_tids}")
chk("forensics.scan (local-only) also drops the host's own SUCCESS (nothing failed locally)",
    "EU-LOCAL-OK" not in scan_tids, f"scan={scan_tids}")

# ── 3. NO FILING: signature_sweep never calls the filer for peer-only evidence ─────────
# Monkeypatch the single chokepoint every filing goes through; count calls. With scan local-only
# the sweep's failure groups are empty (the host has no local failures), so _file_one is never
# reached — the peer's 3 identical crashes can NEVER auto-file on this host.
filed_calls: list[str] = []
_orig_file_one = forensics._file_one


def _spy_file_one(app_cfg, label, proposal, audit=None):
    filed_calls.append(f"{label}:{proposal.get('title', '')[:60]}")
    return None  # don't really file


forensics._file_one = _spy_file_one
try:
    new_keys = forensics.signature_sweep(cfg)
finally:
    forensics._file_one = _orig_file_one

chk("signature_sweep files NOTHING from peer-only crash evidence",
    filed_calls == [] and new_keys == [],
    f"filed_calls={filed_calls} new_keys={new_keys}")

# ── 4. SELF-CONSISTENCY: if those same crashes were LOCAL, the sweep WOULD fire ─────────
# This proves the seal is on the peer/local axis, not a broken sweep: move the crashes into the
# host's own audit and the filer IS reached (so AC3 isn't passing because the test is toothless).
# Clear the sig-filing ledger first — on UNCHANGED code assertion 3's sweep already filed+marked
# this signature from the peer rows, which would suppress the re-run and fake a pass here.
(tmp / "signature_filed.json").unlink(missing_ok=True)
audit.write_text(("\n".join(peer_rows)) + "\n", encoding="utf-8")
# clear the per-file line cache + tasks cache so the rewritten local audit is re-read
D._file_line_cache.clear()
D._audit_cache.clear()
D._tasks_cache.clear()
filed_calls2: list[str] = []
forensics._file_one = lambda app_cfg, label, proposal, audit=None: filed_calls2.append(label) or None
try:
    forensics.signature_sweep(cfg)
finally:
    forensics._file_one = _orig_file_one
chk("tooth-check: the same crashes DO fire the sweep when they are LOCAL (not peer)",
    len(filed_calls2) >= 1, f"local crashes did not fire sweep — test lacks teeth: {filed_calls2}")

# ── report ───────────────────────────────────────────────────────────────────────────
print("\n========== EU-428 AC3 PEER-ROWS-READ-ONLY QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("-------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
