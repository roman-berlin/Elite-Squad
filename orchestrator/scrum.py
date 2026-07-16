"""Scrum Master (S-6) — splits a too-HEAVY ticket into small, independently-shippable sub-tickets.

When a ticket exhausts its build passes because it's genuinely oversized (a repo-wide rename, a
multi-screen feature, a cross-cutting refactor), the PM triage returns ``SPLIT`` and the loop calls
``scrum.split``. The Scrum Master breaks the parent into 2–5 fragments, FILES each in the backlog
(assigned to the Commander, label ``auto-split``), then closes the parent (comment + → Done). Each
fragment is small enough to land on its own, and the autopilot picks them up next cycle.
"""
from __future__ import annotations

import re

from .config import Config

SCRUM_SYSTEM = """You are the SCRUM MASTER of an autonomous software unit. A ticket has proven TOO HEAVY
to land in one build (the Builder + Reviewer burned all their passes on it). Your job: break it into
2–5 SMALL, INDEPENDENT sub-tickets that can each be built and reviewed on their own in a single pass.

Rules for the split:
- Each sub-ticket is ONE coherent, shippable slice — ideally one file/area/screen. A reviewer should be
  able to approve it without the others.
- Order them so earlier ones don't depend on later ones where possible.
- Carry over the parent's real intent + any concrete details (exact names, paths, mappings, env, AC).
  Don't lose specifics — restate them in the relevant sub-ticket.
- Be concrete: each sub-ticket has a short imperative TITLE and a body with **What**, **Where** (files),
  and **Acceptance criteria**. No vague "etc."
- 2–5 tickets. Fewer, larger-but-still-landable slices beat many tiny ones.

Output ONLY the sub-tickets, each in this exact block format, nothing else:

=== TICKET ===
TITLE: <short imperative title>
<body: What / Where / Acceptance criteria>

=== TICKET ===
TITLE: <next>
<body>
"""


def parse_subtickets(text: str | None) -> list[dict[str, str]]:
    """Parse the Scrum Master's reply into [{title, body}] from the '=== TICKET ===' blocks."""
    out: list[dict[str, str]] = []
    if not text:
        return out
    blocks = re.split(r"(?im)^\s*===\s*TICKET\s*===\s*$", text)
    for b in blocks:
        b = b.strip()
        if not b:
            continue
        m = re.search(r"(?im)^\s*TITLE:\s*(.+?)\s*$", b)
        if not m:
            continue
        title = m.group(1).strip()[:240]
        body = b[m.end():].strip()
        if title:
            out.append({"title": title, "body": body or title})
    return out


# Recursion bound: a fragment carries a machine-only HTML-comment marker "<!-- autosplit-depth: N -->" in
# its body; a re-split increments it. Past this ceiling we STOP splitting and escalate to the Commander —
# otherwise a ticket whose single irreducible action always blows the budget would fan out into ever-more
# sub-tickets forever (the adversarial fleet's finding on the budget-triggered auto-split, 2026-07-08).
# The marker is an HTML comment (invisible in rendered Jira, and a human ticket body is very unlikely to
# type it verbatim — unlike a plain "[split-depth: N]" that a ticket ABOUT this feature could spoof). 3
# levels is far more than any real ticket needs and still bounds the blast radius.
_MAX_SPLIT_DEPTH = 3
_DEPTH_RE = re.compile(r"<!--\s*autosplit-depth:\s*(\d+)\s*-->")


def _split_depth(parent) -> int:
    """How many times this ticket's lineage has already been auto-split (0 for an original ticket). Reads
    the MAX marker present, so even if a re-split's LLM echoed the parent's footer and a body ends up with
    more than one marker, the deepest still wins (a naive 'first match' could read a stale lower value and
    let the bound be defeated — the fleet's MAJOR finding)."""
    deps = _DEPTH_RE.findall(getattr(parent, "description", "") or "")
    return max((int(x) for x in deps), default=0)


async def split(cfg: Config, app_name: str, parent, recap: str = "", reason: str = "") -> dict:
    """Break ``parent`` into sub-tickets, FILE them, and close the parent. Returns
    {ok, keys, subs, error}. ``parent`` is a Ticket (needs .id/.summary/.description/.ephemeral)."""
    app = cfg.app(app_name)
    from . import recon, models
    from .backlog.base import make_backlog
    result: dict = {"ok": False, "keys": [], "subs": [], "error": None}

    # Recursion guard: refuse to split past the depth ceiling so an irreducible-but-too-big ticket parks
    # for a human instead of splitting without bound. The caller sees ok=False and escalates as normal.
    depth = _split_depth(parent)
    if depth >= _MAX_SPLIT_DEPTH:
        result["error"] = (f"max auto-split depth ({_MAX_SPLIT_DEPTH}) reached — {parent.id} is irreducibly "
                           "too big for one attempt; narrow it or handle it manually")
        return result

    task = "\n".join(filter(None, [
        f"Parent ticket {parent.id}: {getattr(parent, 'summary', '')}",
        f"\nWhy it's too heavy to land as one: {reason}" if reason else "",
        f"\nParent description:\n{(getattr(parent, 'description', '') or '')[:1800]}",
        f"\nWhat the unit already tried (so fragments don't repeat dead ends):\n{recap[:1500]}" if recap else "",
        "\nBreak it into 2–5 small, independently-shippable sub-tickets in the required block format.",
    ]))
    # EU-52: route through the ladder under the Scrum Master's own ceiling (discussion_model), so a
    # high-effort split conserves under a tight budget yet keeps the configured model when auto_model is off.
    model, mreason = models.for_officer(
        cfg, effort="high", ceiling_model=getattr(cfg, "discussion_model", cfg.reviewer_model))
    if getattr(cfg, "auto_model", False):
        print(f"  · scrum model: {mreason}", flush=True)
    report = await recon.run_officer(
        officer="scrum", label="Scrum Master", system=SCRUM_SYSTEM,
        task=task, cfg=cfg, cwd=app.repo_path, model=model,
        soldier_tools=["Read", "Grep", "Glob"], max_turns=16, effort="high", empty="")
    subs = parse_subtickets(report)
    result["subs"] = subs
    if not subs:
        result["error"] = "the Scrum Master proposed no sub-tickets"
        return result

    bl = make_backlog(app)

    # EU-301: decompose into an EPIC + Task children (grouped, board-readable, built one-at-a-time)
    # instead of flat siblings. The Epic groups the feature; each child is an independent, orderable
    # board card linked to it via `parent`. Graceful fallback: if the board has no "Epic" issue type
    # (or Epic creation fails for any reason), file flat children as before so a split is never lost.
    epic_key = None
    if not getattr(parent, "ephemeral", False):
        try:
            epic_body = ((getattr(parent, "description", "") or parent.summary or "")[:6000]
                         + f"\n\n— Epic decomposed from {parent.id} by the Scrum Master "
                           "(too heavy to land as one; children build one at a time, verify child last).")
            epic_key = bl.create_task(parent.summary or parent.id, epic_body,
                                      labels=["auto-split", "epic"], issue_type="Epic")
            if epic_key:
                result["epic"] = epic_key
                print(f"  🧩 created Epic {epic_key} for {parent.id}", flush=True)
        except Exception as exc:  # noqa: BLE001 — no Epic type / API error → flat siblings (below)
            epic_key = None
            print(f"  · Epic creation failed ({exc}); filing flat children instead.", flush=True)

    # EU-301: build the child list — up to 5 work pieces + a MANDATORY final verify-and-close child
    # carrying the PARENT's acceptance criteria (the end-to-end integration check the pieces don't
    # each cover). The verify child runs last and is what confirms the decomposed feature as a whole.
    children = list(subs[:5])
    _parent_ac = list(getattr(parent, "acceptance_criteria", None) or [])
    _verify_ac = ("\n".join(f"- {a}" for a in _parent_ac) if _parent_ac
                  else (getattr(parent, "description", "") or parent.summary or "")[:1500])
    children.append({
        "title": f"Verify & close: {parent.summary or parent.id}",
        "body": ("Verify the whole feature works end-to-end, then close the epic. This is the "
                 "integration check the individual pieces don't each cover — run it LAST, after the "
                 "sibling pieces land.\n\nAcceptance criteria (the ORIGINAL feature's):\n" + _verify_ac),
    })

    keys: list[str] = []
    for s in children:
        # Strip any depth marker the LLM may have echoed from the parent body, then stamp the ONE
        # authoritative marker — so a fragment carries exactly one, and the depth can never accrete.
        base_body = _DEPTH_RE.sub("", s["body"]).rstrip()
        body = (base_body + f"\n\n— Auto-split from {parent.id} by the Scrum Master (too heavy to land as one)."
                + f"\n<!-- autosplit-depth: {depth + 1} -->")
        try:
            k = bl.create_task(s["title"], body, labels=["auto-split"], parent=epic_key)
        except Exception as exc:  # noqa: BLE001 - one filing failure must not lose the rest
            result["error"] = f"filed {len(keys)}/{len(children)} before a Jira error: {exc}"
            break
        if k:
            keys.append(k)
            # EU-300/EU-301: children start in To Do (their created column) — NOT pre-marked In
            # Progress. A child becomes In Development only when the drain calls ticket_start on it
            # (loop.py), so at most one child per board is ever In Development, and an interrupted
            # run leaves the rest cleanly in the Epic's backlog for the next drain to resume in order.
    result["keys"] = keys
    # EU-358: ok means ALL children filed. A mid-batch Jira error used to still report ok=True and
    # close the parent below — the unfiled fragments existed only in the LLM report, so that slice of
    # the feature silently vanished (parent Done, nothing on the board to build it).
    filed_all = bool(keys) and len(keys) == len(children) and not result.get("error")
    result["ok"] = filed_all

    if keys and not filed_all and not getattr(parent, "ephemeral", False):
        # Partial filing: keep the parent OPEN (the caller escalates on ok=False) and record exactly
        # what landed, so the Commander — or a retry — can file the remainder instead of losing it.
        try:
            bl.add_comment(parent, "⚠️ Split INCOMPLETE — filed only " + ", ".join(keys)
                           + f" of {len(children)} planned children ({result.get('error', 'Jira error')}). "
                           "Parent left open; the remaining children still need filing.")
        except Exception:  # noqa: BLE001
            pass

    # Close the parent: a comment naming the epic + children, then move it out of the queue (→ Done).
    if filed_all and not getattr(parent, "ephemeral", False):
        _into = (f"Epic {epic_key} with children " if epic_key else "children ") + ", ".join(keys)
        try:
            bl.add_comment(parent, "🧩 Too heavy to land as one ticket — the Scrum Master decomposed it "
                           f"into {_into}. The children build one at a time (verify child last); closing "
                           "this parent.")
        except Exception:  # noqa: BLE001
            pass
        try:
            bl.set_status(parent, "Done")
        except Exception:  # noqa: BLE001 - if "Done" isn't a valid transition, the comment still records it
            pass
    return result
