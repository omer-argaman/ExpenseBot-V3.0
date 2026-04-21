"""
handlers/commands.py — Logic for all slash commands.

Every function returns a plain string — no Telegram objects, fully testable.
The Telegram handler layer will call these and send the result to the user.

Commands:
    help()                  → list all commands
    categories()            → list all categories by section
    keywords(name)          → show keywords that trigger a category
    summary()               → this month's budget vs actual per broad section
    category(name)          → budget/actual/balance + transaction history for one category
    balance(name)           → quick remaining balance for one category
    delete(n)               → undo the nth most recent logged expense (default: 1)
    show_history()          → list the last N logged expenses (for picking which to delete)
"""

import json
import logging
from datetime import datetime
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import HISTORY_FILE, HISTORY_LIMIT
from parsing.category_map import CATEGORY_MAP, BROAD_CATEGORIES
from sheets import (
    _build_service,
    find_tab_for_month,
    find_category_row,
    _read_current_amount,
    _read_existing_note,
    _write_amount,
    _write_note,
    get_spreadsheet_tabs,
    SPREADSHEET_ID,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

def help() -> str:
    return (
        "Here is what I can do:\n\n"
        "Logging an expense:\n"
        "  Just type it naturally, e.g.:\n"
        "    groceries 120\n"
        "    Mortgage insurance 1052 paid online\n"
        "    250 fuel toyota\n\n"
        "Commands:\n"
        "  /summary              — this month's budget overview\n"
        "  /category <name>      — details + history for one category\n"
        "  /balance <name>       — quick remaining balance for a category\n"
        "  /categories           — list all available categories\n"
        "  /keywords <name>      — show keywords that trigger a category\n"
        "  /delete               — undo the most recent expense\n"
        "  /delete <n>           — undo the last n expenses (e.g. /delete 3)\n"
        "  /help                 — show this message\n"
    )


# ---------------------------------------------------------------------------
# /categories
# ---------------------------------------------------------------------------

def categories() -> str:
    lines = ["Available categories:\n"]
    for section, cats in BROAD_CATEGORIES.items():
        lines.append(f"{section}:")
        for cat in cats:
            lines.append(f"  • {cat}")
        lines.append("")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# /keywords <name>
# ---------------------------------------------------------------------------

def keywords(name: str) -> str:
    name_lower = name.strip().lower()

    for cat_name, kw_list in CATEGORY_MAP.items():
        if cat_name.lower() == name_lower:
            joined = "\n  ".join(kw_list)
            return f"Keywords for '{cat_name}':\n  {joined}"

    for cat_name, kw_list in CATEGORY_MAP.items():
        if name_lower in [kw.lower() for kw in kw_list]:
            joined = "\n  ".join(kw_list)
            return f"'{name}' is a keyword for '{cat_name}'.\nAll keywords:\n  {joined}"

    return (
        f"Category '{name}' not found.\n"
        "Use /categories to see all available category names."
    )


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------

# Emoji assigned to each broad section — shown in /summary and on buttons.
SECTION_EMOJIS: dict[str, str] = {
    "Home":           "🏠",
    "Transportation": "🚗",
    "Daily Living":   "🛒",
    "Other":          "📦",
    # fallback for any section not listed above
}

def _section_emoji(name: str) -> str:
    return SECTION_EMOJIS.get(name, "📋")


def _fmt_amount(amount: float) -> str:
    """Format an absolute currency amount: ₪1,234"""
    return f"₪{abs(amount):,.0f}"


def _fmt_signed(amount: float) -> str:
    """Format a balance with sign: +₪500, -₪200, ₪0"""
    if amount > 0:
        return f"+₪{amount:,.0f}"
    if amount < 0:
        return f"-₪{abs(amount):,.0f}"
    return "₪0"


def _find_row_index(rows: list, name: str) -> Optional[int]:
    """Return the 0-based index of the first row whose column A matches name (case-insensitive)."""
    for i, row in enumerate(rows):
        if row and row[0].strip().lower() == name.lower():
            return i
    return None


def _prev_month(dt: datetime) -> datetime:
    if dt.month == 1:
        return dt.replace(year=dt.year - 1, month=12, day=1)
    return dt.replace(month=dt.month - 1, day=1)


def _next_month(dt: datetime) -> datetime:
    if dt.month == 12:
        return dt.replace(year=dt.year + 1, month=1, day=1)
    return dt.replace(month=dt.month + 1, day=1)


def _tab_not_found_message(service, dt: datetime) -> str:
    """
    Build a helpful 'sheet missing' message for the slash-command path.

    Unlike the logging flow (which goes through the AI explanation), slash
    commands return plain strings synchronously. A deterministic template
    that lists the user's most recent tab names is usually enough — they
    can see the naming convention and fix their sheet name themselves.
    """
    expected = dt.strftime("%m%y")
    month_label = dt.strftime("%B %Y")
    try:
        existing = [title for (title, _sid) in get_spreadsheet_tabs(service).values()]
    except Exception as exc:
        logger.warning(f"Could not fetch tab list for not-found message: {exc}")
        existing = []

    if not existing:
        return (
            f"No sheet tab found for {month_label}. "
            f"Please create a tab named '{expected}'."
        )

    recent = ", ".join(existing[:5])
    return (
        f"No sheet tab found for {month_label}. "
        f"Your existing sheets: {recent}. "
        f"If you have a tab for this month but it's named differently, "
        f"please rename it to '{expected}'."
    )


def _summary_keyboard(dt: datetime, section_names: list[str] = None) -> InlineKeyboardMarkup:
    """
    Build the summary keyboard:
      - One row of section drill-down buttons per pair of sections (if section_names given)
      - Final row: ← prev month  |  next month →
    """
    rows = []

    if section_names:
        # Pair sections into rows of 2
        for i in range(0, len(section_names), 2):
            pair = section_names[i:i + 2]
            rows.append([
                InlineKeyboardButton(
                    f"{_section_emoji(name)} {name}",
                    callback_data=f"section|{name}|{dt.year}|{dt.month}",
                )
                for name in pair
            ])

    prev = _prev_month(dt)
    nxt  = _next_month(dt)
    rows.append([
        InlineKeyboardButton(
            f"← {prev.strftime('%b %Y')}",
            callback_data=f"summary|{prev.year}|{prev.month}",
        ),
        InlineKeyboardButton(
            f"{nxt.strftime('%b %Y')} →",
            callback_data=f"summary|{nxt.year}|{nxt.month}",
        ),
    ])

    return InlineKeyboardMarkup(rows)


def _format_summary_html(
    dt: datetime,
    sections: list[tuple],
    grand_spent: float,
    grand_budget: float,
    grand_balance: float,
    over_budget: list[str],
) -> str:
    """
    Build the HTML summary message using an emoji-list layout.
    One card per broad section, no <pre> table (avoids mobile wrapping issues).
    """
    lines = [f"<b>Budget — {dt.strftime('%B %Y')}</b>\n"]

    for name, spent, budget, bal in sections:
        emoji = _section_emoji(name)
        warning = " ⚠" if bal < 0 else ""
        lines.append(f"{emoji} <b>{name}</b>{warning}   {_fmt_amount(spent)} / {_fmt_amount(budget)}")

    lines.append("")  # blank line before total
    overall = "⚠️" if grand_balance < 0 else "✅"
    lines.append(
        f"💰 <b>Total   {_fmt_amount(grand_spent)} / {_fmt_amount(grand_budget)}</b>  {overall}"
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /summary
# ---------------------------------------------------------------------------

def summary(dt: datetime = None) -> tuple[str, InlineKeyboardMarkup]:
    """
    Return (html_text, navigation_keyboard) for the given month.

    Reads each broad category's total row directly from the sheet:
      - Finds the broad category header in column A
      - Reads B, C, D from row: header_row + len(subcategories) + 1
    This trusts the sheet's own totals rather than summing subcategories in Python.
    """
    if dt is None:
        dt = datetime.now()

    service  = _build_service()
    tab_info = find_tab_for_month(service, dt)

    if not tab_info:
        return (
            _tab_not_found_message(service, dt),
            _summary_keyboard(dt),
        )

    tab_name, _ = tab_info

    # One API call — read all of A:D
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{tab_name}'!A1:D200"
    ).execute()
    rows = result.get("values", [])

    sections      = []
    grand_spent   = 0.0
    grand_budget  = 0.0
    grand_balance = 0.0
    over_budget   = []

    for section_name, subcats in BROAD_CATEGORIES.items():
        header_idx = _find_row_index(rows, section_name)
        if header_idx is None:
            continue

        # The sheet places the section total x+1 rows below the header,
        # where x = number of subcategories in that section.
        total_idx = header_idx + len(subcats) + 1
        if total_idx >= len(rows):
            continue

        total_row = rows[total_idx]
        budget  = _parse_currency(total_row[1] if len(total_row) > 1 else "")
        spent   = _parse_currency(total_row[2] if len(total_row) > 2 else "")
        balance = _parse_currency(total_row[3] if len(total_row) > 3 else "")

        sections.append((section_name, spent, budget, balance))
        grand_spent   += spent
        grand_budget  += budget
        grand_balance += balance

        if balance < 0:
            over_budget.append(section_name)

    text = _format_summary_html(
        dt, sections, grand_spent, grand_budget, grand_balance, over_budget
    )
    keyboard = _summary_keyboard(dt, section_names=[s[0] for s in sections])
    return text, keyboard


# ---------------------------------------------------------------------------
# Section drill-down (tapped from /summary keyboard)
# ---------------------------------------------------------------------------

def section_detail(section_name: str, dt: datetime = None) -> tuple[str, InlineKeyboardMarkup]:
    """
    Return (html_text, back_keyboard) showing each subcategory's spent/budget for
    the given broad section and month.
    """
    if dt is None:
        dt = datetime.now()

    subcats = BROAD_CATEGORIES.get(section_name)
    if not subcats:
        return f"Section '{section_name}' not found.", _summary_keyboard(dt)

    service  = _build_service()
    tab_info = find_tab_for_month(service, dt)
    if not tab_info:
        return (
            _tab_not_found_message(service, dt),
            _summary_keyboard(dt),
        )

    tab_name, _ = tab_info
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{tab_name}'!A1:D200",
    ).execute()
    rows = result.get("values", [])

    emoji = _section_emoji(section_name)
    lines = [f"{emoji} <b>{section_name} — {dt.strftime('%B %Y')}</b>\n"]

    for cat in subcats:
        idx = _find_row_index(rows, cat)
        if idx is None:
            continue
        row = rows[idx]
        budget = _parse_currency(row[1] if len(row) > 1 else "")
        spent  = _parse_currency(row[2] if len(row) > 2 else "")
        bal    = _parse_currency(row[3] if len(row) > 3 else "")
        warning = " ⚠" if bal < 0 else ""
        lines.append(f"  • <b>{cat}</b>{warning}   {_fmt_amount(spent)} / {_fmt_amount(budget)}")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"← Back to summary",
            callback_data=f"summary|{dt.year}|{dt.month}",
        ),
    ]])
    return "\n".join(lines), keyboard


# ---------------------------------------------------------------------------
# /category <name>
# ---------------------------------------------------------------------------

def category(name: str, dt: datetime = None) -> str:
    if dt is None:
        dt = datetime.now()

    canonical = _resolve_category_name(name)
    if not canonical:
        return (
            f"Category '{name}' not found.\n"
            "Use /categories to see all available category names."
        )

    service = _build_service()
    tab_info = find_tab_for_month(service, dt)
    if not tab_info:
        return _tab_not_found_message(service, dt)
    tab_name, _ = tab_info

    row = find_category_row(service, tab_name, canonical)
    if not row:
        return f"Category '{canonical}' not found in tab '{tab_name}'."

    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{tab_name}'!B{row}:D{row}"
    ).execute()
    vals = result.get("values", [[]])[0] if result.get("values") else []
    budget  = _parse_currency(vals[0] if len(vals) > 0 else "")
    actual  = _parse_currency(vals[1] if len(vals) > 1 else "")
    balance = _parse_currency(vals[2] if len(vals) > 2 else "")

    note = _read_existing_note(service, tab_name, row)

    status = "✅" if balance >= 0 else "⚠️"
    lines = [
        f"{status} {canonical} — {dt.strftime('%B %Y')}",
        f"  Budget:  ₪{budget:,.2f}",
        f"  Spent:   ₪{actual:,.2f}",
        f"  Balance: ₪{balance:,.2f}",
    ]

    if note:
        lines.append("\nTransaction history:")
        for entry in note.strip().split("\n"):
            if entry.strip():
                lines.append(f"  {entry.strip()}")
    else:
        lines.append("\nNo transactions recorded yet.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /balance <name>
# ---------------------------------------------------------------------------

def balance(name: str, dt: datetime = None) -> str:
    if dt is None:
        dt = datetime.now()

    canonical = _resolve_category_name(name)
    if not canonical:
        return f"Category '{name}' not found. Use /categories to see all categories."

    service = _build_service()
    tab_info = find_tab_for_month(service, dt)
    if not tab_info:
        return _tab_not_found_message(service, dt)
    tab_name, _ = tab_info

    row = find_category_row(service, tab_name, canonical)
    if not row:
        return f"Category '{canonical}' not found in tab '{tab_name}'."

    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{tab_name}'!B{row}:D{row}"
    ).execute()
    vals = result.get("values", [[]])[0] if result.get("values") else []
    budget      = _parse_currency(vals[0] if len(vals) > 0 else "")
    balance_val = _parse_currency(vals[2] if len(vals) > 2 else "")

    status = "✅" if balance_val >= 0 else "⚠️"
    return (
        f"{status} {canonical}: ₪{balance_val:,.2f} remaining "
        f"(budget ₪{budget:,.2f})"
    )


# ---------------------------------------------------------------------------
# Expense history — used by /delete
#
# expense_history.json holds a list of the last HISTORY_LIMIT expenses,
# most recent first (index 0 = most recent).
#
# Each entry:
#   {
#     "category":      "Groceries",
#     "amount":        50.0,
#     "tab_name":      "0426",
#     "row":           45,
#     "timestamp":     "2026-04-01 15:04",
#     "original_text": "super 50 milk"
#   }
# ---------------------------------------------------------------------------

def load_history() -> list[dict]:
    """Load the expense history list from disk. Returns [] if file doesn't exist."""
    try:
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_history(history: list[dict]) -> None:
    """Write the expense history list to disk."""
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def append_to_history(
    category: str,
    amount: float,
    tab_name: str,
    row: int,
    timestamp: str,
    original_text: str,
) -> None:
    """
    Add a new expense to the front of the history list and trim to HISTORY_LIMIT.
    Called by handlers/message.py right after a successful log_expense().
    """
    history = load_history()
    history.insert(0, {
        "category":      category,
        "amount":        amount,
        "tab_name":      tab_name,
        "row":           row,
        "timestamp":     timestamp,
        "original_text": original_text,
    })
    history = history[:HISTORY_LIMIT]
    save_history(history)


def show_history() -> str:
    """Return a numbered list of the last HISTORY_LIMIT expenses."""
    history = load_history()
    if not history:
        return "No recent expenses on record."

    lines = ["Recent expenses (most recent first):\n"]
    for i, entry in enumerate(history, start=1):
        lines.append(
            f"  {i}. [{entry['timestamp']}]  {entry['original_text']}  "
            f"→ {entry['category']}"
        )
    lines.append(f"\nUse /delete <n> to undo one of these.")
    return "\n".join(lines)


def delete(n: int = 1) -> str:
    """
    Undo the last n expenses (n=1 means only the most recent, n=3 means the last 3).
    For each entry: subtracts the amount from the sheet and removes the note line.
    Prints a summary of everything that was deleted.
    """
    history = load_history()

    if not history:
        return "Nothing to delete — no recent expenses on record."

    if n < 1:
        return "Please use /delete or /delete <number> (e.g. /delete 3)."

    to_delete = history[:n]
    if not to_delete:
        return "Nothing to delete."

    service = _build_service()

    # Cache sheet_ids so we don't fetch metadata more than once per tab
    sheet_id_cache: dict[str, int] = {}
    meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    for s in meta.get("sheets", []):
        sheet_id_cache[s["properties"]["title"]] = s["properties"]["sheetId"]

    lines = [f"✅ Deleted {len(to_delete)} expense(s):\n"]

    for i, entry in enumerate(to_delete, start=1):
        category_name = entry["category"]
        amount        = entry["amount"]
        tab_name      = entry["tab_name"]
        row           = entry["row"]
        timestamp     = entry["timestamp"]

        sheet_id = sheet_id_cache.get(tab_name)
        if sheet_id is None:
            lines.append(f"  {i}. ⚠️ Could not find tab '{tab_name}' — skipped.")
            continue

        # Subtract amount
        current   = _read_current_amount(service, tab_name, row)
        new_total = current - amount
        _write_amount(service, tab_name, row, new_total)

        # Remove matching note line
        existing_note = _read_existing_note(service, tab_name, row)
        updated_lines = [
            line for line in existing_note.split("\n")
            if not line.startswith(timestamp)
        ]
        _write_note(service, tab_name, row, sheet_id, "\n".join(updated_lines).strip())

        lines.append(
            f"  {i}. {entry['original_text']}  "
            f"(₪{amount:g} from '{category_name}', new total ₪{new_total:g})"
        )

    # Remove deleted entries from history
    history = history[len(to_delete):]
    save_history(history)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_currency(value: str) -> float:
    try:
        return float(str(value).replace("₪", "").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def _resolve_category_name(name: str) -> Optional[str]:
    name_lower = name.strip().lower()
    for cat_name in CATEGORY_MAP:
        if cat_name.lower() == name_lower:
            return cat_name
    return None


# ---------------------------------------------------------------------------
# Telegram async wrappers
#
# Thin async functions that call the sync logic above and reply to the user.
# All replies use parse_mode="HTML" for rich formatting.
# These are imported by bot.py and registered as CommandHandlers.
# ---------------------------------------------------------------------------

from handlers.subscribers import track_subscriber


async def tg_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_subscriber(update.effective_chat.id)
    now = datetime.now()
    text = (
        "<b>How to log an expense:</b>\n"
        "<code>keyword  amount</code>\n"
        "<code>keyword  amount  note</code>\n"
        "<code>amount  keyword</code>  (reversed order also works)\n\n"
        "Examples:\n"
        "<code>groceries 120</code>\n"
        "<code>fuel 50 shell station</code>\n"
        "<code>250 train</code>\n\n"
        "<b>Commands:</b>\n"
        "/summary — monthly budget overview\n"
        "/categories — list all categories &amp; keywords\n"
        "/category &lt;name&gt; — spending details for one category\n"
        "/balance &lt;name&gt; — remaining budget for one category\n"
        "/keywords &lt;name&gt; — what triggers a category\n"
        "/delete — undo the last expense\n"
        "/delete &lt;n&gt; — undo the last n expenses"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Summary",    callback_data=f"summary|{now.year}|{now.month}"),
            InlineKeyboardButton("📋 Categories", callback_data="help_categories"),
        ],
        [
            InlineKeyboardButton("🗑 Delete last", callback_data="help_delete"),
        ],
    ])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def tg_categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_subscriber(update.effective_chat.id)
    lines = []
    for section, cats in BROAD_CATEGORIES.items():
        lines.append(f"\n<b>{section}</b>")
        for cat in cats:
            lines.append(f"  • {cat}")
    await update.message.reply_text("\n".join(lines).strip(), parse_mode="HTML")


async def tg_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_subscriber(update.effective_chat.id)
    name = " ".join(context.args) if context.args else ""
    if not name:
        await update.message.reply_text(
            "Usage: /keywords &lt;category name&gt;", parse_mode="HTML"
        )
    else:
        await update.message.reply_text(keywords(name), parse_mode="HTML")


async def tg_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_subscriber(update.effective_chat.id)
    # Send a "loading" message first, then edit it in-place with the real data.
    msg = await update.message.reply_text("Fetching summary...")
    text, keyboard = summary()
    await msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


async def tg_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_subscriber(update.effective_chat.id)
    name = " ".join(context.args) if context.args else ""
    if not name:
        await update.message.reply_text(
            "Usage: /category &lt;category name&gt;", parse_mode="HTML"
        )
    else:
        await update.message.reply_text(category(name), parse_mode="HTML")


async def tg_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_subscriber(update.effective_chat.id)
    name = " ".join(context.args) if context.args else ""
    if not name:
        await update.message.reply_text(
            "Usage: /balance &lt;category name&gt;", parse_mode="HTML"
        )
    else:
        await update.message.reply_text(balance(name), parse_mode="HTML")


async def tg_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_subscriber(update.effective_chat.id)
    try:
        n = int(context.args[0]) if context.args else 1
    except ValueError:
        await update.message.reply_text(
            "Usage: /delete  or  /delete &lt;number&gt;  e.g. /delete 3",
            parse_mode="HTML",
        )
        return
    await update.message.reply_text(delete(n), parse_mode="HTML")
