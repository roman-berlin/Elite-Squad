"""Failure forensics + auto-post-mortem.

Turns the "errored / escalated" pile into something actionable. Two deterministic, zero-cost pieces:

1. **Taxonomy** — classify every failed run into a small set of causes (under-specified ticket, too
   big / hit the cap, merge conflict, gate/review failure, security block, product decision needed,
   infra/process error, transient) with a recommended fix for each. Drives the cockpit `/forensics` page
   and `general forensics`.
2. **Auto-post-mortem** — when the SAME ticket has failed N times (``postmortem_after``, default 3), the
   unit writes a short post-mortem to ``postmortems/<TICKET>.md``: the pattern, a timeline of attempts,
   the dominant cause, and what would change the outcome. No model call — it reads the audit log it
   already keeps, so it's free and runs unattended.
"""
from __future__ import annotations

import time
from collections import Counter
from pathlib import Path

from . import dashboard as D

# A run counts as a failure worth diagnosing when it escalated, errored, landed only as a PR, or parked
# awaiting a decision. NOTE two vocabularies: dashboard.load_tasks emits these display strings, while a
# live TicketReport.outcome carries Outcome enum *values* — _REPORT_FAIL_VALUES below bridges them.
FAILED_OUTCOMES = {"escalated", "errored", "awaiting decision", "PR / needs you"}
_REPORT_FAIL_VALUES = {"escalated", "errored", "pr_into_dev"}   # Outcome.ESCALATED/ERRORED/PR_OPENED

# Ordered, most-specific-first. Each: (category, label, keywords, recommended action).
_RULES: list[tuple[str, str, tuple[str, ...], str]] = [
    ("not_ready", "Under-specified ticket",
     ("not ready", "handed back", "readiness"),
     "Add acceptance criteria or a fuller description, then /unblock — the readiness gate handed it back before building."),
    ("product_blocker", "Product / IA decision needed",
     ("product blocker", "escalated to commander", "product/ia", "needs a decision", "ia blocker"),
     "Answer the product/IA question (or enable PM auto-decide / automode) so the build can resume."),
    ("security_block", "Security finding",
     ("security", "provost", "critical", "vuln", "secret"),
     "Address the security finding the Provost flagged — it opened a PR instead of landing."),
    ("merge_conflict", "Merge conflict",
     ("conflict", "non-fast-forward", "couldn't merge", "could not merge", "cannot merge", "rebase"),
     "Bring DEV up to date and resolve the conflict; the unit couldn't land the branch cleanly."),
    ("gate_fail", "Failed the gate / review",
     ("rejected", "gate", "typecheck", "tsc", "lint", "test failed", "tests failed", "build failed", "review"),
     "The pre-merge gate (tests/typecheck/lint) or the Reviewer is red — fix it, or correct the gate command in config."),
    ("too_big", "Too big / hit the cap",
     ("ran out of turns", "too big", "max_iterations", "max iterations", "budget exceeded", "cost budget",
      "turn-limit", "turn limit", "one pass"),
     "Split into smaller tickets, or raise builder effort / max_iterations — the build hit its turn/cost cap."),
    ("worktree_busy", "Deferred (worktree busy)",
     ("worktree busy", "deferred"),
     "Transient — another run held the worktree; the unit retries it automatically."),
    ("infra", "Infra / process error",
     ("exception", "process errored", "traceback", "sdk", "timeout", "network", "connection", "errored"),
     "A tooling / SDK / process error — check connectivity and the Builder logs, then /unblock to retry."),
]

_LABELS = {c: lbl for c, lbl, _, _ in _RULES}
_ACTIONS = {c: act for c, _, _, act in _RULES}
_LABELS["unknown"] = "Uncategorized"
_ACTIONS["unknown"] = "Cause not auto-classified — read the attempt notes below."


def classify(outcome: str, text: str = "") -> dict:
    """Map a failure to (category, label, action) from its outcome + reason text. Deterministic."""
    blob = f"{outcome or ''} {text or ''}".lower()
    for cat, label, kws, action in _RULES:
        if any(k in blob for k in kws):
            return {"category": cat, "label": label, "action": action}
    if (outcome or "").lower() == "errored":
        return {"category": "infra", "label": _LABELS["infra"], "action": _ACTIONS["infra"]}
    return {"category": "unknown", "label": _LABELS["unknown"], "action": _ACTIONS["unknown"]}


def _reason(run: dict) -> str:
    return " ".join(x for x in (run.get("note"), run.get("verdict")) if x)


def scan(cfg) -> list[dict]:
    """Every failed run, newest first, each tagged with its category/label/action."""
    out = []
    for r in D.load_tasks(cfg.audit_path):
        if r.get("outcome") in FAILED_OUTCOMES:
            out.append({**r, **classify(r.get("outcome", ""), _reason(r))})
    return out


def taxonomy(cfg) -> list[dict]:
    """Failure counts by category, most common first — for the /forensics breakdown."""
    counts = Counter(r["category"] for r in scan(cfg))
    return [{"category": c, "label": _LABELS.get(c, c), "count": n, "action": _ACTIONS.get(c, "")}
            for c, n in counts.most_common()]


def attempts(cfg, ticket_id: str) -> list[dict]:
    """That ticket's failed runs, OLDEST first (a timeline)."""
    runs = [r for r in scan(cfg) if (r.get("ticket_id") or "").lower() == (ticket_id or "").lower()]
    return list(reversed(runs))


def fail_count(cfg, ticket_id: str) -> int:
    return len(attempts(cfg, ticket_id))


def repeat_offenders(cfg, threshold: int = 2) -> list[dict]:
    """Tickets that have failed >= threshold times, worst first."""
    by_ticket: dict[str, list[dict]] = {}
    for r in scan(cfg):
        by_ticket.setdefault(r.get("ticket_id") or "?", []).append(r)
    rows = []
    for tid, runs in by_ticket.items():
        if len(runs) >= threshold:
            dom = Counter(r["category"] for r in runs).most_common(1)[0][0]
            rows.append({"ticket_id": tid, "app": runs[0].get("app"), "count": len(runs),
                         "category": dom, "label": _LABELS.get(dom, dom), "action": _ACTIONS.get(dom, "")})
    rows.sort(key=lambda x: -x["count"])
    return rows


def postmortems_dir(cfg) -> Path:
    return Path(cfg.audit_path).with_name("postmortems")


def postmortem_path(cfg, ticket_id: str) -> Path:
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in (ticket_id or "ticket"))
    return postmortems_dir(cfg) / f"{safe}.md"


def _fmt_when(dt) -> str:
    try:
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return "—"


def write_postmortem(cfg, ticket_id: str) -> Path | None:
    """Write/refresh a deterministic post-mortem for a repeatedly-failing ticket. Returns the path, or
    None if the ticket has no failures on record."""
    att = attempts(cfg, ticket_id)
    if not att:
        return None
    n = len(att)
    cats = Counter(a["category"] for a in att)
    dom, dom_n = cats.most_common(1)[0]
    app = att[0].get("app") or "?"
    timeline = "\n".join(
        f"- **{_fmt_when(a.get('started'))}** · {a['label']} · {(a.get('note') or '').strip()[:160] or '(no note)'}"
        for a in att)
    # any secondary causes worth naming
    others = [f"{_LABELS.get(c, c)} ×{k}" for c, k in cats.most_common() if c != dom]
    secondary = f"\nSecondary causes seen: {', '.join(others)}.\n" if others else ""
    body = (
        f"# Post-mortem — {ticket_id} ({app})\n\n"
        f"_Auto-written by the unit after {n} failed attempts · {time.strftime('%Y-%m-%d %H:%M')}._\n\n"
        f"## Pattern\n\n"
        f"{n} failed attempts. Dominant cause: **{_LABELS.get(dom, dom)}** ({dom_n}/{n})."
        f"{secondary}\n"
        f"## Timeline\n\n{timeline}\n\n"
        f"## Likely cause & recommended fix\n\n{_ACTIONS.get(dom, '')}\n\n"
        f"## What changes the outcome\n\n"
        f"This ticket has cycled {n} times — re-running as-is will likely fail the same way. "
        f"Apply the fix above before the next attempt, then `/unblock {ticket_id}`.\n")
    p = postmortem_path(cfg, ticket_id)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    except OSError:
        return None
    return p


def maybe_postmortem(cfg, report, audit=None) -> Path | None:
    """Called after a failed ticket report: if this ticket has now failed >= postmortem_after times, write
    (or refresh) its post-mortem and audit it. Best-effort — never raises into the run loop."""
    try:
        after = int(getattr(cfg, "postmortem_after", 3) or 0)
        if after <= 0:
            return None
        out_val = getattr(report.outcome, "value", report.outcome)
        if out_val not in _REPORT_FAIL_VALUES:   # the CURRENT run succeeded — don't post-mortem
            return None
        n = fail_count(cfg, report.ticket_id)
        if n < after:
            return None
        path = write_postmortem(cfg, report.ticket_id)
        if path and audit is not None:
            audit.record("postmortem", ticket_id=report.ticket_id, attempts=n, path=str(path))
        return path
    except Exception:  # noqa: BLE001 - forensics must never break a run
        return None


def latest_postmortems(cfg, limit: int = 20) -> list[dict]:
    """Existing post-mortem files, newest first — for the cockpit list."""
    d = postmortems_dir(cfg)
    try:
        files = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    return [{"ticket_id": p.stem, "path": str(p), "mtime": p.stat().st_mtime} for p in files[:limit]]
