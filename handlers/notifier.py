"""
handlers/notifier.py — Send Telegram messages directly via the HTTP Bot API.

Used by the Flask /ingest path (which runs in a worker thread, outside the
python-telegram-bot async event loop). Going through the raw Bot HTTP API
avoids cross-thread asyncio plumbing entirely.

For inline buttons we send `reply_markup` as JSON; the corresponding taps
are still received and handled by the existing CallbackQueryHandler in
bot.py — we don't need PTB to send them, only to receive callbacks.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import requests

from config import OWNER_CHAT_ID, SUBSCRIBERS_FILE, TELEGRAM_BOT_TOKEN

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org"
_TIMEOUT = 10


def _resolve_chat_id() -> Optional[int]:
    """OWNER_CHAT_ID env var wins; otherwise fall back to first subscriber."""
    if OWNER_CHAT_ID:
        try:
            return int(OWNER_CHAT_ID)
        except (TypeError, ValueError):
            logger.warning("OWNER_CHAT_ID is not an int: %r", OWNER_CHAT_ID)

    try:
        with open(SUBSCRIBERS_FILE) as f:
            subs = json.load(f)
        if isinstance(subs, list) and subs:
            return int(subs[0])
    except Exception:
        pass
    return None


def send_message(
    text: str,
    chat_id: Optional[int] = None,
    inline_keyboard: list[list[dict]] | None = None,
    parse_mode: str = "HTML",
) -> Optional[int]:
    """
    Send a Telegram message via the HTTP Bot API.

    Returns the message_id on success, None on failure.
    `inline_keyboard` is a list of rows, each row a list of button dicts
    with `text` and `callback_data` keys.
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set; cannot send Telegram message.")
        return None

    if chat_id is None:
        chat_id = _resolve_chat_id()
    if chat_id is None:
        logger.error(
            "No chat_id to send to. Set OWNER_CHAT_ID env var or message the bot once "
            "to register a subscriber."
        )
        return None

    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if inline_keyboard:
        payload["reply_markup"] = json.dumps({"inline_keyboard": inline_keyboard})

    url = f"{_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            logger.error("Telegram sendMessage not OK: %s", data)
            return None
        return data["result"]["message_id"]
    except Exception as exc:
        logger.error("Telegram sendMessage failed: %s", exc)
        return None


def edit_message_text(
    message_id: int,
    text: str,
    chat_id: Optional[int] = None,
    parse_mode: str = "HTML",
) -> bool:
    """Edit a previously-sent message. Used to clear inline buttons after a tap."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    if chat_id is None:
        chat_id = _resolve_chat_id()
    if chat_id is None:
        return False

    url = f"{_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("ok", False)
    except Exception as exc:
        logger.warning("Telegram editMessageText failed: %s", exc)
        return False
