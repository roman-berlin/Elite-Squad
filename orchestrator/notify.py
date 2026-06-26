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

import os
import re

import requests

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
            allowed_tools=[], setting_sources=[], max_turns=1, effort="low"), tag=tag)
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


def send(text: str) -> bool:
    """Send a Telegram message. Returns True if sent, False if not configured or
    failed. Never raises — notifications must not break the pipeline."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
        return r.status_code == 200
    except requests.RequestException:
        return False


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


def incoming_texts(updates: list) -> list[tuple[int, str]]:
    """Extract (update_id, text) for text messages from the configured chat."""
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    out = []
    for u in updates:
        msg = u.get("message") or u.get("edited_message") or {}
        text = msg.get("text")
        cid = str((msg.get("chat") or {}).get("id", ""))
        if not text:
            continue
        if chat and cid and cid != str(chat):
            continue
        out.append((u.get("update_id"), text))
    return out
