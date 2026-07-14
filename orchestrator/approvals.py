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

from . import locking
from .config import Config

# kind -> (label, report filename). Each report maps to an officer `apply` coroutine.
KINDS = {
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


def _mutate_state(cfg: Config, mutate_fn) -> None:
    """Locked read-modify-write of approvals.json (2026-07-05 audit §7.4: approve() runs on a
    server bg thread while disapprove() runs inline on a Flask request thread — the old bare
    _load/_save pair lost one kind's record under that interleaving). Best-effort like the old
    _save: an OSError never crashes an approval."""
    def _mut(st):
        return mutate_fn(st if isinstance(st, dict) else {})
    try:
        locking.locked_rmw(_state_file(cfg), _mut, default={}, corrupt_to_default=True)
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
    """Apply the recommendation (adjutant --apply), then commit + push the doctrine."""
    if kind not in KINDS:
        return f"unknown approval kind: {kind}"
    p = _report_path(cfg, kind)
    if not p.exists():
        return f"no pending {kind} report to apply"
    body = p.read_text(encoding="utf-8")
    h = _hash(body)
    from . import adjutant
    summary = await adjutant.apply(cfg)
    pushed = _commit_push(f"{KINDS[kind][0]} — applied (Commander-approved)")

    def _mark(st: dict) -> dict:
        st[kind] = {"hash": h, "action": "approved", "ts": time.time()}
        return st
    _mutate_state(cfg, _mark)
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

    def _mark(st: dict) -> dict:
        st[kind] = {"hash": h, "action": "disapproved", "reason": reason, "ts": time.time()}
        return st
    _mutate_state(cfg, _mark)
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
# adjutant KINDS above (those are single-report-hash approvals); a batch
# is many tickets the Commander can pick through.
# --------------------------------------------------------------------------- #

_PROPOSAL_HISTORY_CAP = 100   # keep recent actioned batches for audit, bound the file

# A 'filing' claim older than this is presumed dead (SIGKILL between approve_proposals' claim
# and record phases) and becomes actionable again — filing.file_findings' summary de-dup guards
# the partially-filed case on the retry. Normal Jira filing completes in seconds.
_FILING_STALE_S = 15 * 60


def _actionable(b: dict) -> bool:
    """A batch the Commander can still act on: pending, or a stale abandoned 'filing' claim
    (2026-07-06 review: a hard crash mid-filing stranded the batch invisibly forever)."""
    if b.get("status") == "pending":
        return True
    if b.get("status") == "filing":
        try:
            return (time.time() - float(b.get("claim_ts", 0))) > _FILING_STALE_S
        except (TypeError, ValueError):
            return True
    return False


def _proposals_file(cfg: Config) -> Path:
    return Path(cfg.audit_path).with_name("proposals.json")


def _load_proposals(cfg: Config) -> list[dict]:
    try:
        d = json.loads(_proposals_file(cfg).read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _mutate_proposals(cfg: Config, mutate_fn) -> None:
    """Locked read-modify-write of proposals.json — the 2026-07-05 audit's HIGH-severity race:
    the Telegram poller thread (materialize_proposal), Flask request threads (approve/deny),
    server bg threads (council enqueue) and a possible concurrent `general council` PROCESS all
    write this file; the old bare _load/_save pair silently lost whichever update finished first.
    Best-effort like the old _save_proposals: an OSError never crashes the caller."""
    def _mut(items):
        return mutate_fn(items if isinstance(items, list) else [])
    try:
        locking.locked_rmw(_proposals_file(cfg), _mut, default=[], corrupt_to_default=True)
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
    re-run of the same council/meeting doesn't stack duplicate cards.

    The full report body is stored in ``body_raw`` alongside the normalised ``proposals`` list so
    that ``materialize_proposal`` can reconstruct the filing block without re-parsing (EU-89)."""
    if isinstance(report, str):
        body_raw = report
        from . import filing
        proposals, _ = filing.parse_tickets(report)
    else:
        proposals = list(report or [])
        body_raw = json.dumps(proposals)
    clean = _normalize_proposals(proposals)
    if not clean:
        return None
    bid = _hash(f"{source}::" + "||".join(p["title"].lower() for p in clean))

    def _enqueue(items: list[dict]) -> list[dict]:
        for b in items:                              # idempotent: same live batch -> reuse
            # 'filing' counts as live too (2026-07-06 review): re-enqueuing the identical report
            # while its batch is mid-claim appended a DUPLICATE id that could never be approved,
            # denied, or trimmed (_find_batch always returned the first, actioned, copy).
            if b.get("id") == bid and b.get("status") in ("pending", "filing"):
                return items
        items.append({
            "id": bid, "kind": "proposal", "source": source, "app": app_name,
            "label": officer_label, "proposals": clean, "body_raw": body_raw,
            "ts": time.time(), "status": "pending",
        })
        # Bound the file: keep all pending (incl. any mid-flight 'filing' claim — trimming one
        # would strand approve_proposals' record phase) + the most recent actioned batches.
        pending_b = [b for b in items if b.get("status") in ("pending", "filing")]
        actioned = [b for b in items if b.get("status") not in ("pending", "filing")][-_PROPOSAL_HISTORY_CAP:]
        return actioned + pending_b
    _mutate_proposals(cfg, _enqueue)
    return bid


def pending_proposals(cfg: Config) -> list[dict]:
    """Proposal batches still awaiting the Commander's decision (newest first). Includes stale
    abandoned 'filing' claims so a crash mid-approval never hides a batch forever."""
    return [b for b in reversed(_load_proposals(cfg)) if _actionable(b)]


def _find_batch(items: list[dict], batch_id: str) -> dict | None:
    for b in items:
        if b.get("id") == batch_id:
            return b
    return None


def approve_proposals(cfg: Config, batch_id: str, titles=None):
    """Approve a queued batch — file the chosen tickets to the board (de-duped, Roman-default
    create via filing.file_findings). `titles` selects a subset (None/empty = file the whole
    batch). Returns the FilingResult, or None if the batch is unknown/already actioned.

    Two-phase under the proposals lock (2026-07-05 audit §7.4): phase 1 atomically CLAIMS the
    pending batch (status='filing') so a concurrent approve/deny/materialize sees it as already
    actioned; the Jira network I/O then runs OUTSIDE the lock (a hung Jira call must not block
    every other proposals writer, e.g. the Telegram poller); phase 2 records the outcome. If
    filing raises, the claim is released back to 'pending' — same retryable end-state as before.
    A hard process crash mid-filing leaves the batch in 'filing'; after _FILING_STALE_S the
    claim is presumed dead and the batch turns actionable again (filing's summary de-dup guards
    the partially-filed retry), so no crash can hide a batch forever (2026-07-06 review)."""
    from . import filing
    claim: dict = {}

    def _claim(items: list[dict]) -> list[dict]:
        batch = _find_batch(items, batch_id)
        if batch is not None and _actionable(batch):
            batch["status"] = "filing"
            batch["claim_ts"] = time.time()   # lets a dead claim go stale → actionable again
            claim["batch"] = dict(batch)      # snapshot for the out-of-lock filing step
        return items
    _mutate_proposals(cfg, _claim)
    if "batch" not in claim:
        return None
    snapshot = claim["batch"]

    wanted = {str(t).strip().lower() for t in (titles or []) if str(t).strip()}
    selected = [p for p in snapshot.get("proposals", [])
                if not wanted or p["title"].strip().lower() in wanted]
    app = _app_by_name(cfg, snapshot.get("app"))
    result = filing.FilingResult()
    try:
        if selected and app is not None:
            # Reuse filing.file_findings (de-dup + slice-1 Roman-default create) by handing it a
            # ===TICKETS=== block of exactly the approved subset.
            block = "===TICKETS===\n" + json.dumps(selected) + "\n===END==="
            result = filing.file_findings(app, snapshot.get("label", "proposal"), block)
    except BaseException:
        def _release(items: list[dict]) -> list[dict]:
            b = _find_batch(items, batch_id)
            if b is not None and b.get("status") == "filing":
                b["status"] = "pending"
            return items
        _mutate_proposals(cfg, _release)
        raise

    def _record(items: list[dict]) -> list[dict]:
        b = _find_batch(items, batch_id)
        if b is not None:
            b["status"] = "approved"
            b["filed"] = result.filed
            b["deduped"] = result.deduped
            b["selected_titles"] = [p["title"] for p in selected]
            b["actioned_ts"] = time.time()
        return items
    _mutate_proposals(cfg, _record)
    try:
        from . import notify
        notify.send(f"✅ Approved & filed — {snapshot.get('source')}: "
                    f"{result.filed_n} new, {result.deduped_n} already open.")
    except Exception:  # noqa: BLE001
        pass
    return result


def deny_proposals(cfg: Config, batch_id: str, reason: str = "") -> bool:
    """Deny a queued batch — discard it, nothing is filed. Returns True if a pending batch was
    found and cleared."""
    hit = {"ok": False}

    def _deny(items: list[dict]) -> list[dict]:
        batch = _find_batch(items, batch_id)
        if batch is not None and _actionable(batch):
            batch["status"] = "denied"
            batch["reason"] = reason
            batch["actioned_ts"] = time.time()
            hit["ok"] = True
        return items
    _mutate_proposals(cfg, _deny)
    return hit["ok"]


def materialize_proposal(cfg: Config, ticket_ref: str) -> str:
    """File the pending proposal batch whose source mentions ticket_ref — directly to the board,
    without a separate Commander approve prompt.  Used when the Commander replies 'create it' /
    'approve' / 'yes' in the Telegram thread, so the unit acts immediately (EU-89).

    Exactly one matching batch must be found; if zero or more than one match, the ambiguity is
    reported back so the Commander can be more specific.  Uses the stored ``body_raw`` to
    reconstruct the filing block and delegates to ``approve_proposals`` (which applies the
    existing de-dup + Jira create-ticket logic)."""
    ref_up = ticket_ref.strip().upper()
    items = _load_proposals(cfg)
    matched = [
        b for b in items
        if _actionable(b)
        and ref_up in str(b.get("source", "")).upper()
    ]
    if not matched:
        return f"No pending proposals found for {ticket_ref}."
    if len(matched) > 1:
        labels = ", ".join(b.get("source", b.get("id", "?")) for b in matched)
        return (
            f"Multiple pending proposal batches mention {ticket_ref} ({labels}). "
            "Reply with the batch id or be more specific."
        )
    batch = matched[0]
    result = approve_proposals(cfg, batch["id"])
    if result is None:
        return (f"Proposal batch for {ticket_ref} could not be filed "
                "(already actioned or unknown).")
    n_filed = result.filed_n if result else 0
    n_dup = result.deduped_n if result else 0
    return (
        f"✅ Filed {n_filed} ticket(s) for {ticket_ref}"
        + (f" ({n_dup} already open, skipped)" if n_dup else "")
        + "."
    )
