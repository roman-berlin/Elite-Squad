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

import requests


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def clip(text: str, limit: int = 700, more: str = "…  (full report in the cockpit)") -> str:
    """Trim a long officer report to a phone-skimmable size.

    Telegram is the skimmable lens; the full text always stays in the cockpit (saved transcripts /
    last-*.md). Cuts at the last paragraph / line / sentence boundary inside the window so a message
    never ends mid-word or mid-thought, then appends a short 'more in the cockpit' footer."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    best = max(cut.rfind("\n"), cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if best < limit // 2:          # no decent boundary in the back half -> fall back to last space
        best = cut.rfind(" ")
    if best > 0:
        cut = cut[:best + 1]
    return cut.rstrip(" \n.,;") + "\n" + more


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
