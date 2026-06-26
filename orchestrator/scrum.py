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


async def split(cfg: Config, app_name: str, parent, recap: str = "", reason: str = "") -> dict:
    """Break ``parent`` into sub-tickets, FILE them, and close the parent. Returns
    {ok, keys, subs, error}. ``parent`` is a Ticket (needs .id/.summary/.description/.ephemeral)."""
    app = cfg.app(app_name)
    from . import recon, models
    from .backlog.base import make_backlog
    result: dict = {"ok": False, "keys": [], "subs": [], "error": None}

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
        body = s["body"] + f"\n\n— Auto-split from {parent.id} by the Scrum Master (too heavy to land as one)."
        try:
            k = bl.create_task(s["title"], body, labels=["auto-split"])
        except Exception as exc:  # noqa: BLE001 - one filing failure must not lose the rest
            result["error"] = f"filed {len(keys)}/{len(subs)} before a Jira error: {exc}"
            break
        if k:
            keys.append(k)
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
