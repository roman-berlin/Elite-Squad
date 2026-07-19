"""EU-261: land commits must not embed the Builder's raw chat transcript as the body, nor a
185-char Jira finding-title as the subject.

Evidence (audit 2026-07-12, product-quality sample): every land wrote permanent history via the
inline f-string at loop.py `_land` — `{ticket.id}: {ticket.summary}\n\n{build.summary}\n\nReviewed-by: …`
— with no subject cap and no body cleanup, while builder.py sets `summary=run.final` (the RAW final
assistant message). 11 product DEV non-merge commits since 07-01 open their body with chat preamble:
af6df5a = "Perfect! All tests are passing. Let me now create a summary of the changes made:";
c835b90 = "Excellent! All fixes and tests are in place. Here's the summary:". Subjects inherited Jira
finding text verbatim: 88ec96a and dfa0cab are each exactly 185 chars.

This harness pins `loop._commit_message` — the pure helper the land path now formats through — so the
shape of permanent history is a tested contract, not an advisory line in the Builder's prompt.
"""
import sys, types
sys.path.insert(0, ".")

# Stub the Agent SDK so importing the orchestrator package never reaches the network.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

from orchestrator import loop

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

ns = types.SimpleNamespace

TRAILER = "Reviewed-by: autodev-reviewer"


def ticket(id="AUTO-130", summary="Fix leaky RLS check"):
    return ns(id=id, summary=summary)


def build(summary):
    return ns(summary=summary)


def msg(tkt, summary):
    return loop._commit_message(tkt, build(summary))


def subject_of(m):
    return m.splitlines()[0]


def body_of(m):
    # everything between the subject's blank-line separator and the trailer
    parts = m.split("\n\n")
    return "\n\n".join(parts[1:-1]).strip()


# --- 1) SUBJECT: the real 185-char Jira summary from 88ec96a must be capped ------------------- #
# Verbatim shape of the audit's evidence: a11y finding text used as a Jira title.
long_summary = ("[BLOCKER/a11y] AuthPage.tsx renders two h1 elements simultaneously (line 42 and "
                "line 118) which violates WCAG 2.2 SC 1.3.1 Info and Relationships and confuses "
                "screen-reader document outline")
chk("evidence fixture is the ~185-char class the audit found", len(long_summary) >= 180, len(long_summary))

m1 = msg(ticket(id="AUTO-130", summary=long_summary), "- did the thing")
s1 = subject_of(m1)
chk("long summary: subject capped at <=72 chars", len(s1) <= 72, f"{len(s1)}: {s1}")
chk("long summary: subject still opens with the ticket id", s1.startswith("AUTO-130: "), s1)
chk("long summary: subject is truncated with an ellipsis", s1.endswith("…"), s1)
chk("long summary: subject is a single line", "\n" not in s1)
chk("long summary: full summary is NOT in the subject", long_summary not in s1)

# --- 2) SUBJECT: a short summary must pass through unmangled ---------------------------------- #
m2 = msg(ticket(id="EU-261", summary="Sanitize land commit messages"), "- did the thing")
chk("short summary: subject is exact, no ellipsis",
    subject_of(m2) == "EU-261: Sanitize land commit messages", subject_of(m2))

# --- 3) SUBJECT: a multi-line summary must collapse (a commit subject is one line) ------------ #
m3 = msg(ticket(id="EU-9", summary="Fix the thing\nand also the other thing"), "- did it")
chk("multi-line summary: subject collapsed to one line",
    subject_of(m3) == "EU-9: Fix the thing and also the other thing", subject_of(m3))

# --- 4) BODY: the real af6df5a preamble is stripped down to the ## Summary heading ------------- #
preamble_heading = ("Perfect! All tests are passing. Let me now create a summary of the changes made:\n"
                    "\n"
                    "## Summary\n"
                    "- Tightened the tenant_id filter on the RLS policy\n"
                    "- Added a regression test for the cross-tenant read\n")
m4 = msg(ticket(), preamble_heading)
b4 = body_of(m4)
chk("preamble+heading: body starts at '## Summary'", b4.startswith("## Summary"), b4[:60])
chk("preamble+heading: 'Perfect!' chat preamble is gone", "Perfect!" not in m4)
chk("preamble+heading: the real content survives",
    "Tightened the tenant_id filter" in b4 and "regression test" in b4, b4)

# --- 5) BODY: c835b90's preamble with NO heading falls back to the first bullet ---------------- #
preamble_bullets = ("Excellent! All fixes and tests are in place. Here's the summary:\n"
                    "\n"
                    "- Replaced the two h1 elements with a single landmark heading\n"
                    "- Added an axe check to the a11y suite\n")
m5 = msg(ticket(), preamble_bullets)
b5 = body_of(m5)
chk("preamble+bullets: body starts at the first bullet",
    b5.startswith("- Replaced the two h1 elements"), b5[:60])
chk("preamble+bullets: 'Excellent!' chat preamble is gone", "Excellent!" not in m5)

# --- 6) BODY: a numbered-list summary anchors on the first numbered item ----------------------- #
preamble_numbered = ("Excellent! The lockfile has been successfully updated. Let me provide the "
                     "final summary:\n"
                     "\n"
                     "1. Regenerated bun.lock against the new registry\n"
                     "2. Pinned the transitive dep\n")
m6 = msg(ticket(), preamble_numbered)
chk("preamble+numbered: body starts at the first numbered item",
    body_of(m6).startswith("1. Regenerated bun.lock"), body_of(m6)[:60])

# --- 7) BODY: no anchor at all -> nothing is ever lost ---------------------------------------- #
no_anchor = "Rewrote the auth redirect so the session cookie is set before the 302."
m7 = msg(ticket(), no_anchor)
chk("no anchor: full summary passes through intact", no_anchor in m7, m7)

# --- 8) The Reviewed-by trailer survives every path ------------------------------------------- #
for label, m in (("long subject", m1), ("preamble+heading", m4), ("no anchor", m7)):
    chk(f"trailer survives ({label})", m.rstrip().endswith(TRAILER), m[-80:])
chk("trailer appears exactly once", m4.count(TRAILER) == 1)

# --- 9) An empty/None builder summary must not produce a malformed message -------------------- #
m9 = msg(ticket(id="EU-1", summary="Do a thing"), "")
chk("empty summary: subject still well-formed", subject_of(m9) == "EU-1: Do a thing", subject_of(m9))
chk("empty summary: trailer still present", m9.rstrip().endswith(TRAILER), m9)
chk("empty summary: no run of 3+ blank lines", "\n\n\n\n" not in m9, repr(m9))


passed = sum(1 for _, c, _ in results if c)
for n, c, d in results:
    print(f"  {'✓' if c else '✗'} {n}" + (f"  [{d}]" if (not c and d) else ""))
print(f"{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
