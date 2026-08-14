# Telegram notifications — send messages and receive updates from the Commander's ops chat.
"""Telegram notifications.

Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment to enable.
If they're unset, every call is a safe no-op (the run is never affected).

Setup (one time):
  1. In Telegram, message @BotFather -> /newbot -> copy the token.
  2. Message your new bot once (say "hi"), then visit
     https://api.telegram.org/bot<TOKEN>/getUpdates and copy the chat id.
  3. export TELEGRAM_BOT_TOKEN=... ; export TELEGRAM_CHAT_ID=...
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import requests

from . import locking

# Phone reports (EU-62): a verbose report is SUMMARISED into a COMPLETE bulleted brief — every decision,
# action item, and blocker in tight bullets — never a truncated prefix that ends with "…(full report in
# the cockpit)". The phone is the primary read, so the message must stand on its own. Text already short
# enough to skim is sent verbatim; anything longer is distilled by the cheap model (`report_brief`), with a
# deterministic bulletizer (`bulletize`) as the never-fails fallback so a report is never lost.
_BRIEF_PASSTHROUGH = 360   # ≤ this many chars is already phone-skimmable — send as-is, no model call
_BULLET = "• "

_REPORT_BRIEF_SYSTEM = (
    "You turn one long officer report into a COMPLETE brief a busy engineer reads on his phone. "
    "Output ONLY a tight bullet list — one '• ' per line, with nothing before or after the bullets. "
    "Keep EVERY essential point: each decision, each action item (who / what / next), and each blocker, "
    "one per bullet. Summarise in your own words; do NOT copy sentences verbatim and do NOT cut the "
    "report off. NEVER write 'see the cockpit', 'full report in the cockpit', or any 'continued "
    "elsewhere' note — the bullets must stand on their own. Aim for 4–8 bullets, ≤ ~110 words total."
)


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


# EU-788: per-send alerting health tracking — so a dead Telegram channel shows on the
# cockpit health surface instead of vanishing into a throttled log line. The module tracks
# the LATEST send outcome (ok / failed), its timestamp, and why it failed; ``alerting_status()``
# exposes it for health checks. A successful send flips it back to "ok"; a failure records
# degraded. Never throttled for health state — only the console warning is throttled.

_alerting_outcome: dict = {"status": "ok", "ts": None, "reason": ""}


def alerting_status() -> dict[str, str | None]:
    """Return the latest Telegram send outcome: {status, ts, reason}.

    ``status`` is "ok" (the last send succeeded) or "failed" (a send failed or raised).
    ``ts`` is the epoch when the last non-ok event was recorded (None while still "ok").
    ``reason`` carries why it failed (short string)."""
    return _alerting_outcome.copy()


def _set_alerting(status: str, reason: str = "") -> None:
    """Record an alerting outcome change. Called from send() — never throttled."""
    if status == "ok":
        _alerting_outcome["status"] = "ok"
        _alerting_outcome["ts"] = None
        _alerting_outcome["reason"] = ""
    else:
        _alerting_outcome["status"] = status
        _alerting_outcome["ts"] = time.time()
        _alerting_outcome["reason"] = reason[:200]  # cap for audit hygiene


def bulletize(text: str, max_bullets: int = 9, max_chars: int = 1000) -> str:
    """Deterministically fold a report into a tight, phone-skimmable bullet list — the never-fails
    fallback behind ``report_brief`` when the cheap model is unavailable.

    Existing bullet / numbered lines are normalised to '• '; a single prose paragraph is split into one
    bullet per sentence. The list is capped to stay skimmable, but each bullet is a COMPLETE thought
    (never cut mid-word) and there is NO 'continue in the cockpit' footer — the report is summarised, not
    truncated-and-punted, so it stands on its own and is never lost."""
    text = (text or "").strip()
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    _marker = r"^([-*•]|\d+[.)])\s+"
    if len(lines) > 1:                         # a multi-line list -> one bullet per line, markers normalised
        points = [re.sub(_marker, "", ln) for ln in lines]
    else:                                      # one prose blob -> strip any marker, then split into sentences
        one = re.sub(_marker, "", lines[0])
        points = re.split(r"(?<=[.!?])\s+", one)
    points = [p.strip() for p in points if p.strip()]
    out: list[str] = []
    used = 0
    for p in points:
        if len(out) >= max_bullets or (out and used + len(p) > max_chars):
            break
        out.append(_BULLET + p)
        used += len(p) + len(_BULLET)
    return "\n".join(out)


async def distill(cfg, *, system: str, user: str, tag: str, fallback) -> str:
    """Run the CHEAP model once to compress ``user`` under ``system`` — the shared engine behind
    ``report_brief`` and the loop's decision brief (EU-62 generalised both onto this). Reads/writes
    nothing, one turn, low effort. NEVER raises and never returns empty: on any model error (or an empty
    answer) it returns ``fallback()`` so a report or escalation is summarised, never lost."""
    try:
        from claude_agent_sdk import ClaudeAgentOptions

        from .agent import run_agent
        run = await run_agent(user, ClaudeAgentOptions(
            model=getattr(cfg, "smalltalk_model", None) or getattr(cfg, "discussion_model", None),
            system_prompt=system, permission_mode="bypassPermissions",
            allowed_tools=[], disallowed_tools=["Task", "Agent"],  # no sub-agent fan-out (AUTO-93 class fix)
            setting_sources=[], max_turns=1, effort="low"), tag=tag)
        return (run.final or run.text or "").strip() or fallback()
    except Exception:  # noqa: BLE001 - summarisation must never break a notification
        return fallback()


async def report_brief(cfg, raw: str) -> str:
    """Distil a verbose officer report into a COMPLETE, phone-skimmable BULLETED brief — every decision,
    action item, and blocker in tight bullets, NOT a truncated prefix with a 'full report in the cockpit'
    punt. Runs on the cheap small-talk model; a report already short enough to skim is returned verbatim
    (no model call), and any model error falls back to the deterministic ``bulletize`` so a report is
    never lost. Replaces the old ``clip`` truncate-and-punt on every report send."""
    raw = (raw or "").strip()
    if len(raw) <= _BRIEF_PASSTHROUGH:         # already a phone line or two — keep it natural, no model call
        return raw
    return await distill(cfg, system=_REPORT_BRIEF_SYSTEM,
                         user="Report to compress into the bullet brief:\n\n" + raw[:3500],
                         tag="report-brief", fallback=lambda: bulletize(raw))


# Telegram's hard per-message ceiling; anything longer is rejected with HTTP 400 (message lost).
_TG_MAX_CHARS = 4096


def _tg_chunks(text: str, limit: int = _TG_MAX_CHARS) -> list[str]:
    """Split an over-long message into <=limit pieces, preferring newline boundaries so an
    escalation report arrives as readable consecutive messages instead of vanishing (EU-358)."""
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 1, limit)
        if cut < limit // 2:          # no usable newline — hard cut rather than tiny fragments
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest)
    return chunks or [""]


_PHONE_MAX_CHARS = 900     # ~a phone screenful; above this a report stops being skimmable


def brief_for_phone(text: str) -> str:
    """Fold an over-long notification into a skimmable brief. Deterministic and never-fails.

    Applied by ``send`` to every outbound message. Anything at or under ``_PHONE_MAX_CHARS`` is
    returned untouched — the goal is to stop the occasional wall of text, not to reformat the
    one-line events that make up most of the traffic.

    A header line (the first line, which carries the emoji + ticket id the Commander scans for) is
    always preserved verbatim; only the body is bulletized. On any failure the original text is
    returned — a brevity helper must never be able to swallow a notification."""
    try:
        if not text or len(text) <= _PHONE_MAX_CHARS:
            return text
        lines = text.splitlines()
        head = lines[0].strip() if lines else ""
        body = "\n".join(lines[1:]).strip()
        if not body:
            return text
        folded = bulletize(body, max_bullets=8, max_chars=_PHONE_MAX_CHARS - len(head) - 2)
        if not folded.strip():
            return text
        return f"{head}\n{folded}" if head else folded
    except Exception:  # noqa: BLE001 — never let brevity lose a message
        return text


def jira_brief(text: str, max_chars: int = 1400) -> str:
    """Fold an officer's report into a brief, skimmable JIRA comment. Deterministic, never raises.

    2026-07-23 (Commander, on the EU-445 PM triage comment): ticket comments were "too long, too
    many programming elements … not a storytelling" — and the hard ``[:1400]`` slice at the comment
    sites cut them MID-SENTENCE. This is the comment-side sibling of ``brief_for_phone``:

    · markdown noise a Jira comment renders badly is dropped (``#`` headings, ``|table|`` rows,
      code fences) — the structured hand-off lines (WHY/BLOCKER/DECISION/OPTIONS/numbered options)
      are kept verbatim, they are the contract the cockpit parses;
    · prose folds to '• ' bullets via ``bulletize`` (complete thoughts, never a mid-word cut);
    · the cap trims at a bullet boundary, not at byte N.
    """
    try:
        text = (text or "").strip()
        if not text:
            return ""
        keep_prefixes = ("WHY PM CANNOT RESOLVE:", "BLOCKER:", "DECISION:", "OPTIONS:",
                         "MANUAL TEST", "TEST:", "1.", "2.", "3.")
        kept: list[str] = []
        prose: list[str] = []
        in_fence = False
        for ln in text.splitlines():
            t = ln.strip()
            if t.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence or not t:
                continue
            if t.startswith("|") or set(t) <= {"-", "|", ":", " "}:
                continue                       # table rows / rules — unreadable in a Jira comment
            t = t.lstrip("#").strip()          # headings become plain lines
            if any(t.startswith(k) for k in keep_prefixes):
                kept.append(t)
            else:
                prose.append(t)
        folded = bulletize("\n".join(prose), max_bullets=6,
                           max_chars=max(200, max_chars - sum(len(k) + 1 for k in kept)))
        out_lines = kept + ([folded] if folded else [])
        out = "\n".join(out_lines).strip()
        if len(out) <= max_chars:
            return out or text[:max_chars]
        # trim at a line boundary, never mid-sentence
        acc: list[str] = []
        used = 0
        for ln in out.splitlines():
            if used + len(ln) + 1 > max_chars:
                break
            acc.append(ln)
            used += len(ln) + 1
        return "\n".join(acc) or out[:max_chars]
    except Exception:  # noqa: BLE001 — a formatting helper must never lose a comment
        return (text or "")[:max_chars]


def send(text: str, chat_id: str | int | None = None, *, silent: bool = False) -> bool:
    """Send a Telegram message. Returns True if sent, False if not configured or
    failed. Never raises — notifications must not break the pipeline.

    By default the message goes to the Commander's ops chat (TELEGRAM_CHAT_ID), so every
    existing caller — ops reports, escalations, council summaries — stays pinned to the ops
    chat. ``chat_id`` may target a specific chat explicitly (needs only the bot token, not
    TELEGRAM_CHAT_ID); since the EU-65 liaison channel's deletion (Phase-2 §2) no production
    path supplies one.

    EU-358: messages over Telegram's 4096-char ceiling are chunked (they were rejected with a 400
    and silently lost — exactly the long escalation reports that most need to arrive), and a 429
    gets ONE bounded retry honouring retry_after (capped so a rate-limit can't stall the pipeline).

    2026-07-22 (Commander: "must be brief"): chunking keeps a long message from being LOST, but a
    report that arrives as three phone notifications is still unreadable on a phone. Every message
    is now folded to a skimmable length HERE, at the one seam they all pass through, rather than
    hunting each verbose call site (and re-hunting every new one). Deliberately the DETERMINISTIC
    bulletizer, never the model: this runs on the notification path of every officer event, so it
    must add no latency, no cost and no failure mode of its own. Short messages — the overwhelming
    majority — are untouched.

    Args:
        text: Message body (will be briefly-folded for phone-readability).
        chat_id: Optional override target; otherwise uses TELEGRAM_CHAT_ID.
        silent: When True, sends with disable_notification=True so it does NOT
            produce a toast/ping on the recipient's device (for probes/smoke tests).
            Defaults to False for normal alert traffic.
    """
    text = brief_for_phone(text)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    target = str(chat_id) if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and target):
        return False
    ok = True
    error_reason = ""
    for chunk in _tg_chunks(text):
        try:
            payload = {"chat_id": target, "text": chunk, "disable_web_page_preview": True}
            if silent:
                payload["disable_notification"] = True
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload,
                timeout=10,
            )
            if r.status_code == 429:
                try:
                    wait = float((r.json().get("parameters") or {}).get("retry_after", 1))
                except Exception:  # noqa: BLE001
                    wait = 1.0
                time.sleep(min(max(wait, 0.0), 10.0))
                r = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json=payload,
                    timeout=10,
                )
            if r.status_code != 200:
                ok = False
                error_reason = f"http {r.status_code}"
                try:
                    err = r.json()
                    error_reason += f" — {err.get('description', '')}".rstrip(" — ")
                except Exception:  # noqa: BLE001
                    pass
        except requests.RequestException as exc:
            ok = False
            error_reason = str(exc)[:200]
    if ok:
        _set_alerting("ok")
    else:
        _set_alerting("failed", error_reason or "Telegram returned non-200")
        _note_send_failure()
    return ok


# 2026-07-19 stabilization: send() never raises and 112 of 113 call sites discard its boolean, so
# a revoked bot token / wrong chat id / outage silently blinded the Commander's primary alert
# channel. One throttled console line per window makes a dead Telegram visible in the run log and
# the cockpit feed without spamming either.
_SEND_FAILURE_LOG_INTERVAL_S = 600.0
_last_send_failure_log = 0.0


def _note_send_failure() -> None:
    global _last_send_failure_log
    now = time.time()
    if now - _last_send_failure_log >= _SEND_FAILURE_LOG_INTERVAL_S:
        _last_send_failure_log = now
        print("  ⚠ Telegram send FAILED (bad token / chat id / network?) — the Commander is not "
              "receiving alerts. Check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID with ./general ping.",
              flush=True)


def get_updates(offset: int | None = None, timeout: int = 0) -> list:
    """Poll Telegram for incoming messages (long-poll if timeout>0). Returns the
    raw update list, or [] if not configured / on error."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return []
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params=params, timeout=timeout + 15)
        return r.json().get("result", []) or [] if r.status_code == 200 else []
    except requests.RequestException:
        return []


# EU-118: plan-limit alert sent flag (one-shot per session to avoid spam)
_plan_limit_alert_sent = False
# EU-213: identity of the episode the flag above was set for — the signature of the specific cap
# (which limits are over + when they reset). Paired with _plan_limit_alert_sent so suppression is
# scoped to ONE episode: a genuinely new cap must alert even if a stale "sent" flag lingers.
_plan_limit_alert_episode: str | None = None

# EU-213: the in-memory flag above resets to False on every process restart, so a fresh autopilot
# process could re-fire the SAME alert the previous process already sent (dedup was per-session
# only). Persist the flag to this small JSON sidecar too; a module-level constant so tests can
# point it at a tempdir. Lives beside the other small state sidecars under state/ (gitignored).
# The file also records the episode signature, so a stale on-disk flag from a PRIOR (resolved)
# episode cannot suppress the alert for a genuinely NEW cap — only a matching episode suppresses.
_PLAN_LIMIT_DEDUP_FILE = Path(__file__).resolve().parent.parent / "state" / "sonnet_alert_dedup.json"


def _episode_signature(over_limits: list, reset_times: list[str]) -> str:
    """Stable identity of a cap episode (EU-213): the sorted set of over-limit keys/labels plus the
    sorted reset times. A genuinely new episode — different limits hit, or a later reset time —
    yields a different signature, so a stale on-disk 'already sent' flag from a prior (resolved)
    episode can never suppress the alert for a NEW one. Order-independent so the same cap produces
    the same signature regardless of how the caller ordered the lists."""
    keys = sorted(
        str((limit or {}).get("key") or (limit or {}).get("label") or "")
        for limit in (over_limits or [])
    )
    resets = sorted(str(r) for r in (reset_times or []))
    return "||".join(keys) + "::" + "||".join(resets)


def _plan_limit_dedup_read() -> tuple[bool, str | None]:
    """Best-effort read of the on-disk dedup: ``(sent, episode_signature)``. Missing/corrupt file
    reads as ``(False, None)`` — losing the dedup on a bad read is far safer than silently
    suppressing a real alert."""
    try:
        data = json.loads(_PLAN_LIMIT_DEDUP_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return bool(data.get("plan_limit_alert_sent")), data.get("episode")
    except (OSError, ValueError):
        pass
    return False, None


def _plan_limit_dedup_loaded() -> bool:
    """Back-compat shim: True when the on-disk flag says an alert was already sent (ignores
    episode identity). Prefer ``_plan_limit_dedup_read`` for episode-scoped decisions."""
    return _plan_limit_dedup_read()[0]


def _plan_limit_dedup_write(sent: bool, episode: str | None = None) -> None:
    """Best-effort persist of the dedup flag + its episode signature — never raises
    (instrumentation must never break a notification). Clearing (``sent=False``) also drops the
    episode so a later cap starts from a clean slate.

    EU-211: merges into the existing document rather than clobbering it — the file also holds
    the dual-watermark dedup keys (``_dual_watermark_dedup_write``), and each writer must
    preserve the other's keys so both dedup domains coexist in one sidecar."""
    def _merge(current):
        doc = dict(current) if isinstance(current, dict) else {}
        doc["plan_limit_alert_sent"] = sent
        doc["episode"] = episode if sent else None
        return doc

    try:
        locking.locked_rmw(_PLAN_LIMIT_DEDUP_FILE, _merge,
                           default={}, corrupt_to_default=True)
    except OSError:
        pass


def plan_limit_alert(over_limits: list, reset_times: list[str]) -> bool:
    """Send a SEVERE Telegram alert when a Claude plan limit is hit.

    Returns True if sent, False if not configured or failed. Only sends ONCE per limit episode —
    the sent flag (and the episode's signature) is persisted to disk (EU-213) as well as in-memory,
    so a process restart while the SAME limit is still active does NOT re-fire the alert. Crucially,
    suppression is scoped to the episode: a stale on-disk flag left over from a PRIOR (resolved)
    cap does not eat the alert for a genuinely NEW cap — a different episode always alerts.
    ``reset_plan_limit_alert()`` clears both so a future limit hit can alert again.

    Args:
        over_limits: List of limit dicts that are hit (from ``usage.plan_limit_hit``)
        reset_times: List of human-readable reset times like "Mon Jun 30 14:30 UTC"
    """
    global _plan_limit_alert_sent, _plan_limit_alert_episode

    sig = _episode_signature(over_limits, reset_times)

    # Which episode (if any) have we already alerted for? In-memory first, else the on-disk record
    # left by a prior (now restarted) process. Only that exact episode is suppressed.
    sent_episode: str | None = None
    if _plan_limit_alert_sent:
        sent_episode = _plan_limit_alert_episode
    else:
        disk_sent, disk_episode = _plan_limit_dedup_read()
        if disk_sent:
            sent_episode = disk_episode
            # Adopt the on-disk state in memory ONLY when it matches the current episode; a stale
            # flag for a different episode is left untouched so this new cap alerts and overwrites it.
            if disk_episode == sig:
                _plan_limit_alert_sent = True
                _plan_limit_alert_episode = disk_episode

    if sent_episode is not None and sent_episode == sig:
        return False

    if not configured():
        return False

    limit_names = []
    for limit in over_limits:
        label = limit.get("label") or limit.get("key") or "unknown"
        limit_names.append(str(label))

    resets_text = ", ".join(reset_times) if reset_times else "unknown time"

    message = (
        f"⛔ <b>CLAUDE PLAN LIMIT REACHED</b> ⛔\n\n"
        f"Autopilot has paused because the following Claude plan limit(s) are exhausted:\n"
        f"• {', '.join(limit_names)}\n\n"
        f"<b>Resets at:</b> {resets_text}\n\n"
        f"New builds are held until the limit renews. "
        f"This prevents API errors and silent churn."
    )

    sent = send(message)
    if sent:
        _plan_limit_alert_sent = True
        _plan_limit_alert_episode = sig
        _plan_limit_dedup_write(True, sig)

    return sent


def reset_plan_limit_alert() -> None:
    """Clear the plan-limit alert sent flag — e.g. after the limit resets.

    Allows a new alert to be sent if the limit is hit again in a future billing period. Clears
    the in-memory flag, its episode signature, and the on-disk dedup (EU-213) so a restarted
    process doesn't adopt stale "already sent" state.
    """
    global _plan_limit_alert_sent, _plan_limit_alert_episode
    _plan_limit_alert_sent = False
    _plan_limit_alert_episode = None
    _plan_limit_dedup_write(False)


# EU-122: dual-provider low-watermark alert sent flag (one-shot per provider per session)
_dual_low_watermark_alerted: set[str] = set()


def _dual_watermark_dedup_read() -> set[str]:
    """Best-effort read of the on-disk dual-watermark dedup set (EU-211). Missing/corrupt file
    reads as an empty set — losing the dedup on a bad read is far safer than silently
    suppressing a real alert (fail-open, matching ``_plan_limit_dedup_read``)."""
    try:
        data = json.loads(_PLAN_LIMIT_DEDUP_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            providers = data.get("dual_low_watermark_alerted") or []
            if isinstance(providers, list):
                return {str(p) for p in providers}
    except (OSError, ValueError):
        pass
    return set()


def _dual_watermark_dedup_write(providers: set[str]) -> None:
    """Best-effort persist of the dual-watermark dedup set — never raises (instrumentation must
    never break a notification). Merges into the shared ``sonnet_alert_dedup.json`` document so
    the co-located plan-limit keys (``_plan_limit_dedup_write``) are preserved (EU-211)."""
    def _merge(current):
        doc = dict(current) if isinstance(current, dict) else {}
        doc["dual_low_watermark_alerted"] = sorted(providers)
        return doc

    try:
        locking.locked_rmw(_PLAN_LIMIT_DEDUP_FILE, _merge,
                           default={}, corrupt_to_default=True)
    except OSError:
        pass


def dual_low_watermark_alert(provider: str, usage_data: dict) -> bool:
    """Send a Telegram alert when a provider crosses the budget bad threshold (low-watermark).

    Args:
        provider: "claude" or "glm"
        usage_data: Provider status dict from usage.dual_provider_budget_status()

    Returns True if sent, False if not configured or failed. Only sends ONCE per provider
    per session (resets on process restart) to avoid spamming.

    EU-211: the in-memory set is unioned with the on-disk dedup set before the check, so a
    restarted process picks up prior sends from disk and does not re-fire the same alert.
    """
    global _dual_low_watermark_alerted

    # EU-211: adopt any prior sends recorded on disk by a previous (restarted) process.
    _dual_low_watermark_alerted |= _dual_watermark_dedup_read()

    # Only alert once per provider per session
    if provider in _dual_low_watermark_alerted:
        return False

    if not configured():
        return False

    if provider == "claude":
        claude_limits = usage_data.get("limits", [])
        if not claude_limits:
            return False

        limit_names = []
        for limit in claude_limits:
            label = limit.get("label") or limit.get("key") or "unknown"
            util = float(limit.get("utilization", 0.0))
            limit_names.append(f"{label} ({util:.1%})")

        message = (
            f"⚠️ <b>CLAUDE PLAN LOW-WATERMARK</b> ⚠️\n\n"
            f"Claude plan limit(s) approaching exhaustion:\n"
            f"• {', '.join(limit_names)}\n\n"
            f"Autopilot will finish the current ticket then stop/switch. "
            f"This prevents mid-build cutoff and API errors."
        )
    elif provider == "glm":
        used = usage_data.get("used", 0)
        cap = usage_data.get("cap", 0)
        pct = usage_data.get("pct", 0.0)

        message = (
            f"⚠️ <b>GLM QUOTA LOW-WATERMARK</b> ⚠️\n\n"
            f"GLM (Z.ai) quota is {pct:.1%} used ({used:,}/{cap:,} tokens).\n\n"
            f"Autopilot will finish the current ticket then stop/switch. "
            f"This prevents mid-build cutoff."
        )
    else:
        return False

    sent = send(message)
    if sent:
        _dual_low_watermark_alerted.add(provider)
        _dual_watermark_dedup_write(_dual_low_watermark_alerted)

    return sent


def reset_dual_low_watermark_alert(provider: str | None = None) -> None:
    """Clear the dual-provider low-watermark alert sent flag.

    Args:
        provider: If "claude" or "glm", clears only that provider's flag.
                  If None, clears all providers (e.g. at midnight or after quota reset).

    EU-211: clears the on-disk dedup set too (per-provider drop, or a full wipe when
    ``provider`` is None), so a restarted process doesn't resurrect a cleared flag from disk.

    EU-390: the on-disk set is unioned into memory BEFORE the discard/clear (mirroring
    ``dual_low_watermark_alert``) — a restarted process has an empty in-memory set, and
    persisting that verbatim on a per-provider reset would wipe the OTHER provider's
    dedup entry from disk as collateral, letting its alert re-fire.
    """
    global _dual_low_watermark_alerted
    _dual_low_watermark_alerted |= _dual_watermark_dedup_read()
    if provider is None:
        _dual_low_watermark_alerted.clear()
    else:
        _dual_low_watermark_alerted.discard(provider)
    _dual_watermark_dedup_write(_dual_low_watermark_alerted)


def incoming_texts(updates: list, cfg=None) -> list[tuple[int, str, str]]:
    """Extract ``(update_id, text, origin)`` for text messages we accept.

    Only the Commander's operations chat (TELEGRAM_CHAT_ID) is accepted; any other chat id is
    ignored. ``origin`` is always ``"ops"`` — the 3-tuple shape (and the ``cfg`` parameter) are
    kept for caller compatibility from the EU-65 liaison era; that outward channel was DELETED
    in Phase-2 §2 (2026-07-06), so no external chat can ever reach the command router."""
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    out: list[tuple[int, str, str]] = []
    for u in updates:
        msg = u.get("message") or u.get("edited_message") or {}
        text = msg.get("text")
        if not text:
            continue
        cid = str((msg.get("chat") or {}).get("id", ""))
        if not chat or not cid or cid == str(chat):   # ops chat (unchanged acceptance)
            out.append((u.get("update_id"), text, "ops"))
        # else: any other chat -> ignored (the EU-65 external channel no longer exists)
    return out
