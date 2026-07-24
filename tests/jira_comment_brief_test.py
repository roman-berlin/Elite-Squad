"""Jira comments are brief bullets, never cut mid-sentence; manual-test lands go to QA (2026-07-23).

Commander, reading the EU-445 PM triage comment: "the comments in the Jira tickets are too long and
have too many programming elements … not a storytelling — and it even cut in the end. fix it
yourself. also if there are QA manual steps to do it's not 'blocked' status - it's QA status."

Three defects, three fixes:
  · the PM comment sites sliced with a bare ``[:1400]`` / ``[:1200]`` — a mid-sentence chop;
    they now go through ``notify.jira_brief`` (drops headings/tables/fences, folds prose to
    bullets, trims at a LINE boundary);
  · the PM triage prompt had a tight format for ESCALATE but none for RESOLVE — the EU-445
    storytelling came from that gap; PM_TRIAGE_SYSTEM now demands PROBLEM/ACTION/RECOMMENDATION
    bullets (~120 words, no tables/headings/code) for everything written to a ticket;
  · a merged land needing human verification went to Blocked (2026-07-20 order); superseded —
    "manual steps to run" IS QA work, Blocked stays reserved for genuine parks.

Pins:
  1. jira_brief on the REAL EU-445 comment: tables/headings gone, bounded, bullet-shaped;
  2. the structured hand-off lines (WHY/BLOCKER/DECISION/OPTIONS/1./2.) survive verbatim;
  3. the cap trims at a line boundary — output never ends mid-word relative to a line;
  4. hostile input never raises and never loses the comment entirely;
  5. every PM comment site calls jira_brief (no bare slice remains);
  6. the manual-test land sets status QA (not Needs Human), and every operator-facing string
     agrees ("moved to QA — manual test steps…");
  7. the PM prompt carries the comment-format contract; the Builder contract says QA column.
"""
import pathlib
import sys
import types

_req = types.ModuleType("requests")
_req.Session = lambda *a, **k: None
_req.RequestException = Exception
_req.post = lambda *a, **k: None
sys.modules["requests"] = _req
_sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
_sdk.__getattr__ = lambda _n: _D
sys.modules["claude_agent_sdk"] = _sdk
sys.path.insert(0, ".")

from orchestrator import notify

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# ── 1+2+3) the REAL EU-445 comment shape ─────────────────────────────────────
EU445 = """## Triage — EU-445

**Verdict: the Reviewer's typing rejection is a verified false positive. The core deliverable is complete and correct.**

I ran the authoritative check — the deterministic typing gate this very ticket hardens — against the Builder's actual working-tree diff:

| Scanner (the source of truth) | Result on this diff |
|---|---|
| `_untyped_public_defs(diff)` | `[]` — no untyped public def |
| explicit-Any scan (masked, `_mask_code_noise`) | `[]` — no real `Any` |

The Reviewer's required-change text is the **exact canned string** from `_enforce_missing_typing` (`reviewer.py:480-482`). But the function that *emits* that string returns **PASS** on this diff — so the Reviewer LLM officer is independently re-deriving the rule from the diff's prose and false-firing.

WHY PM CANNOT RESOLVE: only the Commander can waive the reviewer contract.
DECISION: accept the diff as-is?
OPTIONS:
1. Accept — the deterministic gate passes (RECOMMENDED)
2. Rebuild with annotations anyway
"""
out = notify.jira_brief(EU445, max_chars=1400)
chk("(1a) table rows are gone", "|" not in out, out[:200])
chk("(1b) markdown headings are gone", "## " not in out)
chk("(1c) bounded", len(out) <= 1400, len(out))
chk("(1d) prose folded to bullets", "• " in out or "- " in out, out[:200])
chk("(2a) WHY line survives verbatim",
    "WHY PM CANNOT RESOLVE: only the Commander can waive the reviewer contract." in out)
chk("(2b) DECISION + numbered options survive",
    "DECISION:" in out and "1." in out and "2." in out)

# 3) a long text trims at a LINE boundary
long_in = "\n".join(f"line {i}: " + "word " * 30 for i in range(40))
out2 = notify.jira_brief(long_in, max_chars=600)
chk("(3) the cap trims at a line boundary (every output line is complete)",
    all(ln in long_in or ln.startswith(("• ", "- ")) or ln == "" for ln in out2.splitlines())
    and len(out2) <= 600, len(out2))

# 4) hostile input
for bad in (None, "", "x" * 9000, "|||\n|-|-|\n```\ncode\n```"):
    try:
        r = notify.jira_brief(bad)
        ok = isinstance(r, str)
    except Exception:  # noqa: BLE001
        ok = False
    chk(f"(4) hostile input survives: {str(bad)[:14]!r}", ok)

# ── 5) no bare slice remains at the PM comment sites ─────────────────────────
lsrc = pathlib.Path("orchestrator/loop.py").read_text(encoding="utf-8")
chk("(5a) the escalate comment uses jira_brief", 'notify.jira_brief(esc)' in lsrc)
chk("(5b) no esc[:1400] slice remains", "esc[:1400]" not in lsrc)
chk("(5c) both automode-resolve comments use jira_brief",
    lsrc.count('notify.jira_brief(pm_outcome["body"]') == 2)
chk("(5d) no pm body [:1200] slice remains", 'pm_outcome["body"][:1200]' not in lsrc)

# ── 6) manual-test lands go to QA ────────────────────────────────────────────
# (6a) scoped to the MANUAL-TEST land only. The other four "Needs Human" sites are GENUINE parks
# (unready ticket, no-change verify, red post-merge dev-HEAD, SRE rollback) — those stay Blocked by
# design; the Commander's order was about manual-TEST hand-offs, which are QA work.
_mt = lsrc[lsrc.find("manual = None if cfg.mark_done_on_merge else _manual_test_block"):]
_mt = _mt[:_mt.find("except Exception")] if "except Exception" in _mt[:3000] else _mt[:3000]
chk("(6a) the manual-test land sets status QA (genuine parks stay Blocked)",
    'backlog.set_status(ticket, "QA")' in _mt
    and 'backlog.set_status(ticket, "Needs Human")' not in _mt)
chk("(6b) the head string says QA",
    '"moved to QA — manual test steps on the ticket"' in lsrc)
chk("(6c) the tail string says QA",
    'moved to QA — exact manual test steps are on the ticket' in lsrc)

# ── 7) the prompts carry the new contracts ───────────────────────────────────
psrc = pathlib.Path("orchestrator/pm.py").read_text(encoding="utf-8")
chk("(7a) PM prompt demands the bullet hand-off (PROBLEM/ACTION/RECOMMENDATION)",
    "COMMENT FORMAT" in psrc and "PROBLEM:" in psrc and "RECOMMENDATION:" in psrc
    and "Not a story" in psrc)
bsrc = pathlib.Path("orchestrator/builder.py").read_text(encoding="utf-8")
chk("(7b) Builder MANUAL TEST contract says QA column, not Blocked",
    "ticket into the QA column" in bsrc and "ticket into the Blocked column" not in bsrc)


# ── 8) the TRANSPORT choke point — ALL long comments fold, short pass byte-identical ──────────
jsrc = pathlib.Path("orchestrator/backlog/jira.py").read_text(encoding="utf-8")
chk("(8a) add_comment folds long bodies through jira_brief",
    "notify.jira_brief(body)" in jsrc and 'len(body or "") > 700' in jsrc)

from orchestrator.backlog import jira as _jira
class _Sess:
    def __init__(s): s.posts = []
    def post(s, url, json=None):
        s.posts.append(json)
        class _R:
            def raise_for_status(s2): return None
        return _R()
ad = types.SimpleNamespace(session=_Sess(), _url=lambda p_: f"https://x/{p_}")
tk = types.SimpleNamespace(key="EU-1", id="EU-1")

short = "✅ Merged to dev → moved to QA."
_jira.JiraAdapter.add_comment(ad, tk, short)
sent_short = str(ad.session.posts[-1])
chk("(8b) a short comment passes byte-identical", short in sent_short, sent_short[:120])

_jira.JiraAdapter.add_comment(ad, tk, EU445 + ("filler prose. " * 30))
sent_long = str(ad.session.posts[-1])
chk("(8c) a long comment arrives folded (no table rows survive)", "| Scanner" not in sent_long)
chk("(8d) …and its hand-off lines survive", "WHY PM CANNOT RESOLVE" in sent_long)

print("\n========== JIRA COMMENT BRIEF + MANUAL-TEST→QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
