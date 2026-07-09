"""EU-39 (slice 2) QA: the cockpit chrome consumes slice-1's design tokens, and the
ship-preview page emits a real, escaped Jira deep-link when the app's backlog base_url
is configured (EU-55/F12 dead ship-preview-links fix, end-to-end).

We assert the *skin contract* — that the standalone pages inject the single-source-of-truth
``:root{…}`` token block and reference it via ``var(--…)`` — not specific colour literals, so a
future re-skin in warroom flows through without breaking this test.
"""
import os
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

from orchestrator import cockpit_views as V
from orchestrator import server
from orchestrator.config import AppConfig, Config

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ── tokens are pulled LIVE from the War Room's :root block (single source of truth) ──
css = V._token_css()
chk("token css is a :root{} style block", css.startswith("<style>:root{") and css.endswith("</style>"))
chk("token css carries the palette", "--accent" in css and "--bg" in css and "--ink" in css)
chk("token css read live from warroom (not just fallback)", "--okline" in css)

# fallback is self-consistent for offline/preview renders
chk("fallback defines the same core tokens",
    all(t in V._TOKENS_FALLBACK for t in ("--bg", "--ink", "--accent", "--ring", "--r-md")))

# ── _wrap injects the tokens and skins via var() (so every standalone page inherits them) ──
w = V._wrap("Forensics", "<p>x</p>")
chk("_wrap injects the :root token block", ":root{" in w)
chk("_wrap skins body/chrome via var() tokens", "var(--bg)" in w and "var(--ink)" in w)
chk("_wrap keeps the back-to-cockpit link", "cockpit" in w)

# ── action buttons skinned via tokens ──
ab = V._actbar(V._actbtn("/api/drill", "Run drill"))
chk("_actbar uses var() tokens", "var(--panel2)" in ab or "var(--line2)" in ab)

# ── control bar skinned via tokens, distinct ship/deploy brand colours preserved ──
tmp = Path(tempfile.mkdtemp())

def G(*a):
    subprocess.run(["git", *a], cwd=tmp, check=True, capture_output=True, text=True)

subprocess.run(["git", "init", str(tmp)], check=True, capture_output=True)
G("config", "user.email", "t@t"); G("config", "user.name", "t")
G("checkout", "-b", "DEV")
(tmp / "a.txt").write_text("x\n"); G("add", "-A"); G("commit", "-m", "base")

app = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV", protected_branch="MAIN",
                backlog_backend="jira", backlog={"base_url": "https://acme.atlassian.net"})
cfg = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
bar = V._control_bar(cfg, "automatixy", healthy=True)
chk("control bar tokenised (no bare panel hex)", "var(--panel)" in bar and "#161b25" not in bar)
chk("control bar keeps a11y focus ring", "var(--ring)" in bar)

# ── ship-preview review page: skinned + a REAL Jira deep-link (EU-55/F12) ──
os.environ["GENERAL_COCKPIT_PROMOTE"] = "1"
# add a ticketed commit ahead of MAIN so the review page lists it
G("branch", "MAIN", "DEV")
(tmp / "b.txt").write_text("y\n"); G("add", "-A"); G("commit", "-m", "AUTO-9: a change")
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()
body = client.get("/ship-preview?app=automatixy").get_data(as_text=True)
chk("ship-preview injects tokens + uses var()", ":root{" in body and "var(--ink)" in body)
chk("ship-preview emits a real Jira deep-link when base_url is set",
    'href="https://acme.atlassian.net/browse/AUTO-9"' in body)
chk("ship-preview does NOT ship to /api/ship-main (removed in EU-204)", "/api/ship-main" not in body)

# ── no base_url → degrade to plain text, never a dead/empty href ──
app2 = AppConfig(name="plainapp", repo_path=str(tmp), base_branch="DEV", protected_branch="MAIN",
                 backlog_backend="none")
cfg2 = Config(apps=[app2], audit_path=str(tmp / "a2.jsonl"), use_worktree=False)
cfg2.detected_auth = lambda: "test"
body2 = server.create_app(cfg2).test_client().get("/ship-preview?app=plainapp").get_data(as_text=True)
chk("no base_url -> no broken /browse/ link emitted", "/browse/AUTO-9" not in body2)
chk("no base_url -> ticket still shown as text", "AUTO-9" in body2)

print("\n=============== COCKPIT SKIN QA (EU-39 slice 2) ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
