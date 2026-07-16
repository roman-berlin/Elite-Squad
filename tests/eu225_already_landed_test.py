"""EU-225 — don't rebuild already-landed tickets (pick-time already-landed check).

21 no_changes events parked to 'Needs Human' for a manual verify-and-close (the single highest-
frequency human touchpoint), and Planner CLOSE/ANSWER verdicts were advisory-only — so EU-191 was
fully rebuilt on 2026-07-10 even though its feature had landed 2 days earlier. Nothing checked the
changelog or git log for the ticket id before building.

_already_landed is the deterministic, DOUBLE-KEYED evidence gate: the ticket id must appear in the
Technical Writer changelog (written only on a successful live land) AND on a commit on origin/<base>.
Combined with a Planner non-BUILD verdict that makes it triple-keyed, and it routes to QA (reversible),
so the old senior_pm auto-close mistake can't recur.

Pins:
  (1) changelog hit + git-log hit → evidence string (already landed);
  (2) changelog hit but NO git-log commit → None (git-log corroboration required);
  (3) git-log hit but NO changelog entry → None (changelog required — the land signal);
  (4) field-precise changelog match: EU-19 does NOT match a `· EU-191 ·` entry;
  (5) empty / missing ticket id → None, never crashes;
  (6) source pins: the loop wires the double-keyed close to QA behind autoclose_already_landed.
"""
from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

sdk = types.ModuleType("claude_agent_sdk")
sdk.__getattr__ = lambda n: (lambda *a, **k: None)
sys.modules.setdefault("claude_agent_sdk", sdk)

sys.path.insert(0, ".")

from orchestrator import loop  # noqa: E402

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


APP = types.SimpleNamespace(repo_path=".", base_branch="dev", name="Elite-Unit")


def with_changelog(text):
    return patch.object(loop, "_changelog_path", lambda: _FakePath(text))


class _FakePath:
    def __init__(self, text):
        self._t = text

    def read_text(self):
        return self._t


def fake_gitlog(sha):
    def _run(cmd, *a, **k):
        return types.SimpleNamespace(returncode=0, stdout=(sha + "\n") if sha else "", stderr="")
    return _run


CHANGELOG = "# Development Status\n\n- 2026-07-08 · EU-191 · Elite-Unit · Phase 2 multi-model landed\n"

# (1) both signals present → evidence
with with_changelog(CHANGELOG), patch.object(subprocess, "run", fake_gitlog("abc1234")):
    ev = loop._already_landed(APP, "EU-191")
ok("(1) changelog + git-log hit → already-landed evidence", ev and "abc1234" in ev, f"got {ev!r}")

# (2) changelog hit, no git commit → None
with with_changelog(CHANGELOG), patch.object(subprocess, "run", fake_gitlog("")):
    ev = loop._already_landed(APP, "EU-191")
ok("(2) changelog hit but no origin commit → None (git corroboration required)", ev is None, f"got {ev!r}")

# (3) git commit hit but no changelog entry → None
with with_changelog("# Development Status\n\n- 2026-07-08 · EU-999 · x · y\n"), \
     patch.object(subprocess, "run", fake_gitlog("abc1234")):
    ev = loop._already_landed(APP, "EU-191")
ok("(3) no changelog entry → None (the land signal is required)", ev is None, f"got {ev!r}")

# (4) field-precise: EU-19 must NOT match a `· EU-191 ·` entry
with with_changelog(CHANGELOG), patch.object(subprocess, "run", fake_gitlog("abc1234")):
    ev = loop._already_landed(APP, "EU-19")
ok("(4) EU-19 does not match a `· EU-191 ·` changelog line", ev is None, f"got {ev!r}")

# (5) empty id → None
ok("(5) empty ticket id → None", loop._already_landed(APP, "") is None)
ok("(5b) whitespace id → None", loop._already_landed(APP, "   ") is None)

# (6) source pins for the loop wiring
src = Path("orchestrator/loop.py").read_text()
ok("(6) loop gates the close behind autoclose_already_landed", "autoclose_already_landed" in src)
ok("(6b) it only fires on CLOSE/ANSWER verdicts", '_pres.verdict in ("CLOSE", "ANSWER")' in src)
ok("(6c) it routes to QA (reversible), not Done", 'set_status(ticket, "QA")' in src
   and 'audit.record("already_landed_autoclose"' in src)

print(f"\n{checks}/{checks} passed")
