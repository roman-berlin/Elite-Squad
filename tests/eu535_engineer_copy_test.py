"""EU-535 guard test: every user-facing display string says 'engineer', never 'officer'.

Acceptance criteria covered here:
  1. GET /standup returns HTML with the plural "Engineers' stand-up" heading (the guard's
     \bEngineer\b bare-persona pattern can't match the plural) and no 'Officer stand-up',
     'The officers are reporting', or 'No officer stand-up'.
  2. GET /council and GET /group return HTML whose visible copy has no standalone
     'officer'/'officers' (name=officer wire keys exempt — asserted via diff/grep below).
  3. council.py brief literals read 'engineers' not 'officers'.
  4. server.py display strings have no 'officer'.
"""
from __future__ import annotations

import re
import sys
import tempfile
import types
from pathlib import Path

# --- stub the Agent SDK so orchestrator modules import cleanly (no network) ---
_sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k):
        pass

    def __call__(s, *a, **k):
        return s


_sdk.__getattr__ = lambda _n: _D
sys.modules["claude_agent_sdk"] = _sdk
sys.path.insert(0, ".")

from orchestrator import server
from orchestrator.config import AppConfig, Config

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail) if not cond else ""))


# ── Shared fixtures ────────────────────────────────────────────────────────
_repo = Path(tempfile.mkdtemp()) / "app"
_repo.mkdir()
cfg = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(_repo),
                    base_branch="DEV", protected_branch="main", backlog_backend="none")],
    audit_path=str(Path(tempfile.mkdtemp())), use_worktree=False,
    discussion_model="test",
)
cfg.detected_auth = lambda: "test"
client = server.create_app(cfg).test_client()

# ---- AC1: /standup renders the plural "Engineers' stand-up", zero legacy phrases ----
r = client.get("/standup")
body = r.get_data(as_text=True)
chk("/standup status 200", r.status_code == 200, str(r.status_code))
chk("/standup contains \"Engineers' stand-up\"", "Engineers' stand-up" in body,
    "\"Engineers' stand-up\" missing")
chk("/standup lacks 'Officer stand-up'", "Officer stand-up" not in body, f"FOUND: Officer stand-up")
chk("/standup lacks 'The officers are reporting'", "The officers are reporting" not in body,
    f"FOUND: The officers are reporting")
chk("/standup lacks 'No officer stand-up'", "No officer stand-up" not in body,
    f"FOUND: No officer stand-up")

# ---- AC2: /council has no display 'officer' copy (wire keys OK) ----
r = client.get("/council")
body = r.get_data(as_text=True)
chk("/council status 200", r.status_code == 200, str(r.status_code))

# Strip name=officer input markup before scanning for display copy
cleaned = re.sub(r'name\s*=\s*officer[^>]*>', '', body)
# Also strip value="<officer-name>" hidden inputs
cleaned = re.sub(r'value\s*=\s*"([^"]*)"', lambda m: '"REDACTED"' if m.group(1) in ('general','adjutant','pm','scrum','field_engineer','inspector','sentinel','quartermaster','provost') else m.group(0), cleaned)

officer_words = re.findall(r'\boffic[eé]rs?\b', cleaned, re.IGNORECASE)
chk("/council has no display 'officer'/'officers'", len(officer_words) == 0,
    f"LEFTOVER DISPLAY: {officer_words}")

# Also check /group
r = client.get("/group")
body = r.get_data(as_text=True)
chk("/group status 200", r.status_code == 200, str(r.status_code))
cleaned = re.sub(r'name\s*=\s*officer[^>]*>', '', body)
officer_words = re.findall(r'\boffic[eé]rs?\b', cleaned, re.IGNORECASE)
chk("/group has no display 'officer'/'officers'", len(officer_words) == 0,
    f"LEFTOVER DISPLAY: {officer_words}")

# ---- AC3: council.py brief literals say 'engineers' ----
source_path = Path("orchestrator/council.py")
src = source_path.read_text()

# Stand-up header in chair prompt
chk("council stand-up header says 'engineers'",
    "The engineers' stand-up" in src,
    "Still has \"The officers' stand-up\"")

# Meeting debate lines
meeting_debate_count = src.count("The engineers debated:")
old_debate = src.count("The officers debated:")
chk(f"council meeting debate says 'engineers' ({meeting_debate_count} found)", meeting_debate_count >= 1,
    f"Still has {old_debate} x \"The officers debated:\"")
chk("council has no old 'The officers debated:' display line", old_debate == 0,
    f"Found {old_debate} remaining")

# ---- AC4: server.py display-string grep (non-comment non-plumbing) ----
srv_src = Path("orchestrator/server.py").read_text()

# Find render-string lines (HTML/string literals containing 'officer') that are NOT:
#   - comments (# …)
#   - docstrings
#   - request.form/args plumbing
#   - /group?officer= URLs
#   - identifiers (_officer_, officer=, officers=)
bad_display = []
for i, ln in enumerate(srv_src.splitlines(), 1):
    stripped = ln.strip()
    if not re.search(r'\boffic[eé]rs?\b', ln, re.IGNORECASE):
        continue
    # Skip pure code identifiers / plumbing
    if any(skip in ln for skip in [
            'request.form.getlist("officer")', 'request.args.get("officer")',
            'request.form.get("officer")', 'name=officer', '/group?officer=',
            '_officer_', 'officer = ', 'officers = ', '# ',
            'html.escape(officer)', 'officer if', 'officer ', 'officer}',
            'officer_label', 'officer_key', '_select_officer', '_match_officer',
            'officers=',  # function param
            'tag="group-"',  # tag construction
            '"you", text',  # echo line
        ]):
            continue
    # Check it looks like a display string (contains quotes/f-string with content)
    if "\"" in ln or "'" in ln or "f'" in ln or 'f"' in ln:
        bad_display.append((i, stripped[:100]))

chk("server.py display strings have no 'officer'", not bad_display,
    f"Issue lines: {[(n, l[:70]) for n, l in bad_display]}")

# Print summary
print("\n============= EU-535 ENGINEER COPY GUARD =============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
