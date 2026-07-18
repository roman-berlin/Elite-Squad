"""EU-386 (EU-224b) — dirty-tree respawn forensics + warning.

An accidental restart (crash / KeepAlive respawn) onto uncommitted or stray changes used to be
silent: ``process_start`` recorded sha+pid only, so nothing distinguished a clean-tree boot from
one running unreviewed code. Under test: ``tree_forensics()`` sources dirty/modified_paths from
``git status --porcelain``; the drain's ``process_start`` audit event (autopilot.py) carries the
additive ``dirty``/``modified_paths`` fields; a dirty boot fires a LOUD console warning plus a
Telegram message plus a ``dirty_tree_start`` audit line — a clean boot produces none of those.
The serve-side boot takes the same warning through main.py's serve branch (server.serve()'s own
process_start line is another surface — pinned structurally here).

Offline — the SDK is stubbed, git is faked via a subprocess.run dispatcher; no network.
"""
import asyncio
import contextlib
import io
import json
import re
import subprocess
import sys
import tempfile
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import autopilot as ap
from orchestrator import notify
from orchestrator.config import Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


_REAL_RUN = subprocess.run

def _fake_git(status_stdout):
    """A subprocess.run dispatcher: fake `git status --porcelain` + `git rev-parse`, pass
    everything else (ps, etc.) through to the real thing so the harness stays honest."""
    def _run(cmd, *a, **k):
        if isinstance(cmd, list) and cmd[:2] == ["git", "status"]:
            return types.SimpleNamespace(stdout=status_stdout, stderr="", returncode=0)
        if isinstance(cmd, list) and cmd[:2] == ["git", "rev-parse"]:
            return types.SimpleNamespace(stdout="abc1234\n", stderr="", returncode=0)
        return _REAL_RUN(cmd, *a, **k)
    return _run


def _events(path):
    try:
        return [json.loads(ln) for ln in Path(path).read_text().splitlines() if ln.strip()]
    except OSError:
        return []


_orig_send = notify.send
_orig_pid = ap._PID_FILE
_orig_alert = ap._alert_unclean_restart
_orig_stdout = sys.stdout

# ── §1 tree_forensics parses porcelain (unit) ───────────────────────────────────────────
subprocess.run = _fake_git(" M orchestrator/loop.py\n?? stray.txt\nA  tests/new_test.py\n")
try:
    dirty, paths = ap.tree_forensics()
finally:
    subprocess.run = _REAL_RUN
chk("dirty tree: flag is True", dirty is True, str((dirty, paths)))
chk("dirty tree: paths are extracted from porcelain",
    paths == ["orchestrator/loop.py", "stray.txt", "tests/new_test.py"], str(paths))

subprocess.run = _fake_git("")
try:
    dirty, paths = ap.tree_forensics()
finally:
    subprocess.run = _REAL_RUN
chk("clean tree: (False, [])", dirty is False and paths == [], str((dirty, paths)))

def _boom_run(cmd, *a, **k):
    raise OSError("git is gone")
subprocess.run = _boom_run
try:
    dirty, paths = ap.tree_forensics()
finally:
    subprocess.run = _REAL_RUN
chk("probe failure: forensics fails soft to (False, [])", dirty is False and paths == [])

# ── §2 drain boot on a DIRTY tree: process_start fields + loud warning ──────────────────
# eu175's trick: _alert_unclean_restart (the first statement of the try) booms, so the run
# stops right after the pre-try forensics block — fast, deterministic, and exactly the
# surface under test (process_start + the warning fire BEFORE the loop's setup).
def _boom_setup(_audit):
    raise RuntimeError("stop after the forensics block")

sent = []
notify.send = lambda *a, **k: sent.append(a[0] if a else "") or True
ap._alert_unclean_restart = _boom_setup
subprocess.run = _fake_git(" M orchestrator/loop.py\n?? stray.txt\n")
tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[], audit_path=str(tmp / "audit.jsonl"))
ap._PID_FILE = tmp / "ap.pid"
buf = io.StringIO()
try:
    with contextlib.redirect_stdout(buf):
        try:
            asyncio.run(ap.autopilot(cfg, "myapp", once=True))
        except RuntimeError:
            pass
finally:
    subprocess.run = _REAL_RUN
    ap._alert_unclean_restart = _orig_alert
    sys.stdout = _orig_stdout       # autopilot wraps stdout in a _Tee — unwrap for the next section

evs = _events(cfg.audit_path)
ps = next((e for e in evs if e.get("event") == "process_start"), {})
chk("dirty boot: process_start carries dirty=True", ps.get("dirty") is True, str(ps))
chk("dirty boot: process_start carries modified_paths",
    ps.get("modified_paths") == ["orchestrator/loop.py", "stray.txt"], str(ps))
chk("dirty boot: sha/pid/role fields survive (additive, non-breaking)",
    ps.get("sha") == "abc1234" and ps.get("role") == "autopilot" and ps.get("app") == "myapp",
    str(ps))
chk("dirty boot: dirty_tree_start audit line recorded",
    any(e.get("event") == "dirty_tree_start" and e.get("role") == "autopilot" for e in evs),
    str([e.get("event") for e in evs]))
chk("dirty boot: Telegram warning sent (distinct DIRTY marker)",
    any("DIRTY" in s for s in sent), str(sent))
chk("dirty boot: loud console warning printed", "DIRTY" in buf.getvalue(), buf.getvalue()[:200])

# ── §3 drain boot on a CLEAN tree: fields present, NO warning of any kind ───────────────
sent = []
ap._alert_unclean_restart = _boom_setup
subprocess.run = _fake_git("")
tmp3 = Path(tempfile.mkdtemp())
cfg3 = Config(apps=[], audit_path=str(tmp3 / "audit.jsonl"))
ap._PID_FILE = tmp3 / "ap.pid"
buf3 = io.StringIO()
try:
    with contextlib.redirect_stdout(buf3):
        try:
            asyncio.run(ap.autopilot(cfg3, None, once=True))
        except RuntimeError:
            pass
finally:
    subprocess.run = _REAL_RUN
    ap._alert_unclean_restart = _orig_alert
    ap._PID_FILE = _orig_pid
    notify.send = _orig_send
    sys.stdout = _orig_stdout

evs3 = _events(cfg3.audit_path)
ps3 = next((e for e in evs3 if e.get("event") == "process_start"), {})
chk("clean boot: process_start carries dirty=False + empty modified_paths",
    ps3.get("dirty") is False and ps3.get("modified_paths") == [], str(ps3))
chk("clean boot: NO dirty_tree_start line",
    not any(e.get("event") == "dirty_tree_start" for e in evs3),
    str([e.get("event") for e in evs3]))
chk("clean boot: NO Telegram warning", not any("DIRTY" in s for s in sent), str(sent))
chk("clean boot: NO console warning", "DIRTY" not in buf3.getvalue(), buf3.getvalue()[:200])

# ── §4 serve boot takes the same path (structural — main.py's serve branch) ─────────────
# server.serve()'s own process_start emission is a separate surface (server.py); the boot
# warning + auto-resume are wired on the one line every cockpit boot takes: main.py serve.
_main_src = Path("orchestrator/main.py").read_text(encoding="utf-8")
_m = re.search(r'if args\.command == "serve":\n(.*?)\n\s+return 0', _main_src, re.S)
_block = _m.group(1) if _m else ""
# anchor on the CALL ("server.serve(cfg"), not the bare name — house comments may mention
# server.serve() in prose before the call and must not fool the structural check.
chk("main.py serve: dirty-tree warning wired before serve()",
    "warn_dirty_tree" in _block and "server.serve(cfg" in _block
    and _block.index("warn_dirty_tree") < _block.index("server.serve(cfg"),
    _block[:400])
chk("main.py serve: auto-resume (EU-385) wired before serve()",
    "resume_armed_drains" in _block and "server.serve(cfg" in _block
    and _block.index("resume_armed_drains") < _block.index("server.serve(cfg"), _block[:400])
chk("main.py serve: warning fires before the resume (visibility precedes respawned drains)",
    "warn_dirty_tree" in _block and "resume_armed_drains" in _block
    and _block.index("warn_dirty_tree") < _block.index("resume_armed_drains"))

passed = [n for n, ok, _ in results if ok]
failed = [(n, d) for n, ok, d in results if not ok]
print(f"\neu386_dirty_tree_test: {len(passed)}/{len(results)} passed")
for n, d in failed:
    print(f"  FAIL: {n}" + (f" — {d}" if d else ""))
if failed:
    sys.exit(1)
