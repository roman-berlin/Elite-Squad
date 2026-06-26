"""EU-39 regression guards — the *behavioural* edges of the cockpit visual-refresh that
the skin tests don't exercise:

  1. ``_token_css`` FALLBACK path. The live path (regex out of ``warroom._PAGE``) is covered
     by cockpit_skin_test; here we force the failure branch (no ``:root{}`` to match) and
     prove the standalone pages still get a valid, self-contained token block instead of an
     empty/broken ``<style>`` — so offline / preview / SDK-less renders never lose their skin.

  2. ship-preview Jira deep-link HARDENING (EU-55/F12 dead-link fix). cockpit_skin_test proves
     a clean link renders; this pins the *fix itself*: the emitted anchor carries
     ``rel=noopener`` and the configured backlog ``base_url`` is HTML-escaped, so a base_url
     containing an HTML-special character can't break out of the href. Both would FAIL on the
     pre-fix markup (raw f-string, no rel, no escape).
"""
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

# Stub the Agent SDK so the orchestrator imports cleanly with no network / models.
_sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s


_sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = _sdk
sys.path.insert(0, ".")

from orchestrator import cockpit_views as V
from orchestrator import server, warroom
from orchestrator.config import AppConfig, Config

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ── 1) _token_css FALLBACK branch ────────────────────────────────────────────────
# Live path first (sanity): a real :root block is present, so we read it, not the fallback.
live = V._token_css()
chk("live path returns a :root token block", live.startswith("<style>:root{") and live.endswith("</style>"))

# Force the failure branch: blow away the :root{} the regex looks for.
_saved = warroom._PAGE
try:
    warroom._PAGE = "<!doctype html><style>body{color:#fff}</style>"  # no :root{} to match
    fb = V._token_css()
finally:
    warroom._PAGE = _saved

chk("fallback still yields a valid :root style block", fb == "<style>" + V._TOKENS_FALLBACK + "</style>")
chk("fallback is never an empty/broken <style>", fb.startswith("<style>:root{") and fb.endswith("</style>"))
chk("fallback carries the core palette + system tokens",
    all(t in fb for t in ("--bg", "--ink", "--accent", "--ring", "--r-md", "--okline")))
# and the live path is restored after the swap (no global leakage)
chk("warroom._PAGE restored after fallback test", V._token_css() == live)

# ── shared git fixture for the ship-preview page ─────────────────────────────────
tmp = Path(tempfile.mkdtemp())


def G(*a):
    subprocess.run(["git", *a], cwd=tmp, check=True, capture_output=True, text=True)


subprocess.run(["git", "init", str(tmp)], check=True, capture_output=True)
G("config", "user.email", "t@t"); G("config", "user.name", "t")
G("checkout", "-b", "DEV")
(tmp / "a.txt").write_text("x\n"); G("add", "-A"); G("commit", "-m", "base")
G("branch", "MAIN", "DEV")
(tmp / "b.txt").write_text("y\n"); G("add", "-A"); G("commit", "-m", "AUTO-9: a change")

os.environ["GENERAL_COCKPIT_PROMOTE"] = "1"

# ── 2a) deep-link carries rel=noopener (security hardening) ──────────────────────
app = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV", protected_branch="MAIN",
                backlog_backend="jira", backlog={"base_url": "https://acme.atlassian.net"})
cfg = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "test"
body = server.create_app(cfg).test_client().get("/ship-preview?app=automatixy").get_data(as_text=True)
chk("ship-preview link carries rel=noopener", 'href="https://acme.atlassian.net/browse/AUTO-9"' in body
    and "rel=noopener" in body)

# ── 2b) an HTML-special char in base_url is ESCAPED — can't break out of the href ─
appx = AppConfig(name="evil", repo_path=str(tmp), base_branch="DEV", protected_branch="MAIN",
                 backlog_backend="jira", backlog={"base_url": 'https://x.test/q?a=1&b="2"'})
cfgx = Config(apps=[appx], audit_path=str(tmp / "ax.jsonl"), use_worktree=False)
cfgx.detected_auth = lambda: "test"
bodyx = server.create_app(cfgx).test_client().get("/ship-preview?app=evil").get_data(as_text=True)
chk("base_url '&' is HTML-escaped in the href", "a=1&amp;b=" in bodyx)
chk("base_url '\"' is HTML-escaped (no raw quote injected)", 'b="2"/browse' not in bodyx and "&quot;2&quot;" in bodyx)
chk("escaped link still points at the ticket", "browse/AUTO-9" in bodyx)

print("\n=============== EU-39 COCKPIT REGRESSION QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
