"""EU-181 — cross-machine cockpit sync (2026-07-07). Make the Mac cockpit MIRROR the VPS's runs.

The VPS runs sync pull-only (no git push creds), so it never publishes its own audit to the unit-state
git branch — the Mac never sees the server and the two cockpits diverge. sync.pull_server_audit scp's
the server's audit.jsonl down over the SAME SSH bridge as UNIT.live.md, landing it in the machine-LOCAL
<audit-dir>/shared/<server>.jsonl (gitignored, NOT the .unit-state git clone). dashboard.audit_lines
reads <audit-dir>/shared/*, so the Mac cockpit shows the server's runs — and because the file is never
in the git clone, it can NEVER be re-published to the shared branch (no tracking / reset --hard / exclude
hazard, no server double-count).

This harness pins the contract END-TO-END. Two prior adversarial fleets shaped it: the first caught that
an earlier version wrote where the cockpit never read (dead mirror, vacuous test); the second caught that
writing into the git clone was fragile against an already-tracked peer file. Written fail-first: the
end-to-end mirror check goes RED if the write path and the dashboard read path ever diverge again.
"""
import sys, types, os, tempfile
from pathlib import Path

REPO = "."
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, REPO)

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))

from orchestrator import sync, dashboard
from orchestrator.config import Config

_ENVK = ("GENERAL_SERVER_SSH", "GENERAL_SYNC_PULL_ONLY", "GENERAL_SERVER_HOST_ID",
         "GENERAL_SERVER_REPO", "GENERAL_SERVER_AUDIT")
def _clear_env():
    for k in _ENVK:
        os.environ.pop(k, None)

def _cfg_under_state():
    """A Config whose audit.jsonl lives under a state/ subdir — the real layout, where sync._repo_root
    strips 'state' (so the git shared/ is <root>/.unit-state/shared) while the LOCAL shared/ the pulled
    server audit lands in is <root>/state/shared."""
    base = Path(tempfile.mkdtemp()).resolve()
    (base / "state").mkdir(parents=True, exist_ok=True)
    cfg = Config(apps=[])
    cfg.audit_path = str(base / "state" / "audit.jsonl")
    Path(cfg.audit_path).write_text('{"event":"local_build","ticket_id":"EU-1"}\n', encoding="utf-8")
    return cfg, base

def _stub_scp(payload: str):
    def _run(args, **kw):
        Path(args[-1]).write_text(payload, encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stderr="", stdout="")
    return _run

check("sync.pull_server_audit exists", hasattr(sync, "pull_server_audit"))

if hasattr(sync, "pull_server_audit"):
    _orig_run = sync.subprocess.run

    # ── guards ────────────────────────────────────────────────────────────────────────────────────
    _clear_env()
    check("no-op when GENERAL_SERVER_SSH unset", sync.pull_server_audit(_cfg_under_state()[0]).get("attempted") is False)
    os.environ["GENERAL_SERVER_SSH"] = "ubuntu@1.2.3.4"; os.environ["GENERAL_SYNC_PULL_ONLY"] = "1"
    check("no-op on the server itself (pull-only)", sync.pull_server_audit(_cfg_under_state()[0]).get("attempted") is False)
    _clear_env()

    # ── END-TO-END: the pulled server audit is actually READ by the cockpit ───────────────────────
    os.environ["GENERAL_SERVER_SSH"] = "ubuntu@1.2.3.4"
    cfg, base = _cfg_under_state()
    sync.subprocess.run = _stub_scp('{"event":"server_ship","ticket_id":"EU-42"}\n')
    try:
        res = sync.pull_server_audit(cfg)
    finally:
        sync.subprocess.run = _orig_run
    check("attempted + pulled off the server", res.get("attempted") and res.get("pulled"), str(res))
    dst = base / "state" / "shared" / "server.jsonl"
    check("wrote to the LOCAL gitignored <audit-dir>/shared/server.jsonl", dst.exists(), str(dst))
    check("NEVER writes into the .unit-state git clone (no tracking hazard)",
          not (base / ".unit-state").exists() or not list((base / ".unit-state").rglob("server.jsonl")))
    merged = "\n".join(dashboard.audit_lines(cfg.audit_path))
    check("MIRROR: cockpit reads the local audit", "local_build" in merged)
    check("MIRROR: cockpit reads the pulled SERVER audit (EU-42)", "server_ship" in merged and "EU-42" in merged, merged[:200])
    _clear_env()

    # ── sanitisation: a hostile GENERAL_SERVER_HOST_ID cannot escape shared/ ──────────────────────
    os.environ["GENERAL_SERVER_SSH"] = "ubuntu@1.2.3.4"
    os.environ["GENERAL_SERVER_HOST_ID"] = "My Server/../east"   # spaces, slash, dot-dot
    cfg2, base2 = _cfg_under_state()
    sync.subprocess.run = _stub_scp('{"event":"srv"}\n')
    try:
        sync.pull_server_audit(cfg2)
    finally:
        sync.subprocess.run = _orig_run
    shared2 = base2 / "state" / "shared"
    files = list(shared2.glob("*.jsonl")) if shared2.is_dir() else []
    check("hostile host id sanitised to a single flat shared/<safe>.jsonl (no path escape)",
          len(files) == 1 and "/" not in files[0].name and " " not in files[0].name, str(files))
    check("no file escaped the shared/ dir via '../'", not (base2 / "state" / "east.jsonl").exists()
          and not (base2 / "east.jsonl").exists())
    _clear_env()

    # ── never fatal: an scp/FS failure degrades to a reported error, never raises ─────────────────
    os.environ["GENERAL_SERVER_SSH"] = "ubuntu@1.2.3.4"
    def _boom(args, **kw): raise OSError("scp blew up")
    sync.subprocess.run = _boom
    try:
        r = sync.pull_server_audit(_cfg_under_state()[0]); crashed = False
    except Exception:
        r, crashed = None, True
    finally:
        sync.subprocess.run = _orig_run
    check("scp/FS failure degrades to a reported error (never raises)", (not crashed) and r is not None and r.get("error"))
    _clear_env()

    # ── git_sync stages only the host's OWN file (never re-publishes a peer/server file) ──────────
    src = Path("orchestrator/sync.py").read_text()
    check("git_sync stages only shared/<own-host>.jsonl (not `git add shared`)",
          'add", f"shared/{host_id(cfg)}.jsonl"' in src and '_git(sd, "add", "shared")' not in src)

print("\n============ EU-181 CROSS-MACHINE SYNC QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
