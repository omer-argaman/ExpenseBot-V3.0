"""
handlers/callbacks.py — Inline keyboard button callbacks.

Handles:
  fuzzy_yes / fuzzy_no  — fuzzy-suggestion confirm flow (existing)
  summary|YYYY|M        — month nav from /summary
  section|<name>|YYYY|M — section drill-down
  help_categories       — show all categories
  help_delete           — undo last expense
  txn:<pending_id>:cat:<index>  — user picks a category for an SMS-ingested
                                  transaction; logs it and persists the
                                  merchant->category mapping.
  txn:<pending_id>:more         — expand to all categories.
  txn_pick:<pending_id>:<category_b64>  — final pick from the expanded view.

The pending state (suggestion, amount, original_text) for the fuzzy flow is
stored in context.user_data["pending"]. The pending state for SMS asks lives
in the in-memory map in handlers.transaction_handler so it can be written by
the Flask thread and read by the PTB callback thread.
"""

import base64
import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from parsing.category_map import CATEGORY_MAP
from parsing.merchant_map import learn as learn_merchant
from sheets import log_expense
from handlers.commands import (
    append_to_history,
    summary as get_summary,
    section_detail as get_section_detail,
    delete as do_delete,
    BROAD_CATEGORIES,
)
from handlers.ai_handler import explain_sheet_missing
from handlers.transaction_handler import peek_pending, pop_pending

_EXPIRED_MSG = (
    "This category prompt expired (bot restarted or it's older than 24h). "
    "If you still need to log it, check the amount on the message above "
    "or wait for the next SMS."
)

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

    # ------------------------------------------------------------------
    # txn:<pending_id>:cat:<index>   — user picked one of the candidate buttons
    # txn:<pending_id>:more          — expand to all categories
    # txn_pick:<pending_id>:<b64>    — final pick from the expanded keyboard
    # ------------------------------------------------------------------
    elif data.startswith("txn:") or data.startswith("txn_pick:"):
        await _handle_txn_callback(query, data)

    else:
        logger.warning(f"Unknown callback data: {data!r}")
        await query.edit_message_text("Unknown action.")


# ---------------------------------------------------------------------------
# SMS-ingested transaction confirmation flow
# ---------------------------------------------------------------------------

def _expanded_category_keyboard(pending_id: str) -> InlineKeyboardMarkup:
    """Two-column keyboard with every CATEGORY_MAP key."""
    buttons: list[list[InlineKeyboardButton]] = []
    cats = list(CATEGORY_MAP.keys())
    for i in range(0, len(cats), 2):
        row = []
        for cat in cats[i:i + 2]:
            row.append(InlineKeyboardButton(
                cat,
                callback_data=_pick_callback(pending_id, cat),
            ))
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


def _pick_callback(pending_id: str, category: str) -> str:
    # Base64-url encode the category to keep callback_data <= 64 bytes
    # and free of any reserved characters.
    encoded = base64.urlsafe_b64encode(category.encode("utf-8")).decode("ascii")
    return f"txn_pick:{pending_id}:{encoded}"


def _decode_pick(data: str) -> tuple[str | None, str | None]:
    parts = data.split(":", 2)
    if len(parts) != 3:
        return None, None
    _, pending_id, encoded = parts
    try:
        category = base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8")
    except Exception:
        return None, None
    return pending_id, category


async def _handle_txn_callback(query, data: str) -> None:
    if data.startswith("txn_pick:"):
        pending_id, category = _decode_pick(data)
        if not pending_id or not category:
            await query.edit_message_text(_EXPIRED_MSG)
            return
        await _commit_txn(query, pending_id, category)
        return

    parts = data.split(":")
    # Format: txn:<pending_id>:cat:<index>   OR   txn:<pending_id>:more
    if len(parts) < 3:
        await query.edit_message_text("Invalid action.")
        return
    pending_id = parts[1]
    action = parts[2]

    if action == "more":
        if peek_pending(pending_id) is None:
            await query.edit_message_text(_EXPIRED_MSG)
            return
        keyboard = _expanded_category_keyboard(pending_id)
        await query.edit_message_reply_markup(reply_markup=keyboard)
        return

    if action == "cat" and len(parts) >= 4:
        try:
            index = int(parts[3])
        except ValueError:
            await query.edit_message_text("Invalid choice.")
            return
        ask = peek_pending(pending_id)
        if ask is None:
            await query.edit_message_text(_EXPIRED_MSG)
            return
        if index < 0 or index >= len(ask.candidates):
            await query.edit_message_text("Invalid choice.")
            return
        category = ask.candidates[index]
        await _commit_txn(query, pending_id, category)
        return

    await query.edit_message_text("Unknown action.")


async def _commit_txn(query, pending_id: str, category: str) -> None:
    ask = peek_pending(pending_id)
    if ask is None:
        await query.edit_message_text(_EXPIRED_MSG)
        return

    if category not in CATEGORY_MAP:
        await query.edit_message_text(f"Unknown category: {category}")
        return

    log_dt = (
        datetime(ask.txn_date.year, ask.txn_date.month, ask.txn_date.day)
        if ask.txn_date else None
    )
    log_result = log_expense(
        category=category,
        amount=ask.amount_ils,
        original_text=ask.sheet_note,
        dt=log_dt,
    )
    if not log_result.success:
        if log_result.failure is not None:
            explanation = await explain_sheet_missing(ask.sheet_note, log_result.failure)
            await query.edit_message_text(explanation, parse_mode="HTML")
        else:
            await query.edit_message_text(
                f"❌ Could not log: {log_result.message}", parse_mode="HTML"
            )
        return

    pop_pending(pending_id)

    append_to_history(
        category=log_result.category,
        amount=log_result.amount_added,
        tab_name=log_result.tab_name,
        row=log_result.row,
        timestamp=log_result.timestamp,
        original_text=ask.sheet_note,
    )

    if ask.merchant_normalized:
        learn_merchant(ask.merchant_normalized, category, source="user")

    saved_line = ""
    if ask.merchant_normalized:
        saved_line = (
            f"<i>Saved {ask.merchant_normalized!r} = {category}. "
            f"Won't ask again for this merchant.</i>"
        )

    await query.edit_message_text(
        f"<b>Logged: {abs(log_result.amount_added):g} ILS → {category}</b>\n"
        f"{ask.merchant_raw}\n"
        f"{saved_line}",
        parse_mode="HTML",
    )
