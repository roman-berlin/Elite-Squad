"""EU-73 — Healthcheck tests for the Mac launchd keepalive daemon.

Verifies five properties of the daemon support added in this ticket:

  (a) The install script exists on disk and carries the executable bit.
  (b) The plist XML embedded in the script declares KeepAlive=<true/>.
  (c) ProgramArguments in that plist drives ``general --live autopilot <app>``.
  (d) daemon_running() correctly reflects process state via PID-file + os.kill,
      tested with fully-stubbed I/O so no real processes are touched.
  (e) The script's DOCUMENTED load-time start behaviour matches the plist's ACTUAL
      behaviour: KeepAlive=true is always-on (``launchctl load`` starts it immediately),
      RunAtLoad (if present) does not contradict that, and the comments/echoes say so —
      the exact comment/plist mismatch that bounced iteration 3.
"""
from __future__ import annotations

import os
import re
import stat
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, ".")

import orchestrator.autopilot as _ap_mod
from orchestrator.autopilot import daemon_running  # noqa: E402 — after sys.path tweak

# ── repo layout ───────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
INSTALL_SCRIPT = ROOT / "scripts" / "install-mac-autopilot-daemon.sh"

# ── result accumulator ────────────────────────────────────────────────────────
results: list[tuple[str, bool, str]] = []


def check(name: str, cond, detail: str = "") -> None:
    """Record a named assertion; does NOT raise — full results printed at the end."""
    results.append((name, bool(cond), str(detail)))


# ─────────────────────────────────────────────────────────────────────────────
# (a) Install script — existence and executable bit
# ─────────────────────────────────────────────────────────────────────────────
check(
    "install script exists at scripts/install-mac-autopilot-daemon.sh",
    INSTALL_SCRIPT.exists(),
    str(INSTALL_SCRIPT),
)

_script_mode = os.stat(INSTALL_SCRIPT).st_mode if INSTALL_SCRIPT.exists() else 0
_is_executable = bool(_script_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
check(
    "install script has at least one executable bit set",
    _is_executable,
    f"mode=0o{oct(_script_mode)[2:]}",
)


# ─────────────────────────────────────────────────────────────────────────────
# Shared helper: extract and parse the plist XML from the install-script heredoc
# ─────────────────────────────────────────────────────────────────────────────
def _extract_plist_root(script_path: Path) -> ET.Element:
    """Return the root Element of the plist XML embedded in *script_path*.

    The plist is a heredoc (``cat > "$PLIST" <<PLIST_EOF … PLIST_EOF``) inside the
    shell script.  Shell variable references (``${VAR}``) are replaced with the
    placeholder string ``"placeholder"`` before parsing so the XML is well-formed.
    The DOCTYPE declaration is stripped because xml.etree.ElementTree does not
    support external DTD references.
    """
    raw = script_path.read_text(encoding="utf-8")
    m = re.search(r"<<PLIST_EOF\n(.*?)\nPLIST_EOF", raw, re.DOTALL)
    if not m:
        raise ValueError("PLIST_EOF heredoc not found in install script")
    plist_xml = m.group(1)
    # Replace all ${VARNAME} shell substitutions with a stable placeholder.
    plist_xml = re.sub(r"\$\{[A-Z_]+\}", "placeholder", plist_xml)
    # Remove the DOCTYPE line — ET cannot resolve external DTDs.
    plist_xml = re.sub(r"<!DOCTYPE[^>]*>", "", plist_xml)
    return ET.fromstring(plist_xml)


def _plist_key_map(root: ET.Element) -> dict[str, ET.Element]:
    """Walk the top-level <dict> of a plist and return {key_text: value_element}."""
    # The root may be <plist> wrapping a <dict>, or <dict> directly.
    d = root.find("dict") if root.tag == "plist" else root
    if d is None:
        return {}
    mapping: dict[str, ET.Element] = {}
    children = list(d)
    it = iter(children)
    for child in it:
        if child.tag == "key":
            try:
                value_el = next(it)
                mapping[child.text or ""] = value_el
            except StopIteration:
                break
    return mapping


_plist_root: ET.Element | None = None
_parse_err: Exception | None = None
if INSTALL_SCRIPT.exists():
    try:
        _plist_root = _extract_plist_root(INSTALL_SCRIPT)
    except Exception as exc:  # noqa: BLE001
        _parse_err = exc

check("plist XML in install script parses without error", _plist_root is not None, str(_parse_err))

# ─────────────────────────────────────────────────────────────────────────────
# (b) KeepAlive key is present and set to <true/>
# ─────────────────────────────────────────────────────────────────────────────
if _plist_root is not None:
    _kv = _plist_key_map(_plist_root)
    _ka = _kv.get("KeepAlive")
    check(
        "plist has KeepAlive key",
        _ka is not None,
        f"keys present={list(_kv)}",
    )
    check(
        "plist KeepAlive value is <true/> (not <false/> or <string>)",
        _ka is not None and _ka.tag == "true",
        f"tag={_ka.tag if _ka is not None else 'missing'}",
    )
else:
    check("plist has KeepAlive key", False, "plist did not parse")
    check("plist KeepAlive value is <true/>", False, "plist did not parse")


# ─────────────────────────────────────────────────────────────────────────────
# (c) ProgramArguments drives the correct ``general --live autopilot`` invocation
# ─────────────────────────────────────────────────────────────────────────────
if _plist_root is not None:
    _kv = _plist_key_map(_plist_root)
    _pa = _kv.get("ProgramArguments")
    check(
        "plist has ProgramArguments key",
        _pa is not None,
        f"keys present={list(_kv)}",
    )
    if _pa is not None:
        _args = [el.text or "" for el in _pa if el.tag == "string"]
        check(
            "ProgramArguments[0] is the general binary (non-empty after var substitution)",
            len(_args) >= 1 and bool(_args[0]),
            f"args={_args}",
        )
        check(
            "ProgramArguments contains '--live' flag",
            "--live" in _args,
            f"args={_args}",
        )
        check(
            "ProgramArguments contains 'autopilot' sub-command",
            "autopilot" in _args,
            f"args={_args}",
        )
        # --live must come BEFORE 'autopilot' so the CLI parser sees it as a global flag.
        if "--live" in _args and "autopilot" in _args:
            check(
                "ProgramArguments: '--live' precedes 'autopilot' (global flag ordering)",
                _args.index("--live") < _args.index("autopilot"),
                f"args={_args}",
            )
        else:
            check("ProgramArguments: '--live' precedes 'autopilot'", False, f"args={_args}")
    else:
        check("ProgramArguments[0] is the general binary", False, "ProgramArguments key missing")
        check("ProgramArguments contains '--live'", False, "ProgramArguments key missing")
        check("ProgramArguments contains 'autopilot'", False, "ProgramArguments key missing")
        check("ProgramArguments '--live' precedes 'autopilot'", False, "ProgramArguments key missing")
else:
    check("plist has ProgramArguments key", False, "plist did not parse")
    check("ProgramArguments contains '--live'", False, "plist did not parse")
    check("ProgramArguments contains 'autopilot'", False, "plist did not parse")
    check("ProgramArguments '--live' precedes 'autopilot'", False, "plist did not parse")


# ─────────────────────────────────────────────────────────────────────────────
# (d) daemon_running() — unit tests with stubbed os.kill and PID-file I/O
#
# The helper lives in orchestrator.autopilot.daemon_running().  It reads
# _PID_FILE and calls os.kill(pid, 0) — we patch both at the module level so
# no real processes are probed and no real files are touched.
# ─────────────────────────────────────────────────────────────────────────────

# d-1: PID file absent → FileNotFoundError (an OSError) → returns False
_mock_no_file = MagicMock()
_mock_no_file.read_text.side_effect = FileNotFoundError("/tmp/general-autopilot.pid: No such file")
with patch.object(_ap_mod, "_PID_FILE", _mock_no_file):
    _result_missing = _ap_mod.daemon_running()
check("daemon_running() → False when PID file is absent", not _result_missing)

# d-2: PID file present, os.kill(pid, 0) succeeds → process is alive → True
_mock_alive = MagicMock()
_mock_alive.read_text.return_value = "12345\n"
with (
    patch.object(_ap_mod, "_PID_FILE", _mock_alive),
    patch("orchestrator.autopilot.os.kill", return_value=None) as _kill_alive,
):
    _result_alive = _ap_mod.daemon_running()
check("daemon_running() → True when process is alive (os.kill succeeds)", _result_alive)
# Verify it probed PID 12345 with signal 0.
_kill_alive.assert_called_once_with(12345, 0)
check(
    "daemon_running() probes the correct PID with signal 0",
    _kill_alive.call_args == ((12345, 0),),
    str(_kill_alive.call_args),
)

# d-3: PID file present, os.kill raises OSError(ESRCH) → process gone → False
_mock_dead = MagicMock()
_mock_dead.read_text.return_value = "99999"
with (
    patch.object(_ap_mod, "_PID_FILE", _mock_dead),
    patch("orchestrator.autopilot.os.kill", side_effect=OSError(3, "No such process")),
):
    _result_dead = _ap_mod.daemon_running()
check("daemon_running() → False when os.kill raises OSError (process dead)", not _result_dead)

# d-4: PID file contains non-numeric garbage → ValueError → returns False
_mock_bad = MagicMock()
_mock_bad.read_text.return_value = "not-a-number\n"
with patch.object(_ap_mod, "_PID_FILE", _mock_bad):
    _result_bad = _ap_mod.daemon_running()
check("daemon_running() → False when PID file has non-numeric content", not _result_bad)

# d-5: PID file present, os.kill raises PermissionError (EPERM: alive, not owned by us)
#      Implementation catches all OSError subclasses → safe default is False
_mock_eperm = MagicMock()
_mock_eperm.read_text.return_value = "1"
with (
    patch.object(_ap_mod, "_PID_FILE", _mock_eperm),
    patch("orchestrator.autopilot.os.kill", side_effect=PermissionError(1, "Not permitted")),
):
    _result_eperm = _ap_mod.daemon_running()
check(
    "daemon_running() → False on EPERM (safe default: cannot confirm ownership)",
    not _result_eperm,
)


# ─────────────────────────────────────────────────────────────────────────────
# (e) Load-time start behaviour ⇄ documentation consistency (EU-73 iter-4)
#
# launchd fact: an unconditional KeepAlive=true makes the job ALWAYS-ON — ``launchctl load``
# starts it immediately and launchd restarts it on exit; RunAtLoad is then redundant but must
# NOT contradict that (if present it has to be <true/>, never <false/>).  So the install script's
# comments + echoes must document "loading the agent starts LIVE autopilot immediately" and must
# NOT claim a manual first start is needed.  This asserts the DOCUMENTED behaviour matches the
# plist's ACTUAL load-time behaviour — the exact mismatch that bounced iteration 3.
# ─────────────────────────────────────────────────────────────────────────────
_SCRIPT_TEXT = INSTALL_SCRIPT.read_text(encoding="utf-8") if INSTALL_SCRIPT.exists() else ""

_keepalive_true = False
_runatload_tag: str | None = None   # None ⇒ key absent
if _plist_root is not None:
    _kv = _plist_key_map(_plist_root)
    _ka_el = _kv.get("KeepAlive")
    _keepalive_true = _ka_el is not None and _ka_el.tag == "true"
    _ral_el = _kv.get("RunAtLoad")
    _runatload_tag = _ral_el.tag if _ral_el is not None else None

# Actual load-time behaviour from the plist keys: KeepAlive=true OR RunAtLoad=true ⇒ starts at load.
_starts_at_load = _keepalive_true or (_runatload_tag == "true")
check(
    "plist semantics: the job STARTS at load (KeepAlive=true and/or RunAtLoad=true)",
    _starts_at_load,
    f"KeepAlive_true={_keepalive_true} RunAtLoad_tag={_runatload_tag}",
)

# RunAtLoad, if present, must not contradict KeepAlive=true's always-on start-at-load.
check(
    "plist: RunAtLoad (if present) is <true/>, never <false/> (no 'load but do not start' contradiction)",
    _runatload_tag in (None, "true"),
    f"RunAtLoad tag={_runatload_tag}",
)

# The prose MUST affirm start-at-load …
_affirms_start = bool(re.search(r"start\w*\s+LIVE\s+autopilot\s+immediately", _SCRIPT_TEXT, re.IGNORECASE))
check(
    "script documents that loading the agent starts LIVE autopilot immediately",
    _affirms_start,
    "expected the phrase 'starts LIVE autopilot immediately'",
)

# … and MUST NOT carry any stale claim that load does not start it / that a manual start is needed.
_contradictions: list[str] = []
if re.search(r"does\s+not\s+start", _SCRIPT_TEXT, re.IGNORECASE):
    _contradictions.append("'does NOT start' (claims load does not start it)")
if re.search(r"start\s+manually", _SCRIPT_TEXT, re.IGNORECASE):
    _contradictions.append("'Start manually' (claims a manual first start)")
if re.search(r"launchctl\s+start\b", _SCRIPT_TEXT, re.IGNORECASE):
    _contradictions.append("'launchctl start' (redundant/misleading for an always-on KeepAlive job)")
check(
    "script has NO comment/echo contradicting start-at-load "
    "(no 'does NOT start' / 'Start manually' / 'launchctl start')",
    not _contradictions,
    "; ".join(_contradictions),
)

# The crux: documented behaviour MATCHES the plist's actual load-time behaviour.
check(
    "documented load-time start behaviour MATCHES the plist (starts-at-load ⇄ prose, no contradiction)",
    _starts_at_load and _affirms_start and not _contradictions,
    f"starts_at_load={_starts_at_load} affirms={_affirms_start} contradictions={_contradictions}",
)


# ─────────────────────────────────────────────────────────────────────────────
# result summary
# ─────────────────────────────────────────────────────────────────────────────
passed_n = sum(1 for _, ok, _ in results if ok)
print(f"\n========= EU-73 autopilot daemon keepalive tests =========")
for name, ok, det in results:
    label = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{label}] {name}{extra}")
print("----------------------------------------------------------")
print(f"  {passed_n}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed_n == len(results) else f"{len(results) - passed_n} FAIL ❌")
sys.exit(0 if passed_n == len(results) else 1)
