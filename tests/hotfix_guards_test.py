"""Guards for three 2026-07-22 hotfixes that shipped WITHOUT tests (Commander order, same day).

Each was hand-verified when it landed — a real Telegram sink, a real reinstall, a real curl against
the live cockpit — but hand-verification does not survive into next month. Worse, all three are the
class of bug that only appears when the system is HEALTHY, so normal use never exercises them:

  · the watchdog only matters during an outage;
  · the installer only runs when someone installs;
  · the log panel only matters while a run is live.

That is the same "latent until it works" pattern as the audit self-merge double-count, which sat
invisible for 25 days because the sync it depended on was broken.

Pins:
  1. watchdog `_env_val` reads BOTH `export KEY=v` and bare `KEY=v` — the live .env uses the export
     form, and matching only the bare form installed the watchdog MUTE (it detected an outage,
     crossed its threshold, then logged "TELEGRAM_* unset — alert not sent");
  2. the publisher installer's plist heredoc contains NO backticks — it is unquoted (it must expand
     ${LABEL}/${GENERAL_BIN}), so a backtick there is COMMAND SUBSTITUTION: install literally
     executed the command names in its own comments and pasted a live sync line into the plist;
  3. `run_logger.drain_log_path()` really resolves a redirected stdout to its file — the cockpit's
     `data-log-path` fallback depends on it, and an empty attribute makes the panel's JS bail
     without ever opening the EventSource.
"""
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import types

_sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
_sdk.__getattr__ = lambda _n: _D
sys.modules["claude_agent_sdk"] = _sdk
_req = types.ModuleType("requests")
_req.Session = lambda *a, **k: None
_req.RequestException = Exception
sys.modules["requests"] = _req
sys.path.insert(0, ".")

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ── 1. the watchdog's _env_val, exercised as REAL bash ───────────────────────
wd = pathlib.Path("scripts/watchdog.sh")
if not wd.exists():
    chk("(1) scripts/watchdog.sh exists", False, "missing")
else:
    src = wd.read_text(encoding="utf-8")
    m = re.search(r"^_env_val\(\)\s*\{.*?^\}", src, re.S | re.M)
    chk("(1a) _env_val() is extractable from the script", bool(m))
    if m:
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="wdenv-"))
        # both declaration forms, plus a decoy that must NOT match
        (tmp / ".env").write_text(
            'export TELEGRAM_BOT_TOKEN="exported-value"\n'
            "TELEGRAM_CHAT_ID=bare-value\n"
            "NOT_TELEGRAM_CHAT_ID=decoy\n", encoding="utf-8")
        harness = f'GENERAL_DIR="{tmp}"\n{m.group(0)}\n'

        def _call(key):
            r = subprocess.run(["bash", "-c", harness + f'_env_val {key}'],
                               capture_output=True, text=True, timeout=20)
            return r.stdout.strip()

        chk("(1b) reads the `export KEY=\"v\"` form (the live .env's shape)",
            _call("TELEGRAM_BOT_TOKEN") == "exported-value", _call("TELEGRAM_BOT_TOKEN"))
        chk("(1c) still reads a bare `KEY=v`",
            _call("TELEGRAM_CHAT_ID") == "bare-value", _call("TELEGRAM_CHAT_ID"))
        chk("(1d) an absent key yields empty, not garbage", _call("NO_SUCH_KEY") == "",
            _call("NO_SUCH_KEY"))

# ── 2. the installer heredoc must not execute its own comments ───────────────
inst = pathlib.Path("scripts/install-mac-audit-publisher-daemon.sh")
if not inst.exists():
    chk("(2) the publisher installer exists", False, "missing")
else:
    isrc = inst.read_text(encoding="utf-8")
    here = re.search(r"<<PLIST_EOF\n(.*?)\nPLIST_EOF", isrc, re.S)
    chk("(2a) the plist heredoc is present", bool(here))
    if here:
        body = here.group(1)
        chk("(2b) ZERO backticks inside the unquoted heredoc (they would EXECUTE)",
            "`" not in body, body.count("`"))
        chk("(2c) the plist still carries the absolute binary path (not a bare command)",
            "${GENERAL_BIN}" in body or "/general" in body)
        chk("(2d) it is still an unquoted heredoc — the ${...} expansion is intentional",
            "<<PLIST_EOF" in isrc and "<<'PLIST_EOF'" not in isrc)

# ── 3. drain_log_path resolves a redirected stdout for real ──────────────────
from orchestrator import run_logger

tmp2 = pathlib.Path(tempfile.mkdtemp(prefix="drainlog-"))
target = tmp2 / "out.log"
probe = (
    "import sys; sys.path.insert(0, %r)\n"
    "from orchestrator import run_logger\n"
    "p = run_logger.drain_log_path()\n"
    "sys.stderr.write(str(p))\n" % os.getcwd()
)
with target.open("w") as fh:
    r = subprocess.run([sys.executable, "-c", probe], stdout=fh,
                       stderr=subprocess.PIPE, text=True, timeout=30)
resolved = (r.stderr or "").strip()
# Compare PHYSICAL paths: drain_log_path() reads the target out of lsof, which reports the resolved
# path, and on macOS /var is a symlink to /private/var. Not a defect — but worth pinning, because
# anything that compares this return value against a *configured* path (rather than tailing it)
# must resolve first or it will silently never match.
chk("(3a) a process whose stdout is a FILE resolves to that file",
    resolved and pathlib.Path(resolved).resolve() == target.resolve(),
    f"{resolved!r} != {str(target)!r}")

r2 = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=30)
chk("(3b) a piped (non-file) stdout resolves to None — never a bogus path",
    (r2.stderr or "").strip() in ("None", ""), (r2.stderr or "").strip()[:60])

wsrc = pathlib.Path("orchestrator/warroom.py").read_text(encoding="utf-8")
chk("(3c) the cockpit falls back to it when state has no log_path",
    "_rl.drain_log_path() or \"\"" in wsrc and "if not log_stream_path:" in wsrc)

print("\n========== HOTFIX GUARDS (2026-07-22) ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
