"""EU-335 — a stale unit checkout (cockpit running behind origin/dev) must surface a health warning.

2026-07-15: the morning daily brief reported "Shipped yesterday (0)" while 9 tickets had merged.
Root cause: the Technical Writer's changelog append + *.md.lock/.elite/ artifacts kept the primary
checkout dirty, so autopull's fast-forward was blocked and the resident cockpit ran ~40-commit-stale
code. Nothing surfaced the staleness — health said healthy.

Pins:
  (1) stale_checkout_check warns when local base is behind origin (N commits) — only for the unit's
      OWN repo (repo_path == the General root);
  (2) it returns None for a product app (a different repo_path), so product apps don't get a
      spurious freshness line;
  (3) it returns 'ok' when up to date;
  (4) gitignore hardening: *.md.lock and .elite/ are now in the committed .gitignore, so the
      land-time artifacts can't dirty a fresh checkout / the VPS tree.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, ".")

from orchestrator import health  # noqa: E402

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


ROOT = Path(__file__).resolve().parent.parent


class _Cfg:
    audit_path = str(ROOT / "state" / "audit.jsonl")  # so sync._repo_root(cfg) == the General root


def app(repo_path, base="dev"):
    return types.SimpleNamespace(repo_path=repo_path, base_branch=base, name="Elite-Unit")


def fake_git(behind: int, rc: int = 0):
    def _g(cwd, *args, **kw):
        return types.SimpleNamespace(returncode=rc, stdout=str(behind), stderr="")
    return _g


# (1) unit repo, N commits behind → warn
with patch("orchestrator.sync._git", fake_git(3)):
    r = health.stale_checkout_check(_Cfg(), app(str(ROOT)))
ok("(1) behind origin → warn naming the drift", r is not None and r[0] == "warn" and "behind" in r[1],
   f"got {r}")

# (2) a product app (different repo path) → None (no spurious freshness line)
with patch("orchestrator.sync._git", fake_git(3)):
    r = health.stale_checkout_check(_Cfg(), app("/some/other/product/repo"))
ok("(2) product app (different repo) → None", r is None, f"got {r}")

# (3) up to date → ok
with patch("orchestrator.sync._git", fake_git(0)):
    r = health.stale_checkout_check(_Cfg(), app(str(ROOT)))
ok("(3) up to date → ok", r is not None and r[0] == "ok", f"got {r}")

# (3b) git can't answer (non-zero rc) → None, never crashes health
with patch("orchestrator.sync._git", fake_git(0, rc=1)):
    r = health.stale_checkout_check(_Cfg(), app(str(ROOT)))
ok("(3b) git error → None (never takes health down)", r is None, f"got {r}")

# (4) gitignore hardening committed
gi = (ROOT / ".gitignore").read_text()
ok("(4) *.md.lock is gitignored", "*.md.lock" in gi)
ok("(4b) .elite/ is gitignored", ".elite/" in gi)

print(f"\n{checks}/{checks} passed")
