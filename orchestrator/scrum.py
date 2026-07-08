"""Scrum Master (S-6) — splits a too-HEAVY ticket into small, independently-shippable sub-tickets.

When a ticket exhausts its build passes because it's genuinely oversized (a repo-wide rename, a
multi-screen feature, a cross-cutting refactor), the PM triage returns ``SPLIT`` and the loop calls
``scrum.split``. The Scrum Master breaks the parent into 2–5 fragments, FILES each in the backlog
(assigned to the Commander, label ``auto-split``), then closes the parent (comment + → Done). Each
fragment is small enough to land on its own, and the autopilot picks them up next cycle.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

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
    keys: list[str] = []
    for s in subs[:6]:
        # Strip any depth marker the LLM may have echoed from the parent body, then stamp the ONE
        # authoritative marker — so a fragment carries exactly one, and the depth can never accrete.
        base_body = _DEPTH_RE.sub("", s["body"]).rstrip()
        body = (base_body + f"\n\n— Auto-split from {parent.id} by the Scrum Master (too heavy to land as one)."
                + f"\n<!-- autosplit-depth: {depth + 1} -->")
        try:
            k = bl.create_task(s["title"], body, labels=["auto-split"])
        except Exception as exc:  # noqa: BLE001 - one filing failure must not lose the rest
            result["error"] = f"filed {len(keys)}/{len(subs)} before a Jira error: {exc}"
            break
        if k:
            keys.append(k)
            # The fragments ARE the active work now (they're implemented together to finish the parent),
            # so move each straight to In Progress ("in development") rather than leaving it in To Do —
            # the board shows the real state, and the autopilot resumes In Progress first. Best-effort:
            # a missing transition just leaves it in its created column (set_status handles that), and a
            # status nudge must never lose a filed fragment.
            try:
                bl.set_status(SimpleNamespace(key=k), "In Progress")
            except Exception:  # noqa: BLE001
                pass
    result["keys"] = keys
    result["ok"] = bool(keys)

    # Close the parent: a comment naming the fragments, then move it out of the queue (→ Done).
    if keys and not getattr(parent, "ephemeral", False):
        try:
            bl.add_comment(parent, "🧩 Too heavy to land as one ticket — the Scrum Master split it into: "
                           + ", ".join(keys) + ". Closing this parent; the fragments land on their own.")
        except Exception:  # noqa: BLE001
            pass
        try:
            bl.set_status(parent, "Done")
        except Exception:  # noqa: BLE001 - if "Done" isn't a valid transition, the comment still records it
            pass
    return result
