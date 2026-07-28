"""EU-39 regression guards — the *behavioural* edges of the cockpit visual-refresh that
the skin tests don't exercise:

  1. ``_token_css`` emits ``THEME_TOKENS_CSS`` verbatim regardless of what's in
     ``warroom._PAGE`` — EU-791 deleted the regex + fallback path. We prove that even if
     ``_PAGE`` contains no ``:root{}``, ``_token_css()`` still returns a complete token block.

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


# ── 1) _token_css ALWAYS emits themes (EU-791: no fallback, no regex) ────────────
live = V._token_css()
chk("live path returns a :root token block (+ theme boot, 2026-07-19)",
    "<style>" in live and ":root{" in live and "</style>" in live
    and "data-theme=light" in live)
chk("_token_css() wraps THEEME_TOKENS_CSS byte-identically",
    live == "<style>" + warroom.THEME_TOKENS_CSS + "</style>" + V._THEME_BOOT)

# Prove independence from _PAGE content: change _PAGE, confirm _token_css()
# stays the same (EU-791: it reads THEME_TOKENS_CSS, never scrapes _PAGE).
_saved_page = warroom._PAGE
warroom._PAGE = "<!doctype html><style>body{color:#fff}</style>"  # no :root{} at all
fb = V._token_css()
warroom._PAGE = _saved_page

chk("_token_css() ignores _PAGE content (reads THEME_TOKENS_CSS instead)",
    fb == live)
chk("even with broken _PAGE, token block is still complete (:root{} present)",
    ":root{" in fb and "</style>" in fb and "data-theme=light" in fb)
# and the live path is unaffected by the swap (no global leakage)
chk("live path restored after _PAGE modification", V._token_css() == live)

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
