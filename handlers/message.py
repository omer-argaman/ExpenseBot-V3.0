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
The list is hard-capped at AI_HISTORY_MAX_STORED entries so it cannot grow
without bound, and each individual entry is truncated at MAX_CONTENT_CHARS to
prevent one verbose turn from ballooning memory.
"""

import asyncio
import logging
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from parsing.parser import parse, ParseResult
from sheets import log_expense
from handlers.commands import append_to_history, delete as delete_expenses, summary as get_summary
from handlers.subscribers import track_subscriber
from handlers.ai_handler import ask_ai, explain_sheet_missing

logger = logging.getLogger(__name__)

# Keep at most this many entries in ai_history (each "user" + each "assistant"
# counts as one). 24 = 12 full exchanges — larger than AI_HISTORY_LIMIT (12) so
# trimming is cheap and there's always enough context for the AI to read.
AI_HISTORY_MAX_STORED = 24

# Truncate any single history entry to this many characters. Long tool outputs
# (e.g. summary text) occasionally leak into assistant messages; cap them so
# one entry cannot balloon memory on its own.
MAX_CONTENT_CHARS = 1000


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
    """
    Append a message to the per-user AI conversation history.

    Bounded in two ways to prevent the memory leak that previously pushed the
    bot past Render's 512 MB limit:
      1. Each entry's content is truncated at MAX_CONTENT_CHARS.
      2. The total list length is capped at AI_HISTORY_MAX_STORED — oldest
         entries are dropped when the cap is exceeded.
    """
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS] + "..."

    history: list = context.user_data.setdefault("ai_history", [])
    history.append({"role": role, "content": content})

    overflow = len(history) - AI_HISTORY_MAX_STORED
    if overflow > 0:
        del history[:overflow]


def _get_ai_history(context: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    return context.user_data.get("ai_history", [])


async def _handle_log_failure(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    log_result,
    user_text: str,
) -> None:
    """
    Send a helpful error when log_expense() fails.

    If the failure was a missing month tab, ask the AI to explain exactly
    what's wrong (whitespace, typo, missing sheet, etc.) — see
    ai_handler.explain_sheet_missing. For any other failure, fall back to
    the raw LogResult.message.
    """
    if log_result.failure is not None:
        explanation = await explain_sheet_missing(user_text, log_result.failure)
    else:
        explanation = f"❌ {log_result.message}"
    _add_to_ai_history(context, "assistant", explanation)
    await update.message.reply_text(explanation, parse_mode="HTML")


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
    context.user_data["last_seen"] = datetime.now().timestamp()
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
            _add_to_ai_history(context, "user", text)
            await _handle_log_failure(update, context, log_result, text)
        return

    # ------------------------------------------------------------------
    # AI path — parser is uncertain or has no match
    # ------------------------------------------------------------------
    history = _get_ai_history(context)
    ai_result = await ask_ai(text, history)

    action = ai_result["action"]

    # ------------------------------------------------------------------
    # Single expense log
    # ------------------------------------------------------------------
    if action == "log":
        log_result = log_expense(
            category=ai_result["category"],
            amount=ai_result["amount"],
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
            _add_to_ai_history(context, "user", text)
            await _handle_log_failure(update, context, log_result, text)

    # ------------------------------------------------------------------
    # Multiple expenses in one message
    # ------------------------------------------------------------------
    elif action == "log_multiple":
        lines = ["✅ Logged:"]
        sheet_failure_result = None
        for exp in ai_result["expenses"]:
            log_result = log_expense(
                category=exp["category"],
                amount=exp["amount"],
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
                lines.append(f"  • ₪{exp['amount']:g} → {exp['category']}")
            elif log_result.failure is not None:
                # Sheet is missing — all remaining expenses would fail for
                # the same reason. Short-circuit and explain once.
                sheet_failure_result = log_result
                break
            else:
                lines.append(f"  • ❌ {exp['category']}: {log_result.message}")

        _add_to_ai_history(context, "user", text)

        if sheet_failure_result is not None:
            # Send whatever succeeded first (if anything), then the AI
            # explanation for the sheet problem.
            if len(lines) > 1:
                partial = "\n".join(lines)
                _add_to_ai_history(context, "assistant", partial)
                await update.message.reply_text(f"<b>{partial}</b>", parse_mode="HTML")
            await _handle_log_failure(update, context, sheet_failure_result, text)
        else:
            reply_text = "\n".join(lines)
            _add_to_ai_history(context, "assistant", reply_text)
            await update.message.reply_text(f"<b>{reply_text}</b>", parse_mode="HTML")

    # ------------------------------------------------------------------
    # Delete / undo
    # ------------------------------------------------------------------
    elif action == "delete":
        n = ai_result.get("n", 1)
        reply_text = delete_expenses(n)
        _add_to_ai_history(context, "user", text)
        _add_to_ai_history(context, "assistant", reply_text)
        await update.message.reply_text(reply_text, parse_mode="HTML")

    # ------------------------------------------------------------------
    # Plain text reply (question, clarification, recommendation, etc.)
    # Optionally accompanied by the interactive summary UI.
    # ------------------------------------------------------------------
    else:
        show_summary_info = ai_result.get("show_summary")
        reply_text        = ai_result.get("text", "").strip()

        # Always record the user's message in history once
        _add_to_ai_history(context, "user", text)

        # Display the interactive summary UI if the AI requested it
        if show_summary_info:
            month = show_summary_info["month"]
            year  = show_summary_info["year"]
            dt    = datetime(year, month, 1)
            msg   = await update.message.reply_text("Fetching summary...")
            summary_text, keyboard = await asyncio.to_thread(get_summary, dt)
            await msg.edit_text(summary_text, parse_mode="HTML", reply_markup=keyboard)
            _add_to_ai_history(
                context, "assistant",
                f"[Showed summary for {dt.strftime('%B %Y')}]",
            )

        # Send the AI's text response only if it has something to say
        if reply_text:
            _add_to_ai_history(context, "assistant", reply_text)
            await update.message.reply_text(reply_text, parse_mode="HTML")

        # If neither the summary nor text was produced (shouldn't happen), log it
        if not show_summary_info and not reply_text:
            logger.warning("ask_ai returned empty reply with no show_summary for: %s", text)
