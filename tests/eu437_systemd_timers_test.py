"""EU-437 — VPS cron schedules moved to systemd timers (DST-proof).

Offline harness that exercises `scripts/install-server-timers.sh` with DRY_RUN=1 and pins every
acceptance criterion:

  AC1 — the dry-run writes exactly 4 unit files into OUTPUT_DIR only; no /etc/systemd/system or
        systemctl calls are made in dry-run mode. Timers carry tz-suffixed OnCalendar + Persistent.
  AC2 — services route through ./general cron-guard --job <daily|council> with output appended
        to council/cron.log (EU-432 guard wrapping preserved verbatim).
  AC5 — non-dry-run path contains systemctl daemon-reload, enable --now for both timers, and
        prints timedatectl + systemctl list-timers output.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # the General repo root
SCRIPT = ROOT / "scripts" / "install-server-timers.sh"

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# ══════════════════════════════════════════════════════════════════════════════
# Mock systemd --version (the script checks >= 240; Ubuntu VPS always qualifies).
# We place a tiny stub ahead of PATH so the version gate passes on dev boxes
# that lack a real systemd binary (macOS, CI containers).
# ══════════════════════════════════════════════════════════════════════════════
_mock_dir = Path(tempfile.mkdtemp(prefix="eu437-mock-"))
_mock_bin = _mock_dir / "systemd"
_mock_bin.write_text(
    "#!/bin/sh\necho 'systemd 249'\n",
    encoding="utf-8",
)
os.chmod(str(_mock_bin), 0o755)

# ══════════════════════════════════════════════════════════════════════════════
# Hermetic HOME: the script does `cd "$HOME/General"` — we create a throwaway
# /tmp/fakehome with a General -> repo-root symlink so the cd succeeds on a
# clean box without depending on any developer's real $HOME layout.
# ══════════════════════════════════════════════════════════════════════════════
_fake_home = Path("/tmp/fakehome")
_fake_home.mkdir(parents=True, exist_ok=True)
_fake_general = _fake_home / "General"
if _fake_general.is_symlink() or _fake_general.exists():
    if _fake_general.is_symlink():
        _fake_general.unlink()
    else:
        import shutil
        shutil.rmtree(_fake_general)
os.symlink(str(ROOT), str(_fake_general))

_run_env = {
    **os.environ,
    "PATH": str(_mock_dir) + ":" + os.environ["PATH"],
    "HOME": "/tmp/fakehome",
    "USER": "root",
}


# ══════════════════════════════════════════════════════════════════════════════
# Run the installer in dry-run mode
# ══════════════════════════════════════════════════════════════════════════════
tmpdir = Path(tempfile.mkdtemp(prefix="eu437-timers-"))

proc = subprocess.run(
    ["bash", str(SCRIPT)],
    capture_output=True, text=True,
    env={**_run_env, "DRY_RUN": "1", "OUTPUT_DIR": str(tmpdir)},
)

chk("AC1dry: script exits 0 in dry-run", proc.returncode == 0, f"rc={proc.returncode}")
chk("AC1dry: stderr is clean", proc.stderr == "", proc.stderr[:200] if proc.stderr else "")

# ── AC1: exactly 4 files in OUTPUT_DIR, nothing in /etc/systemd/system ──────────
files = sorted(p.name for p in tmpdir.iterdir())
expected = sorted(["general-daily.service", "general-daily.timer",
                    "general-council.service", "general-council.timer"])
chk("AC1: exactly 4 unit files written (general-daily.* + general-council.*)",
    files == expected, f"got {files}, wanted {expected}")

# Verify nothing leaked to /etc/systemd/system
for name in expected:
    real_path = Path("/etc/systemd/system") / name
    chk(f"AC1: {name} NOT installed under /etc/systemd/system in dry-run",
        not real_path.exists(), "")

# ── AC1 continued: timer contents correct ─────────────────────────────────────
daily_timer = (tmpdir / "general-daily.timer").read_text(encoding="utf-8")
council_timer = (tmpdir / "general-council.timer").read_text(encoding="utf-8")

chk("AC1: daily timer OnCalendar has timezone suffix Asia/Jerusalem",
    "*-*-* 08:30:00 Asia/Jerusalem" in daily_timer, "")
chk("AC1: daily timer Persistent=true", "Persistent=true" in daily_timer, "")
chk("AC1: council timer OnCalendar = Mon *-*-* 09:30:00 Asia/Jerusalem",
    "Mon *-*-* 09:30:00 Asia/Jerusalem" in council_timer, "")
chk("AC1: council timer Persistent=true", "Persistent=true" in council_timer, "")

# ── AC2: service ExecStart preserves cron-guard wrapper ────────────────────────
daily_svc = (tmpdir / "general-daily.service").read_text(encoding="utf-8")
council_svc = (tmpdir / "general-council.service").read_text(encoding="utf-8")

for tag, svc_text in [("daily", daily_svc), ("council", council_svc)]:
    chk(f"AC2:{tag} service Type=oneshot",
        "Type=oneshot" in svc_text, "")
    chk(f"AC2:{tag} service has User= set (expanded from $USER)",
        "User=" in svc_text and len(svc_text.split("User=")[1].split("\n")[0]) > 0,
        "missing User= in:\n" + svc_text)
    chk(f"AC2:{tag} service WorkingDirectory=/.../General (expanded from $HOME_DIR)",
        "/General" in svc_text and "WorkingDirectory=" in svc_text,
        "missing WorkingDirectory with /General in:\n" + svc_text)
    chk(f"AC2:{tag} service ExecStart routes through cron-guard --job {tag}",
        f"./general cron-guard --job {tag}" in svc_text,
        "missing cron-guard in:\n" + svc_text)
    chk(f"AC2:{tag} service ExecStart appends to council/cron.log",
        ">> council/cron.log 2>&1" in svc_text,
        "missing redirect in:\n" + svc_text)

# ── AC5: non-dry-run path contains required commands ───────────────────────────
script_text = SCRIPT.read_text(encoding="utf-8")
chk("AC5: non-dry-run path calls systemctl daemon-reload",
    "systemctl daemon-reload" in script_text, "")
chk("AC5: non-dry-run path enables general-daily.timer",
    "systemctl enable --now general-daily.timer" in script_text, "")
chk("AC5: non-dry-run path enables general-council.timer",
    "systemctl enable --now general-council.timer" in script_text, "")
chk("AC5: non-dry-run path prints timedatectl output",
    "timedatectl" in script_text, "")
chk("AC5: non-dry-run path prints systemctl list-timers for general-*",
    "systemctl list-timers 'general-*'" in script_text, "")

# ══════════════════════════════════════════════════════════════════════════════
# Report
# ══════════════════════════════════════════════════════════════════════════════
print("\n===================== EU-437 SYSTEMD TIMERS QA =====================")
passed = sum(1 for _, ok_, _ in results if ok_)
for name, ok_, det in results:
    print(f"  [{'PASS' if ok_ else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok_ else ""))
print("-------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
