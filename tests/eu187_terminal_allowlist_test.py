"""EU-187 — the cockpit /api/terminal endpoint must never run free-form shell (2026-07-06 audit N4).

POST /api/terminal used to execute `subprocess.Popen(cmd, shell=True)` on an arbitrary string —
unauthenticated command execution as the cockpit's user (local RCE; escalates the moment the
cockpit is bound off-loopback or proxied). This pins the no-shell allowlist contract on the pure
validator so it can't regress:

  · base command (argv[0]) must be in a fixed vetted allowlist — anything else is rejected;
  · interpreters (python/python3/bun/node/sh/bash/…) are NEVER allowed (== arbitrary code);
  · file-reading commands are confined to the working directory — an absolute/`..`-escaping
    operand is rejected, so /etc/passwd, ~/.ssh, out-of-tree secrets can't be exfiltrated;
  · shell metacharacters (chaining/substitution/redirection/globbing) are rejected outright.

Fail-first: `server._terminal_validate` does not exist on dev yet, so this harness errors at
import-resolution until the validator lands (the security behaviour is genuinely absent).
"""
from __future__ import annotations

import sys
import types
import tempfile
from pathlib import Path

# ── stubs so orchestrator.server imports without Flask/SDK/live deps ──
for name in ("claude_agent_sdk",):
    m = types.ModuleType(name)
    m.__getattr__ = lambda n: (lambda *a, **k: None)
    sys.modules.setdefault(name, m)

sys.path.insert(0, ".")

from orchestrator import server  # noqa: E402

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


validate = getattr(server, "_terminal_validate", None)
ok("_terminal_validate exists (the no-shell allowlist landed)", validate is not None,
   "server._terminal_validate is missing — /api/terminal still runs free-form shell")

CWD = Path(tempfile.mkdtemp())
(CWD / "inside.txt").write_text("hello")

# 1) a vetted read-only command passes and yields a parsed argv (no shell string)
argv, err = validate("git status", CWD)
ok("(1) allowlisted command passes", err is None and argv and argv[0] == "git", f"argv={argv} err={err}")

# 2) interpreters are rejected — they are arbitrary code execution
for interp in ("python3 -m http.server", "python -c 'import os'", "bash -c ls",
               "sh script.sh", "node app.js", "bun x pkg"):
    a, e = validate(interp, CWD)
    ok(f"(2) interpreter rejected: {interp.split()[0]}", a is None and e, f"got argv={a}")

# 3) an un-allowlisted binary is rejected
for prog in ("rm -rf /", "curl http://evil", "nc -e /bin/sh", "chmod 777 x"):
    a, e = validate(prog, CWD)
    ok(f"(3) non-allowlisted rejected: {prog.split()[0]}", a is None and e, f"got argv={a}")

# 4) shell metacharacters are rejected even for an allowed base command
for meta in ("ls; rm x", "cat a | sh", "echo $(whoami)", "git log && curl x",
             "cat a > /tmp/x", "echo `id`"):
    a, e = validate(meta, CWD)
    ok(f"(4) metachar rejected: {meta!r}", a is None and e, f"got argv={a}")

# 5) path-confinement: file-reading command may read inside cwd, not outside
a, e = validate("cat inside.txt", CWD)
ok("(5a) in-cwd read allowed", e is None and a, f"err={e}")
for esc in ("cat /etc/passwd", "cat ../../secret", "tail ~/.ssh/id_rsa", "head /etc/hosts"):
    a, e = validate(esc, CWD)
    ok(f"(5b) out-of-tree read rejected: {esc}", a is None and e, f"got argv={a}")

# 6) empty / unparseable input is rejected, never crashes
for bad in ("", "   ", "cat 'unterminated"):
    a, e = validate(bad, CWD)
    ok(f"(6) bad input rejected cleanly: {bad!r}", a is None and e is not None, f"got argv={a} err={e}")

print(f"\n{checks}/{checks} passed")
