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
3. **Self-healing consumption (EU-231)** — forensics output is CONSUMED, not shelved: every written
   post-mortem files ONE deduped backlog ticket (+ a one-line Telegram note), and a cross-ticket
   crash-signature aggregator (``signature_sweep``) auto-files ONE infra ticket when the same
   normalized failure text recurs across tickets. Before this, 9 postmortems were written and zero
   consumed, and Roman filed EU-221 by hand from a pattern the audit already contained.
"""
from __future__ import annotations

import re
import time
from collections import Counter
from pathlib import Path

from . import dashboard as D
from .officers import display

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
     f"Address the security finding the {display('provost')} flagged — it opened a PR instead of landing."),
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


def scan(cfg, local_only: bool = True) -> list[dict]:
    """Every failed run, newest first, each tagged with its category/label/action.

    EU-428 AC3: ``local_only`` defaults to **True** — scan reads ONLY this host's own audit, never the
    synced ``shared/<peer>.jsonl`` rows. scan is the root of every WRITE the forensics subsystem makes
    (``signature_sweep`` cross-ticket auto-filing + ``_file_postmortem_ticket`` per-ticket filing), so a
    crash that happened ON THE MAC must never auto-file a ticket ON THE SERVER that merely *received*
    the row over sync. Peer rows stay display + liveness only (``dashboard.load_tasks`` default is still
    peer-inclusive). Callers that genuinely need the merged peer view (none in the write path today) pass
    ``local_only=False``."""
    out = []
    for r in D.load_tasks(cfg.audit_path, local_only=local_only):
        if r.get("outcome") in FAILED_OUTCOMES:
            out.append({**r, **classify(r.get("outcome", ""), _reason(r))})
    return out


def taxonomy(cfg, local_only: bool = False) -> list[dict]:
    """Failure counts by category, most common first — for the /forensics breakdown.

    Display caller: ``local_only`` defaults to **False** (peer-inclusive) so the cockpit page still
    mirrors what every host built. The filing path does not route through here."""
    counts = Counter(r["category"] for r in scan(cfg, local_only=local_only))
    return [{"category": c, "label": _LABELS.get(c, c), "count": n, "action": _ACTIONS.get(c, "")}
            for c, n in counts.most_common()]


def attempts(cfg, ticket_id: str, local_only: bool = True) -> list[dict]:
    """That ticket's failed runs, OLDEST first (a timeline).

    EU-428 AC3: ``local_only`` defaults to **True** — a host counts only ITS OWN attempts at a ticket.
    Feeds the post-mortem filing threshold (``maybe_postmortem``) and the Builder's retry hint
    (``loop``), both of which must reflect this host's history, not a peer's."""
    runs = [r for r in scan(cfg, local_only=local_only)
            if (r.get("ticket_id") or "").lower() == (ticket_id or "").lower()]
    return list(reversed(runs))


def fail_count(cfg, ticket_id: str, local_only: bool = True) -> int:
    return len(attempts(cfg, ticket_id, local_only=local_only))


def repeat_offenders(cfg, threshold: int = 2, local_only: bool = False) -> list[dict]:
    """Tickets that have failed >= threshold times, worst first.

    Display caller: ``local_only`` defaults to **False** (peer-inclusive) for the /forensics page."""
    by_ticket: dict[str, list[dict]] = {}
    for r in scan(cfg, local_only=local_only):
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
    """Called after a failed ticket report: run the cross-ticket signature sweep, and if this ticket
    has now failed >= postmortem_after times, write (or refresh) its post-mortem, audit it, and file
    it as a deduped backlog ticket (EU-231). Best-effort — never raises into the run loop.
    ``postmortem_after=0`` disables ALL of it, including auto-filing (the kill switch)."""
    try:
        after = int(getattr(cfg, "postmortem_after", 3) or 0)
        if after <= 0:
            return None
        out_val = getattr(report.outcome, "value", report.outcome)
        if out_val not in _REPORT_FAIL_VALUES:   # the CURRENT run succeeded — don't post-mortem
            return None
        # EU-231b: every failed report re-checks the cross-ticket crash signatures. Runs BEFORE the
        # per-ticket threshold gate — a signature can trip on a ticket's FIRST failure (it needs
        # >=2 tickets, not one ticket failing repeatedly). Deduped by stable title, so at most one
        # open ticket per signature ever files.
        signature_sweep(cfg, audit)
        n = fail_count(cfg, report.ticket_id)
        if n < after:
            return None
        path = write_postmortem(cfg, report.ticket_id)
        if path and audit is not None:
            audit.record("postmortem", ticket_id=report.ticket_id, attempts=n, path=str(path))
        if path:
            # EU-231a: the postmortem's "likely cause & recommended fix" must not die on disk.
            _file_postmortem_ticket(cfg, report.ticket_id, audit)
        return path
    except Exception:  # noqa: BLE001 - forensics must never break a run
        return None


# --------------------------------------------------------------------------- #
# EU-231 — close the self-healing loop. Two deterministic filers (no model call), both best-effort
# and deduped by their STABLE TITLE through filing.file_findings -> backlog.find_open_by_summary
# (the existing EU-42 dedupe — no new store). postmortem_after=0 kill-switches both via
# maybe_postmortem, their only loop-side trigger.

# What varies per run but not per CAUSE: Jira keys, timestamps, filesystem paths, git shas/hex ids,
# line numbers, counts. Strip those and the SAME crash shape from different tickets folds into one
# bucket. Order matters: ticket keys before paths (keys appear inside worktree paths), timestamps
# before the generic number scrub. The number scrub anchors only its LEFT edge so "32s"/"8s"
# both fold to "<N>s" (a right \b would skip digits glued to a unit).
_SIG_SCRUB: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b"), "<TICKET>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?\b"), "<TS>"),
    (re.compile(r"(?:/[\w.@+-]+){2,}"), "<PATH>"),
    (re.compile(r"\b[0-9a-f]{7,64}\b"), "<HASH>"),
    (re.compile(r"\bline \d+\b"), "line <N>"),
    (re.compile(r"\b\d+(?:\.\d+)?"), "<N>"),
]

_SIG_WINDOW_DAYS = 7      # only recent recurrence is a live pattern (mirrors EU-358's window doctrine)
_SIG_MIN_OCCURRENCES = 3  # per the EU-231 spec: >=3 occurrences ...
_SIG_MIN_TICKETS = 2      # ... across >=2 distinct tickets
# worktree_busy is an EXPECTED transient (another run held the tree, auto-retried) — aggregating it
# would file an "infra" ticket for normal contention.
_SIG_SKIP_CATEGORIES = {"worktree_busy"}
# EU-400 — DESIGNED human-handoffs to skip at the SWEEP level (NOT in classify()). 'max passes — PM
# escalated' is the reason loop.py:2685-2686 writes when a run exhausts its pass budget: the ticket is
# ALREADY parked for the Commander via the needs_human event + Telegram notify + Blocked park
# (loop.py:2674-2686). Aggregating it here would auto-file a meta-ticket for the honest escalation
# path — the very outcome that produced this ticket (EU-400). Mirror the worktree_busy rationale: an
# EXPECTED terminal outcome, not a crash. Kept as a sweep-level signature skip so the /forensics
# taxonomy still shows these rows unchanged (classify()/taxonomy() are not touched) and the skip
# cannot mask a genuine crash category. Substring match catches minor wording variants ('PM escalated
# FAIL', 'PM escalated', etc.) without broadening to any real crash text.
_SIG_SKIP_SIGNATURES = ("max passes — pm escalated",)
_SIG_SKIP_SIG_SUBSTRINGS = ("pm escalated",)


def _is_designed_handoff(sig: str) -> bool:
    """True if a normalized failure signature is an EXPECTED designed handoff (already surfaced to
    the Commander per-ticket), not a crash worth aggregating. EU-400."""
    s = sig or ""
    if s in _SIG_SKIP_SIGNATURES:
        return True
    return any(sub in s for sub in _SIG_SKIP_SIG_SUBSTRINGS)


def signature_key(text: str) -> str:
    """Normalize a failure's reason text into a stable cross-ticket signature. Deterministic —
    no model call (EU-231b)."""
    s = str(text or "")
    for pat, repl in _SIG_SCRUB:
        s = pat.sub(repl, s)
    return " ".join(s.lower().split())[:160]


def _self_app(cfg) -> object | None:
    """The configured app whose repo IS this orchestrator itself (the Elite-Unit / EU project),
    else None. 2026-07-20 (live-fire finding): infra signatures — turn-limit, control-request
    timeout, max-passes — are failure classes of the UNIT, so their tickets belong in the unit's
    own backlog. Filing them into the app whose tickets happened to crash (AUTO-200/201/202) put
    unbuildable meta-tickets in the Commander's product queue, where a drain would pick one up and
    burn a real build trying to 'implement' a crash signature inside the product repo."""
    from pathlib import Path
    try:
        me = Path(__file__).resolve().parents[1]
    except OSError:
        return None
    for a in getattr(cfg, "apps", None) or []:
        try:
            if Path(a.repo_path).resolve() == me:
                return a
        except OSError:
            continue
    return None


def _resolve_app(cfg, rows) -> object | None:
    """The first row whose app name resolves to a configured app (rows given newest-first), or None.
    An unresolvable app (renamed/removed from config) skips filing rather than raising."""
    for r in rows:
        name = r.get("app")
        if not name:
            continue
        try:
            return cfg.app(name)
        except Exception:  # noqa: BLE001 - cfg.app raises KeyError for unknown names
            continue
    return None


def _file_one(app_cfg, label: str, proposal: dict) -> str | None:
    """File ONE synthesized finding through filing.file_findings, whose title-dedupe
    (backlog.find_open_by_summary) is the EU-231 dedupe. Returns the key only when NEWLY filed
    (deduped / failed / unsupported backend -> None, so callers notify+audit only on new tickets).
    Imports stay local: filing pulls in the backlog adapters, which the cockpit's thin read paths
    (taxonomy/scan renders) never need."""
    from . import filing
    try:
        res = filing.file_findings(app_cfg, label, filing.make_block([proposal]))
        return res.filed[0] if res.filed else None
    except Exception:  # noqa: BLE001 - a backlog hiccup must never sink forensics
        return None


def _notify_line(text: str) -> None:
    """One-line Telegram note. notify.send never raises; guard the import anyway — forensics must
    never break a run (EU-231)."""
    try:
        from . import notify
        notify.send(text)
    except Exception:  # noqa: BLE001
        pass


def _file_postmortem_ticket(cfg, ticket_id: str, audit=None) -> str | None:
    """EU-231a: file a written postmortem's "likely cause & recommended fix" as one backlog ticket
    + a one-line Telegram note. The title carries ticket_id + dominant category, so the title
    dedupe IS the ticket_id+category dedupe key the spec asks for — and a genuinely SHIFTED
    dominant cause files a new ticket instead of hiding behind the old one."""
    att = attempts(cfg, ticket_id)
    if not att:
        return None
    app_cfg = _resolve_app(cfg, list(reversed(att)))   # newest attempt's app wins
    if app_cfg is None:
        return None
    n = len(att)
    cats = Counter(a["category"] for a in att)
    dom, dom_n = cats.most_common(1)[0]
    timeline = "\n".join(
        f"- {_fmt_when(a.get('started'))} · {a['label']} · {(a.get('note') or '').strip()[:160] or '(no note)'}"
        for a in att)
    title = f"[postmortem] {ticket_id} — {_LABELS.get(dom, dom)}"
    body = (f"Auto-filed by forensics (EU-231) from the post-mortem at "
            f"{postmortem_path(cfg, ticket_id)}.\n\n"
            f"{n} failed attempts; dominant cause: {_LABELS.get(dom, dom)} ({dom_n}/{n}).\n\n"
            f"Recommended fix:\n\n{_ACTIONS.get(dom, '')}\n\n"
            f"Timeline:\n\n{timeline}\n")
    key = _file_one(app_cfg, "postmortem",
                    {"title": title, "type": "Task", "severity": "MEDIUM", "body": body})
    if key:
        if audit is not None:
            audit.record("postmortem_filed", ticket_id=ticket_id, filed=key,
                         attempts=n, category=dom)
        _notify_line(f"📋 Post-mortem filed: {key} — {ticket_id} failed {n}× "
                     f"({_LABELS.get(dom, dom)})")
    return key


def _sig_state_file(cfg) -> Path:
    return Path(getattr(cfg, "audit_path", "./state/audit.jsonl")).with_name("signature_filed.json")


def _sig_last_filed(cfg) -> dict:
    """{normalized signature: unix ts of the last ticket we filed for it}. Empty on any read
    problem — an unreadable ledger must never suppress a genuine new pattern (fail OPEN here:
    the cost of a duplicate ticket is noise; the cost of silence is a missed crash class)."""
    import json as _json
    try:
        data = _json.loads(_sig_state_file(cfg).read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _mark_sig_filed(cfg, sig: str, when: float) -> None:
    """Record that this signature's evidence up to ``when`` has been surfaced. Best-effort."""
    from . import locking

    def _mut(d):
        d = d if isinstance(d, dict) else {}
        d[str(sig)] = float(when)
        return d
    try:
        locking.locked_rmw(_sig_state_file(cfg), _mut, default={}, corrupt_to_default=True)
    except OSError:
        pass


def _row_ts(row: dict) -> float:
    """A failed-run row's start time as a unix ts (0.0 when undatable — such a row can never be
    'newer than the last filing', so it stays out of a re-open decision)."""
    started = row.get("started")
    try:
        return float(started.timestamp()) if started is not None else 0.0
    except (AttributeError, OSError, OverflowError, ValueError):
        return 0.0


def signature_sweep(cfg, audit=None, now: float | None = None) -> list[str]:
    """EU-231b — cross-ticket crash-signature aggregator: scan the last 7 days of failed runs and,
    when the SAME normalized signature appears >= 3 times across >= 2 distinct tickets, auto-file
    ONE deduped infra ticket carrying the raw evidence lines. Deterministic + best-effort; never
    raises into the run loop. Triggered from maybe_postmortem on every failed report; public so a
    cycle hook (autopilot._learn_from_cycle) can also run it. Returns NEWLY-filed keys."""
    try:
        cutoff = float(now if now is not None else time.time()) - _SIG_WINDOW_DAYS * 86400
        groups: dict[str, list[dict]] = {}
        for r in scan(cfg):
            if r.get("category") in _SIG_SKIP_CATEGORIES:
                continue
            started = r.get("started")
            try:
                if started is None or started.timestamp() < cutoff:
                    continue   # undatable rows can't prove recency — leave them out of the window
            except (OSError, OverflowError, ValueError):
                continue
            raw = _reason(r).strip()
            if not raw:
                continue   # nothing to fingerprint
            sig = signature_key(raw)
            if not sig:
                continue
            # EU-400: skip the designed 'max passes — PM escalated' handoff — it is already surfaced
            # to the Commander per-ticket (needs_human + Telegram + Blocked park), not a crash.
            if _is_designed_handoff(sig):
                continue
            groups.setdefault(sig, []).append(r)
        filed: list[str] = []
        # 2026-07-21: only count evidence the Commander has NOT already been shown. Closing a
        # tracker used to guarantee its return: the 7-day window still held the same old failures,
        # so the very next sweep re-filed the identical signature (EU-415/416/401 → EU-422/423/424
        # within two hours of being closed). A filed ticket ACKNOWLEDGES the rows that existed at
        # filing time; only genuinely NEW recurrences may re-open the class.
        seen_before = _sig_last_filed(cfg)
        for sig, rows in groups.items():
            since = seen_before.get(sig, 0.0)
            if since:
                rows = [r for r in rows if _row_ts(r) > since]
            tickets = {r.get("ticket_id") or "?" for r in rows}
            if len(rows) < _SIG_MIN_OCCURRENCES or len(tickets) < _SIG_MIN_TICKETS:
                continue
            # Infra signatures are the UNIT's own defects — file them on the orchestrator's
            # project (EU) when it is a configured app, not into the product backlog whose
            # tickets happened to be the victims. Fall back to the old victim-app routing only
            # when the unit isn't registered as an app (evidence lines still name the victims).
            app_cfg = _self_app(cfg) or _resolve_app(cfg, rows)
            if app_cfg is None:
                continue
            evidence = "\n".join(
                f"- {r.get('ticket_id') or '?'} · {_fmt_when(r.get('started'))} · {_reason(r).strip()[:200]}"
                for r in reversed(rows))   # scan() is newest-first; evidence reads oldest-first
            title = f"[infra-signature] {sig[:110]}"
            body = (f"Auto-filed by forensics.signature_sweep (EU-231): the same normalized failure "
                    f"signature hit {len(rows)}× across {len(tickets)} tickets "
                    f"({', '.join(sorted(tickets))}) within {_SIG_WINDOW_DAYS} days.\n\n"
                    f"Normalized signature:\n\n    {sig}\n\n"
                    f"Raw evidence lines:\n\n{evidence}\n\n"
                    f"This is a cross-ticket pattern — fix the shared cause, not the individual "
                    f"tickets.")
            key = _file_one(app_cfg, "infra-signature",
                            {"title": title, "type": "Bug", "severity": "HIGH", "body": body})
            if key:
                filed.append(key)
                # Acknowledge exactly the evidence this ticket carries: a later sweep counts only
                # rows NEWER than the newest row we just reported, so closing the ticket cannot
                # resurrect it from the same failures.
                _mark_sig_filed(cfg, sig, max((_row_ts(r) for r in rows), default=time.time()))
                if audit is not None:
                    audit.record("crash_signature_filed", filed=key, occurrences=len(rows),
                                 tickets=sorted(tickets), signature=sig[:160])
                _notify_line(f"📋 Crash signature filed: {key} — {len(rows)}× across "
                             f"{len(tickets)} tickets in {_SIG_WINDOW_DAYS}d")
        return filed
    except Exception:  # noqa: BLE001 - forensics must never break a run
        return []


def latest_postmortems(cfg, limit: int = 20) -> list[dict]:
    """Existing post-mortem files, newest first — for the cockpit list."""
    d = postmortems_dir(cfg)
    try:
        files = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    return [{"ticket_id": p.stem, "path": str(p), "mtime": p.stat().st_mtime} for p in files[:limit]]
