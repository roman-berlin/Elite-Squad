"""Memory consolidation + learning from rejections.

Two deterministic, zero-cost passes that compound the unit's learning over time:

1. **Consolidate the Lessons log** — dedup near-identical bullets (same lesson, ignoring its date) and
   prune to the newest ``max_bullets`` so ``UNIT.live.md`` stays tight instead of growing forever.
2. **Learn from rejections** — scan the Reviewer's FAIL verdicts in the audit log; when the same *kind*
   of change is demanded across >= ``min_count`` distinct tickets, fold a one-line lesson into the log
   ("Reviewer repeatedly required X — do Y"). So the unit stops repeating the mistakes its own
   Reviewer keeps catching. (The old "drill proposal" surfacing left with the drillmaster, EU-327.)
3. **Learn from recurring failure classes (EU-399)** — the rejection themes only cover 8 hardcoded
   keywords. The classes that actually dominate the live log — needs_human reasons, normalized
   ticket_exception signatures, and stuck gate fingerprints — are bucketed deterministically and, when
   any bucket recurs across >= 3 distinct tickets, folded into the SAME log as one standing lesson.
   So a class of escalation repeating across many tickets finally yields Planner/Builder guidance.

No model call — it reads the audit log and the log file it already keeps, so it runs unattended and free.
The Technical Writer calls ``run()`` after it writes, and ``general consolidate`` / the cockpit expose it directly.
"""
from __future__ import annotations

import json
import re
from datetime import date

from . import dashboard as D

# Recurring rejection themes, most-specific first. Each: (key, label, keywords, recommended action).
_THEMES: list[tuple[str, str, tuple[str, ...], str]] = [
    ("tenant_isolation", "an explicit tenant filter",
     ("business_id", "tenant_id", "tenant", "rls", "cross-tenant", "cross tenant"),
     "Add an explicit business_id/tenant_id filter to EVERY query — RLS is defense-in-depth, not the boundary."),
    ("missing_tests", "tests with the change",
     ("test", "coverage", "regression", "vitest", "pytest", "untested"),
     "Write tests WITH the code — happy path + a regression for the bug — before requesting review."),
    ("typing", "complete typing",
     ("type annotation", "typing", "type hint", "bare dict", "no any", "`any`", " any ", "tsc", "mypy"),
     "Annotate fully; no `any` / bare dict; run the typecheck gate locally before review."),
    ("error_handling", "error & edge-case handling",
     ("error handling", "exception", "edge case", "null check", "none check", "bare except", "guard clause"),
     "Handle failure paths + edge cases explicitly; fail fast over fail-silent."),
    ("validation", "input validation",
     ("validation", "validate", "pydantic", "schema", "sanitize input"),
     "Validate inputs against a schema (Pydantic) before acting on them."),
    ("security", "the security fix",
     ("security", "secret", "injection", "authz", "auth check", "vulnerab"),
     "Close the security gap (secrets, authz, injection) before it reaches review."),
    ("naming_style", "the project's style/lint",
     ("naming", "rename", "lint", "formatting", "convention", "unused import", "dead code"),
     "Follow the project's naming + lint conventions; clear the linter before review."),
    ("docs", "docstrings / comments",
     ("docstring", "comment", "documentation", "undocumented"),
     "Add a concise docstring/comment explaining the why."),
]
_LABELS = {k: lbl for k, lbl, _, _ in _THEMES}
_ACTIONS = {k: act for k, _, _, act in _THEMES}


def _theme_of(text: str) -> str | None:
    blob = (text or "").lower()
    for key, _lbl, kws, _act in _THEMES:
        if any(k in blob for k in kws):
            return key
    return None


def rejection_patterns(cfg, *, min_count: int = 2) -> list[dict]:
    """Recurring Reviewer-rejection themes: a theme demanded across >= min_count DISTINCT tickets.
    Returns [{theme, label, count, tickets, action}] sorted by count desc."""
    by_theme: dict[str, set[str]] = {}
    for line in D.audit_lines(cfg.audit_path):
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if ev.get("event") != "review" or str(ev.get("verdict", "")).upper() != "FAIL":
            continue
        tid = ev.get("ticket_id") or "?"
        texts = list(ev.get("required_changes") or [])
        for iss in (ev.get("issues") or []):
            if isinstance(iss, dict):
                texts.append(f"{iss.get('area', '')} {iss.get('detail', '')}")
        seen_here: set[str] = set()
        for t in texts:
            theme = _theme_of(t)
            if theme and theme not in seen_here:        # count each theme once per ticket
                by_theme.setdefault(theme, set()).add(tid)
                seen_here.add(theme)
    out = []
    for theme, tickets in by_theme.items():
        if len(tickets) >= min_count:
            out.append({"theme": theme, "label": _LABELS[theme], "count": len(tickets),
                        "tickets": sorted(tickets), "action": _ACTIONS[theme]})
    out.sort(key=lambda p: -p["count"])
    return out


# --------------------------------------------------------------------------- #
# EU-399 — learn from the failure classes that actually dominate the live log,
# not just reviewer-rejection keywords. Three deterministic buckets, each counted
# across DISTINCT tickets (a standing lesson guides FUTURE, different tickets — a
# single ticket failing repeatedly is the postmortem's job, not a standing lesson):
#   * needs_human           -> bucketed by its `reason` field
#   * ticket_exception      -> bucketed by forensics.signature_key(error) (the EXISTING
#                              normalizer this ticket briefs on — no second normalizer to drift)
#   * gate_fingerprint_stuck -> bucketed by its `fingerprint` field
# A bucket recurring across >= min_tickets distinct tickets yields ONE lesson, deduped
# against the log on fold (see run()). No model call — it reads the same audit log
# rejection_patterns() does, so it is free and runs unattended on every cycle/scribe.
#
# Threshold = DISTINCT TICKETS (not raw occurrences): matches rejection_patterns'
# discipline and the ticket's own framing ("a class repeating across 10 tickets").
# A window is deliberately NOT applied — rejection_patterns (the sibling this extends)
# scans the whole log, and these are meant to be *standing* (durable) lessons; the
# threshold itself is the noise filter.
#
# EU-400 note: forensics.signature_sweep SKIPS the designed 'max passes — PM escalated'
# handoff so it won't auto-FILE a noise meta-ticket. That rationale is about ticket-filing
# noise and does NOT transfer here — a standing lesson from max-pass recurrence is exactly
# the Planner/Builder guidance this loop exists to surface, so needs_human reasons are
# bucketed verbatim (including that one). Different surface, different cost/benefit.
_NH_ACTIONS: dict[str, str] = {
    "turn-limit": "tickets keep exceeding the turn budget — scope them smaller or raise "
                  "builder_max_turns before re-queueing.",
    "max passes — PM escalated": "the Reviewer kept rejecting across passes — tighten the ticket's "
                                 "exit criteria / build quality rather than retrying the same pass.",
    "product blocker — escalated to Commander": "these need a product/IA decision up front — surface "
                                                 "the open question in the Planner so it's answered before build.",
}
_NH_ACTION_DEFAULT = ("this escalation class keeps recurring across tickets — address the shared "
                      "cause before the next pick.")


def _lesson(kind: str, key: str, count: int, tickets: list[str], phrase: str, action: str) -> dict:
    return {"kind": kind, "key": key, "count": count, "tickets": tickets,
            "phrase": phrase, "action": action}


def recurrence_lessons(cfg, *, min_tickets: int = 3) -> list[dict]:
    """EU-399 — recurring failure-class lessons from the live audit log. Buckets needs_human by
    `reason`, ticket_exception by `forensics.signature_key(error)`, and gate_fingerprint_stuck by
    `fingerprint`; any bucket spanning >= ``min_tickets`` DISTINCT tickets yields one lesson
    (``{kind, key, count, tickets, phrase, action}``), sorted by count desc. Free + deterministic.
    """
    try:
        from . import forensics      # reuse the EXISTING signature normalizer (deferred: keeps the
    except Exception:                # module's import surface unchanged; forensics never imports consolidate)
        forensics = None
    nh: dict[str, set[str]] = {}     # reason      -> distinct tickets
    ex: dict[str, set[str]] = {}     # signature   -> distinct tickets
    gs: dict[str, set[str]] = {}     # fingerprint -> distinct tickets
    for line in D.audit_lines(cfg.audit_path):
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        name = ev.get("event")
        tid = ev.get("ticket_id") or "?"
        if name == "needs_human":
            reason = str(ev.get("reason") or "").strip()
            if reason:
                nh.setdefault(reason, set()).add(tid)
        elif name == "ticket_exception":
            err = str(ev.get("error") or "").strip()
            if err and forensics is not None:
                sig = forensics.signature_key(err)
                if sig:
                    ex.setdefault(sig, set()).add(tid)
        elif name == "gate_fingerprint_stuck":
            fp = str(ev.get("fingerprint") or "").strip()
            if fp:
                gs.setdefault(fp, set()).add(tid)
    out: list[dict] = []
    for reason, tickets in nh.items():
        if len(tickets) >= min_tickets:
            t = sorted(tickets)
            phrase = f"Needs-human reason '{reason}' recurred"
            out.append(_lesson("needs_human", reason, len(tickets), t, phrase,
                               _NH_ACTIONS.get(reason, _NH_ACTION_DEFAULT)))
    for sig, tickets in ex.items():
        if len(tickets) >= min_tickets:
            t = sorted(tickets)
            phrase = f"Error signature '{sig[:80]}' recurred"
            out.append(_lesson("ticket_exception", sig, len(tickets), t, phrase,
                               "a normalized error signature keeps crashing builds — fix the shared "
                               "cause (or classify it infra via orchestrator/infra_classify.py) "
                               "rather than retrying per ticket."))
    for fp, tickets in gs.items():
        if len(tickets) >= min_tickets:
            t = sorted(tickets)
            phrase = f"Stuck gate fingerprint '{fp[:80]}' recurred"
            out.append(_lesson("gate_stuck", fp, len(tickets), t, phrase,
                               "the same gate-failure fingerprint is sticking across tickets — "
                               "investigate the shared gate breakage instead of rebuilding."))
    out.sort(key=lambda p: -p["count"])
    return out


# --------------------------------------------------------------------------- #
# EU-837 — filing_precision: deterministic autofile quality scores per officer.
# Reads the audit log, filters is_autofiled terminal events, buckets by source_officer,
# computes precision = merged / total, excludes officers below min_outcomes,
# returns results sorted by precision ascending (worst first). Free + deterministic.
# --------------------------------------------------------------------------- #
_BUCKET_MAP = {
    "land_pushed": "merged",
    "no_changes": "closed_unchanged",
    "needs_human": "escalated",
}


def filing_precision(cfg, *, min_outcomes: int = 5) -> list[dict]:
    """Bucket autofiled terminal events by source_officer and compute precision = merged / total.

    Returns ``[{officer, merged, escalated, closed_unchanged, total, precision}]`` sorted
    by precision ascending (worst first). Officers with ``total < min_outcomes`` are silently
    excluded. Returns ``[]`` when no autofiled terminal events exist.
    """
    buckets: dict[str, dict[str, int]] = {}
    for line in D.audit_lines(cfg.audit_path):
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not ev.get("is_autofiled"):
            continue
        mapped = _BUCKET_MAP.get(ev.get("event"))
        if not mapped:
            continue
        officer = ev.get("source_officer")
        if not officer:
            continue
        buckets.setdefault(officer, {"merged": 0, "escalated": 0, "closed_unchanged": 0})
        buckets[officer][mapped] += 1
    out: list[dict] = []
    for officer, counts in buckets.items():
        total = counts["merged"] + counts["escalated"] + counts["closed_unchanged"]
        if total >= min_outcomes:
            out.append({
                "officer": officer,
                "merged": counts["merged"],
                "escalated": counts["escalated"],
                "closed_unchanged": counts["closed_unchanged"],
                "total": total,
                "precision": round(counts["merged"] / total, 10),
            })
    out.sort(key=lambda r: r["precision"])
    return out


# --------------------------------------------------------------------------- #
# Lessons-log consolidation (dedup + prune)
_DATE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s*[:\-—]\s*")


def _parse_bullets(raw: str) -> list[str]:
    """Bullet bodies (without the leading '- '), in file order (newest first)."""
    out = []
    for line in (raw or "").splitlines():
        s = line.strip()
        if s.startswith("- "):
            out.append(s[2:].strip())
    return out


def _norm(body: str) -> str:
    """Normalize a lesson for dedup: drop the leading date, lowercase, keep alphanumerics only."""
    body = _DATE.sub("", body or "")
    return re.sub(r"[^a-z0-9]+", " ", body.lower()).strip()


def _dedup(bullets: list[str]) -> tuple[list[str], int]:
    seen: set[str] = set()
    out: list[str] = []
    removed = 0
    for b in bullets:
        n = _norm(b)
        if not n or n in seen:
            removed += 1
            continue
        seen.add(n)
        out.append(b)
    return out, removed


def consolidate_log(raw: str, *, max_bullets: int = 24) -> tuple[str, dict]:
    """Dedup + prune the raw living-log text. Returns (new bullet block, report)."""
    bullets = _parse_bullets(raw)
    deduped, removed = _dedup(bullets)
    pruned = max(0, len(deduped) - max_bullets)
    kept = deduped[:max_bullets]
    block = "\n".join(f"- {b}" for b in kept)
    return block, {"before": len(bullets), "removed_dupes": removed, "pruned": pruned, "kept": len(kept)}


def run(cfg, *, write: bool = True, max_bullets: int = 24, min_count: int = 2,
        min_tickets: int = 3) -> dict:
    """Fold recurring lessons into the log, then dedup + prune it. Best-effort; never raises.

    Two lesson sources, both folded through the SAME idempotent/deduped path so they share one cap:
      * rejection_patterns (``min_count`` distinct tickets) — the 8 reviewer-rejection themes.
      * recurrence_lessons  (``min_tickets`` distinct tickets) — EU-399: needs_human reasons,
        ticket_exception signatures, and stuck gate fingerprints. New on top of the keyword themes.
    """
    try:
        from . import memory
        try:
            raw = memory.LIVE_PATH.read_text(encoding="utf-8")
        except OSError:
            raw = ""
        bullets = _parse_bullets(raw)
        patterns = rejection_patterns(cfg, min_count=min_count)
        recurrences = recurrence_lessons(cfg, min_tickets=min_tickets)
        today = date.today().isoformat()
        added: list[str] = []

        def _fold(phrase: str, count: int, tickets: list[str], action: str) -> None:
            """Insert one lesson at the top unless its (count-independent) headline is already in the
            log — idempotent across runs. Same shape + cap discipline as every other lesson."""
            if any(phrase.lower() in b.lower() for b in bullets):
                return
            bullet = (f"{today}: {phrase} ({count} tickets, e.g. {tickets[0]}) — {action}")
            bullets.insert(0, bullet)
            added.append(bullet)

        for p in patterns:
            _fold(f"Reviewer repeatedly required {p['label']}", p["count"], p["tickets"], p["action"])
        for r in recurrences:
            _fold(r["phrase"], r["count"], r["tickets"], r["action"])
        deduped, removed = _dedup(bullets)
        pruned = max(0, len(deduped) - max_bullets)
        kept = deduped[:max_bullets]
        if write and (added or removed or pruned):
            memory.update_log("\n".join(f"- {b}" for b in kept))
        return {"patterns": patterns, "recurrences": recurrences, "added": added,
                "removed_dupes": removed, "pruned": pruned, "kept": len(kept),
                "written": bool(write and (added or removed or pruned)),
                "filing_precision": filing_precision(cfg)}
    except Exception as exc:  # noqa: BLE001 - memory hygiene must never break a run
        return {"patterns": [], "recurrences": [], "added": [], "removed_dupes": 0, "pruned": 0,
                "kept": 0, "written": False, "error": str(exc),
                "filing_precision": []}
