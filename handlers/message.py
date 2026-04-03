"""
handlers/message.py — Free-text expense message processing.

process_expense()     — pure sync logic, used by main.py test runner
tg_handle_message()   — async Telegram handler, calls process_expense internally
"""

import logging
import re
import json
import time
import sys

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from parsing.parser import parse, ParseResult
from sheets import log_expense
from handlers.commands import append_to_history
from handlers.subscribers import track_subscriber

logger = logging.getLogger(__name__)

# #region agent debug helpers
_DBG_LOG = "/Users/omer/Desktop/Expences_Project/Parents/ExpenseBotParents-1/.cursor/debug-b7a257.log"

def _mem_mb() -> float:
    try:
        with open('/proc/self/status') as f:
            for line in f:
                if line.startswith('VmRSS'):
                    return round(int(line.split()[1]) / 1024, 1)
    except Exception:
        pass
    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(rss / (1024 * 1024 if sys.platform == 'darwin' else 1024), 1)
    except Exception:
        return -1

def _dbg(location: str, msg: str, hyp: str, **data):
    try:
        payload = {"sessionId": "b7a257", "timestamp": int(time.time() * 1000),
                   "location": location, "message": msg, "hypothesisId": hyp,
                   "data": {"mem_mb": _mem_mb(), **data}}
        with open(_DBG_LOG, 'a') as f:
            f.write(json.dumps(payload) + '\n')
        logger.info(f"[DBG:{hyp}] {msg} mem={_mem_mb()}MB {data}")
    except Exception:
        pass
# #endregion


def process_expense(text: str) -> tuple[str, ParseResult]:
    """
    Parse and (if matched) log a free-text expense message.

    Returns:
        (reply, result) where reply is the string to send the user
        and result is the ParseResult (useful for callers that want more detail).
    """
    result = parse(text)

    if result.status == "matched" or result.status == "reversed":
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
# Telegram handler
# ---------------------------------------------------------------------------

async def tg_handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Main Telegram entry point for free-text messages.

    Checks for a pending conversation state first (ask_amount flow).
    Otherwise parses the message normally and handles each status:
      matched / reversed  → log and confirm
      ask_amount          → ask for the amount, store state
      fuzzy_confirm       → send inline Yes / No buttons, store state
      no_match            → send error hint
    """
    track_subscriber(update.effective_chat.id)
    text = update.message.text.strip()

    # #region agent debug - H-A/H-C: memory at handler entry
    _mem_before = _mem_mb()
    _dbg("message.py:tg_handle_message", "handler entry",
         hyp="H-A",
         user_data_keys=list(context.user_data.keys()),
         all_users_count=len(context.application.user_data),
         text_preview=text[:30])
    # #endregion

    pending = context.user_data.get("pending")

    # ------------------------------------------------------------------
    # Resume a pending ask_amount conversation
    # ------------------------------------------------------------------
    if pending and pending["type"] == "ask_amount":
        m = re.search(r"-?\d+(?:\.\d+)?", text)
        if not m:
            await update.message.reply_text(
                "I need a number. How much was it?", parse_mode="HTML"
            )
            return

        amount   = float(m.group())
        category = pending["category"]
        original = pending["original_text"]
        context.user_data.pop("pending", None)

        log_result = log_expense(
            category=category,
            amount=amount,
            original_text=original,
        )
        if log_result.success:
            append_to_history(
                category=log_result.category,
                amount=log_result.amount_added,
                tab_name=log_result.tab_name,
                row=log_result.row,
                timestamp=log_result.timestamp,
                original_text=original,
            )
            await update.message.reply_text(
                f"<b>{log_result.message}</b>", parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                f"❌ Sheet error: {log_result.message}", parse_mode="HTML"
            )
        return

    # ------------------------------------------------------------------
    # Normal message — parse and act (single call to process_expense)
    # ------------------------------------------------------------------
    reply, result = process_expense(text)

    if result.status in ("matched", "reversed"):
        await update.message.reply_text(f"<b>{reply}</b>", parse_mode="HTML")

    elif result.status == "ask_amount":
        context.user_data["pending"] = {
            "type": "ask_amount",
            "category": result.category,
            "original_text": text,
        }
        await update.message.reply_text(
            f"I recognise <b>{result.category}</b> but there's no amount.\n"
            f"How much was it?",
            parse_mode="HTML",
        )

    elif result.status == "fuzzy_confirm":
        context.user_data["pending"] = {
            "type": "fuzzy_confirm",
            "suggestion": result.suggestion,
            "amount": result.amount,
            "original_text": text,
        }
        if result.amount is not None:
            msg = f"Did you mean <b>{result.suggestion}</b>? (₪{result.amount:g})"
        else:
            msg = f"Did you mean <b>{result.suggestion}</b>?"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Yes, {result.suggestion}", callback_data="fuzzy_yes"),
            InlineKeyboardButton("❌ No",                        callback_data="fuzzy_no"),
        ]])
        await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="HTML")

    else:  # no_match
        await update.message.reply_text(
            "❌ I couldn't match that to any category.\n"
            "Use /categories to browse, or /keywords &lt;name&gt; to check keywords.",
            parse_mode="HTML",
        )

    # #region agent debug - H-C: did memory drop after full handler completes?
    import gc as _gc
    _gc.collect()
    _dbg("message.py:tg_handle_message", "handler exit (after gc)",
         hyp="H-C",
         mem_before_mb=_mem_before,
         delta_mb=round(_mem_mb() - _mem_before, 1))
    # #endregion
