"""
handlers/message.py — Free-text expense message processing.

process_expense()     — pure sync logic, used by main.py test runner
tg_handle_message()   — async Telegram handler

Flow
----
1.  Run the rule-based parser first (free, instant).
2.  If the parser is confident (matched / reversed) → log immediately.
3.  Otherwise → hand off to the AI handler (ask_ai), which either:
      a. calls log_expense via tool-use  →  log the expense
      b. returns a short text reply       →  send it as-is

Conversation history
--------------------
Per-user history is stored in context.user_data["ai_history"] as a plain list
of {"role": "user"|"assistant", "content": str} dicts (OpenAI message format).
The AI handler caps how far back it looks, so this list can grow indefinitely
without ballooning prompt costs.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from parsing.parser import parse, ParseResult
from sheets import log_expense
from handlers.commands import append_to_history
from handlers.subscribers import track_subscriber
from handlers.ai_handler import ask_ai

logger = logging.getLogger(__name__)


def process_expense(text: str) -> tuple[str, ParseResult]:
    """
    Parse and (if matched) log a free-text expense message.
    Used by main.py CLI test runner — no AI involved here.

    Returns:
        (reply, result) where reply is the string to send the user
        and result is the ParseResult.
    """
    result = parse(text)

    if result.status in ("matched", "reversed"):
        log_result = log_expense(
            category=result.category,
            amount=result.amount,
            original_text=result.original_text,
        )
        if log_result.success:
            append_to_history(
                category=log_result.category,
                amount=log_result.amount_added,
                tab_name=log_result.tab_name,
                row=log_result.row,
                timestamp=log_result.timestamp,
                original_text=result.original_text,
            )
            return log_result.message, result
        else:
            return f"❌ Sheet error: {log_result.message}", result

    elif result.status == "ask_amount":
        return (
            f"I recognise '{result.category}' but there's no amount in your message.\n"
            f"How much was it? (e.g. '{result.category.lower()} 120')"
        ), result

    elif result.status == "fuzzy_confirm":
        if result.amount is not None:
            return (
                f"Did you mean '{result.suggestion}'? (₪{result.amount:g})\n"
                f"If so, resend as: {result.suggestion.lower()} {result.amount:g}"
            ), result
        else:
            return (
                f"Did you mean '{result.suggestion}'?\n"
                f"If so, resend as: {result.suggestion.lower()} <amount>"
            ), result

    else:  # no_match
        return (
            "I couldn't match that to any category.\n"
            "Use /categories to see what's available, "
            "or /keywords <name> to see what triggers a category."
        ), result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _add_to_ai_history(context: ContextTypes.DEFAULT_TYPE, role: str, content: str) -> None:
    """Append a message to the per-user AI conversation history."""
    history: list = context.user_data.setdefault("ai_history", [])
    history.append({"role": role, "content": content})


def _get_ai_history(context: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    return context.user_data.get("ai_history", [])


# ---------------------------------------------------------------------------
# Telegram handler
# ---------------------------------------------------------------------------

async def tg_handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Main Telegram entry point for all free-text messages.

    Fast path  — parser returns matched / reversed:
        Log immediately, no AI call, instant response.

    AI path — everything else (no_match, ask_amount, fuzzy_confirm):
        Pass message + per-user conversation history to ask_ai().
        If AI picks a category + amount → log it.
        If AI replies with text → send it (e.g. asking for the amount).
    """
    track_subscriber(update.effective_chat.id)
    text = update.message.text.strip()

    result = parse(text)

    # ------------------------------------------------------------------
    # Fast path — rule-based parser is confident
    # ------------------------------------------------------------------
    if result.status in ("matched", "reversed"):
        log_result = log_expense(
            category=result.category,
            amount=result.amount,
            original_text=result.original_text,
        )
        if log_result.success:
            append_to_history(
                category=log_result.category,
                amount=log_result.amount_added,
                tab_name=log_result.tab_name,
                row=log_result.row,
                timestamp=log_result.timestamp,
                original_text=result.original_text,
            )
            # Keep AI history in sync so follow-up messages have context
            _add_to_ai_history(context, "user", text)
            _add_to_ai_history(context, "assistant", log_result.message)
            await update.message.reply_text(f"<b>{log_result.message}</b>", parse_mode="HTML")
        else:
            await update.message.reply_text(
                f"❌ Sheet error: {log_result.message}", parse_mode="HTML"
            )
        return

    # ------------------------------------------------------------------
    # AI path — parser is uncertain or has no match
    # ------------------------------------------------------------------
    history = _get_ai_history(context)
    ai_result = await ask_ai(text, history)

    if ai_result["action"] == "log":
        category = ai_result["category"]
        amount   = ai_result["amount"]

        log_result = log_expense(
            category=category,
            amount=amount,
            original_text=text,
        )
        if log_result.success:
            append_to_history(
                category=log_result.category,
                amount=log_result.amount_added,
                tab_name=log_result.tab_name,
                row=log_result.row,
                timestamp=log_result.timestamp,
                original_text=text,
            )
            _add_to_ai_history(context, "user", text)
            _add_to_ai_history(context, "assistant", log_result.message)
            await update.message.reply_text(f"<b>{log_result.message}</b>", parse_mode="HTML")
        else:
            await update.message.reply_text(
                f"❌ Sheet error: {log_result.message}", parse_mode="HTML"
            )

    else:  # action == "reply"
        reply_text = ai_result["text"]
        _add_to_ai_history(context, "user", text)
        _add_to_ai_history(context, "assistant", reply_text)
        await update.message.reply_text(reply_text, parse_mode="HTML")
