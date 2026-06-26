"""Approvals inbox — the officers PROPOSE, the Commander approves, the unit APPLIES.

The Engineering Coach (doctrine upgrades) and the Engineering Manager (personnel actions) are propose-only: they
write a report and stop. This collects those pending recommendations so the Commander can
**Approve** (the unit runs the matching `--apply`, then commits + pushes the doctrine to The-General)
or **Disapprove** (cleared, and the reason is logged to Unit Memory so it isn't re-proposed).

It's the home for overnight self-improvement too: the unit drills at night, you approve in the
morning. Only `officers/` is committed — never a blanket `git add -A` — so nothing unrelated ships.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

from .config import Config

# kind -> (label, report filename). Each report maps to an officer `apply` coroutine.
KINDS = {
    "drill": ("Engineering Coach — doctrine upgrade", "drill-report.md"),
    "adjutant": ("Engineering Manager — personnel action", "adjutant-report.md"),
}


def _state_file(cfg: Config) -> Path:
    return Path(cfg.audit_path).with_name("approvals.json")


def _load(cfg: Config) -> dict:
    try:
        d = json.loads(_state_file(cfg).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(cfg: Config, st: dict) -> None:
    try:
        _state_file(cfg).write_text(json.dumps(st), encoding="utf-8")
    except OSError:
        pass


def _report_path(cfg: Config, kind: str) -> Path:
    return Path(cfg.audit_path).with_name(KINDS[kind][1])


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def pending(cfg: Config) -> list[dict]:
    """Officer recommendations awaiting a decision — a report that exists and whose current
    content hasn't already been approved/disapproved."""
    st = _load(cfg)
    out: list[dict] = []
    for kind, (label, _fname) in KINDS.items():
        p = _report_path(cfg, kind)
        try:
            body = p.read_text(encoding="utf-8") if p.exists() else ""
        except OSError:
            body = ""
        if not body.strip():
            continue
        h = _hash(body)
        if (st.get(kind) or {}).get("hash") == h:   # this exact report already actioned
            continue
        out.append({"kind": kind, "label": label, "body": body, "hash": h})
    return out


def _commit_push(msg: str) -> str:
    """Commit ONLY officers/ (what an apply touches) and push. Never a blanket add -A."""
    root = str(Path(__file__).resolve().parent.parent)
    try:
        subprocess.run(["git", "-C", root, "add", "officers/"], capture_output=True, timeout=30)
        c = subprocess.run(["git", "-C", root, "commit", "-m", msg],
                           capture_output=True, text=True, timeout=30)
        if "nothing to commit" in (c.stdout + c.stderr).lower():
            return "applied (no officer-file change to push)"
        p = subprocess.run(["git", "-C", root, "push"], capture_output=True, text=True, timeout=60)
        return "committed + pushed to origin" if p.returncode == 0 \
            else f"committed; push failed: {(p.stderr or '').strip()[:140]}"
    except Exception as exc:  # noqa: BLE001 - commit/push must not crash the approval
        return f"commit/push error: {exc}"


async def approve(cfg: Config, kind: str) -> str:
    """Apply the recommendation (drill/adjutant --apply), then commit + push the doctrine."""
    if kind not in KINDS:
        return f"unknown approval kind: {kind}"
    p = _report_path(cfg, kind)
    if not p.exists():
        return f"no pending {kind} report to apply"
    body = p.read_text(encoding="utf-8")
    h = _hash(body)
    if kind == "drill":
        from . import drillmaster
        summary = await drillmaster.apply(cfg)
    else:
        from . import adjutant
        summary = await adjutant.apply(cfg)
    pushed = _commit_push(f"{KINDS[kind][0]} — applied (Commander-approved)")
    st = _load(cfg)
    st[kind] = {"hash": h, "action": "approved", "ts": time.time()}
    _save(cfg, st)
    try:
        from . import notify
        notify.send(f"✅ Approved & applied — {KINDS[kind][0]}. {pushed}")
    except Exception:  # noqa: BLE001
        pass
    return (summary or "") + "\n\n" + pushed


def disapprove(cfg: Config, kind: str, reason: str = "") -> None:
    """Clear the recommendation and log WHY to Unit Memory so it isn't re-proposed."""
    if kind not in KINDS:
        return
    p = _report_path(cfg, kind)
    h = _hash(p.read_text(encoding="utf-8")) if p.exists() else ""
    st = _load(cfg)
    st[kind] = {"hash": h, "action": "disapproved", "reason": reason, "ts": time.time()}
    _save(cfg, st)
    try:
        from . import council
        council.add_commander_note(cfg, f"Disapproved {KINDS[kind][0]}: {reason or '(no reason given)'}")
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Unit-proposed tickets awaiting the Commander (EU-61 Part B).
#
# A second, distinct approval kind: batches of fileable findings (from
# filing.parse_tickets) raised by a council/daily, an ad-hoc meeting, or an
# out-of-scope in-dev finding. Instead of going straight to the board, each
# batch lands in this queue so the Commander can Approve (file to the board,
# de-duped, with slice-1's Roman-default create) — optionally only a chosen
# subset of the tickets — or Deny (discard). This is separate state from the
# drill/adjutant KINDS above (those are single-report-hash approvals); a batch
# is many tickets the Commander can pick through.
# --------------------------------------------------------------------------- #

_PROPOSAL_HISTORY_CAP = 100   # keep recent actioned batches for audit, bound the file


def _proposals_file(cfg: Config) -> Path:
    return Path(cfg.audit_path).with_name("proposals.json")


def _load_proposals(cfg: Config) -> list[dict]:
    try:
        d = json.loads(_proposals_file(cfg).read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_proposals(cfg: Config, items: list[dict]) -> None:
    try:
        _proposals_file(cfg).write_text(json.dumps(items), encoding="utf-8")
    except OSError:
        pass


def _app_by_name(cfg: Config, name: str):
    apps = getattr(cfg, "apps", None) or []
    for a in apps:
        if getattr(a, "name", None) == name:
            return a
    return apps[0] if apps else None   # fall back to the primary app


def _normalize_proposals(raw: list[dict]) -> list[dict]:
    """Keep only real proposals, de-duped by title within the batch."""
    out: list[dict] = []
    seen: set[str] = set()
    for p in raw:
        if not isinstance(p, dict):
            continue
        title = str(p.get("title", "")).strip()
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        out.append({
            "title": title,
            "type": str(p.get("type", "Task")) or "Task",
            "severity": str(p.get("severity", "?")),
            "body": str(p.get("body", "")),
        })
    return out


def enqueue_proposals(cfg: Config, *, app_name: str, officer_label: str, source: str,
                      report) -> str | None:
    """Queue a batch of unit-proposed tickets for the Commander's Approve/Deny instead of filing
    them straight to the board. `report` may be a raw report string (parsed with
    filing.parse_tickets) or an already-parsed list of proposal dicts. Returns the batch id, or
    None when there's nothing fileable. Identical pending batches collapse (idempotent) so a
    re-run of the same council/meeting doesn't stack duplicate cards."""
    if isinstance(report, str):
        from . import filing
        proposals, _ = filing.parse_tickets(report)
    else:
        proposals = list(report or [])
    clean = _normalize_proposals(proposals)
    if not clean:
        return None
    bid = _hash(f"{source}::" + "||".join(p["title"].lower() for p in clean))
    items = _load_proposals(cfg)
    for b in items:                                  # idempotent: same pending batch -> reuse
        if b.get("id") == bid and b.get("status") == "pending":
            return bid
    items.append({
        "id": bid, "kind": "proposal", "source": source, "app": app_name,
        "label": officer_label, "proposals": clean, "ts": time.time(), "status": "pending",
    })
    # Bound the file: keep all pending + the most recent actioned batches.
    pending_b = [b for b in items if b.get("status") == "pending"]
    actioned = [b for b in items if b.get("status") != "pending"][-_PROPOSAL_HISTORY_CAP:]
    _save_proposals(cfg, actioned + pending_b)
    return bid


def pending_proposals(cfg: Config) -> list[dict]:
    """Proposal batches still awaiting the Commander's decision (newest first)."""
    return [b for b in reversed(_load_proposals(cfg)) if b.get("status") == "pending"]


def _find_batch(items: list[dict], batch_id: str) -> dict | None:
    for b in items:
        if b.get("id") == batch_id:
            return b
    return None


def approve_proposals(cfg: Config, batch_id: str, titles=None):
    """Approve a queued batch — file the chosen tickets to the board (de-duped, Roman-default
    create via filing.file_findings). `titles` selects a subset (None/empty = file the whole
    batch). Returns the FilingResult, or None if the batch is unknown/already actioned."""
    from . import filing
    items = _load_proposals(cfg)
    batch = _find_batch(items, batch_id)
    if batch is None or batch.get("status") != "pending":
        return None
    wanted = {str(t).strip().lower() for t in (titles or []) if str(t).strip()}
    selected = [p for p in batch.get("proposals", [])
                if not wanted or p["title"].strip().lower() in wanted]
    app = _app_by_name(cfg, batch.get("app"))
    result = filing.FilingResult()
    if selected and app is not None:
        # Reuse filing.file_findings (de-dup + slice-1 Roman-default create) by handing it a
        # ===TICKETS=== block of exactly the approved subset.
        block = "===TICKETS===\n" + json.dumps(selected) + "\n===END==="
        result = filing.file_findings(app, batch.get("label", "proposal"), block)
    batch["status"] = "approved"
    batch["filed"] = result.filed
    batch["deduped"] = result.deduped
    batch["selected_titles"] = [p["title"] for p in selected]
    batch["actioned_ts"] = time.time()
    _save_proposals(cfg, items)
    try:
        from . import notify
        notify.send(f"✅ Approved & filed — {batch.get('source')}: "
                    f"{result.filed_n} new, {result.deduped_n} already open.")
    except Exception:  # noqa: BLE001
        pass
    return result


def deny_proposals(cfg: Config, batch_id: str, reason: str = "") -> bool:
    """Deny a queued batch — discard it, nothing is filed. Returns True if a pending batch was
    found and cleared."""
    items = _load_proposals(cfg)
    batch = _find_batch(items, batch_id)
    if batch is None or batch.get("status") != "pending":
        return False
    batch["status"] = "denied"
    batch["reason"] = reason
    batch["actioned_ts"] = time.time()
    _save_proposals(cfg, items)
    return True
