"""
handlers/callbacks.py — Inline keyboard button callbacks.

Currently handles the fuzzy-confirm flow:
  fuzzy_yes  → user confirmed the suggested category, log the expense
  fuzzy_no   → user rejected, tell them to retype

The pending state (suggestion, amount, original_text) is stored in
context.user_data["pending"] by tg_handle_message before the buttons are sent.
"""

import logging
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from sheets import log_expense
from handlers.commands import (
    append_to_history,
    summary as get_summary,
    section_detail as get_section_detail,
    delete as do_delete,
    BROAD_CATEGORIES,
)
from handlers.ai_handler import explain_sheet_missing

logger = logging.getLogger(__name__)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()  # acknowledge the tap immediately (removes loading indicator)

    data = query.data
    pending = context.user_data.get("pending")

    # ------------------------------------------------------------------
    # fuzzy_yes — log the confirmed expense
    # ------------------------------------------------------------------
    if data == "fuzzy_yes":
        if not pending or pending.get("type") != "fuzzy_confirm":
            await query.edit_message_text("This confirmation has expired. Please send your expense again.")
            return

        category    = pending["suggestion"]
        amount      = pending["amount"]
        original    = pending["original_text"]
        context.user_data.pop("pending", None)

        if amount is None:
            # Category confirmed but still no amount — ask for it
            context.user_data["pending"] = {
                "type": "ask_amount",
                "category": category,
                "original_text": original,
            }
            await query.edit_message_text(
                f"Got it — <b>{category}</b>.\nHow much was it? Just reply with the amount.",
                parse_mode="HTML",
            )
            return

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
            await query.edit_message_text(
                f"<b>{log_result.message}</b>", parse_mode="HTML"
            )
        elif log_result.failure is not None:
            explanation = await explain_sheet_missing(original, log_result.failure)
            await query.edit_message_text(explanation, parse_mode="HTML")
        else:
            await query.edit_message_text(
                f"❌ {log_result.message}", parse_mode="HTML"
            )

    # ------------------------------------------------------------------
    # fuzzy_no — user rejected the suggestion
    # ------------------------------------------------------------------
    elif data == "fuzzy_no":
        context.user_data.pop("pending", None)
        await query.edit_message_text(
            "OK, I won't log that.\n"
            "Use /categories to browse, or /keywords &lt;name&gt; to check keywords.",
            parse_mode="HTML",
        )

    # ------------------------------------------------------------------
    # summary|YYYY|M — navigate to a different month's summary
    # ------------------------------------------------------------------
    elif data.startswith("summary|"):
        _, year_str, month_str = data.split("|")
        dt = datetime(int(year_str), int(month_str), 1)
        text, keyboard = get_summary(dt)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)

    # ------------------------------------------------------------------
    # section|<name>|YYYY|M — drill down into one broad section
    # ------------------------------------------------------------------
    elif data.startswith("section|"):
        _, section_name, year_str, month_str = data.split("|", 3)
        dt = datetime(int(year_str), int(month_str), 1)
        text, keyboard = get_section_detail(section_name, dt)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)

    # ------------------------------------------------------------------
    # help_categories — show all categories (tapped from /help keyboard)
    # ------------------------------------------------------------------
    elif data == "help_categories":
        lines = []
        for section, cats in BROAD_CATEGORIES.items():
            lines.append(f"\n<b>{section}</b>")
            for cat in cats:
                lines.append(f"  • {cat}")
        await query.edit_message_text("\n".join(lines).strip(), parse_mode="HTML")

    # ------------------------------------------------------------------
    # help_delete — undo the most recent expense (tapped from /help keyboard)
    # ------------------------------------------------------------------
    elif data == "help_delete":
        result = do_delete(1)
        await query.edit_message_text(result, parse_mode="HTML")

    else:
        logger.warning(f"Unknown callback data: {data!r}")
        await query.edit_message_text("Unknown action.")
