"""
handlers/monthly_report.py — Automated monthly summary + anomaly report.

Runs on the 1st of each month at 09:00 Israel time.
Covers the previous calendar month.

Public API:
    send_monthly_report(context)  — job callback registered in bot.py
    format_monthly_report(dt)     — build (html_text, keyboard) for any month
"""

import json
import logging
import math
from datetime import datetime
from typing import Optional

from telegram.ext import ContextTypes

from config import SUBSCRIBERS_FILE, SPREADSHEET_ID
from handlers.commands import (
    _find_row_index,
    _fmt_amount,
    _parse_currency,
    _section_emoji,
    _summary_keyboard,
)
from parsing.category_map import BROAD_CATEGORIES
from sheets import _build_service, find_tab_for_month, find_tab_in_tabs, get_spreadsheet_tabs

logger = logging.getLogger(__name__)

# Maximum number of anomaly bullets shown in the report.
MAX_ANOMALIES = 5

# Tier 2: only flag if spending is this many times above the rolling average.
ANOMALY_MULTIPLIER = 1.5

# Tier 2: skip historical comparison for categories whose coefficient of
# variation exceeds this threshold (too noisy to be meaningful).
HIGH_VARIANCE_COV = 1.0

# Minimum months of history required before Tier 2 runs.
MIN_HISTORY_MONTHS = 3

# How far back to look for historical data (months).
MAX_HISTORY_MONTHS = 12


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _prev_month_dt(dt: datetime) -> datetime:
    """Return the first day of the month preceding dt."""
    if dt.month == 1:
        return dt.replace(year=dt.year - 1, month=12, day=1)
    return dt.replace(month=dt.month - 1, day=1)


def _shift_months_back(dt: datetime, n: int) -> datetime:
    """Return the first day of the month n months before dt."""
    month = dt.month - n
    year  = dt.year
    while month <= 0:
        month += 12
        year  -= 1
    return dt.replace(year=year, month=month, day=1)


# ---------------------------------------------------------------------------
# Historical data
# ---------------------------------------------------------------------------

def _extract_section_spent(rows: list, section_name: str, subcats: list[str]) -> Optional[float]:
    """
    Extract the total-row spent value for one broad section from pre-loaded rows.
    Returns None if the section header or total row is not found.
    """
    header_idx = _find_row_index(rows, section_name)
    if header_idx is None:
        return None
    total_idx = header_idx + len(subcats) + 1
    if total_idx >= len(rows):
        return None
    total_row = rows[total_idx]
    return _parse_currency(total_row[2] if len(total_row) > 2 else "")


def _build_history_tab_data(
    service,
    prev_month_dt: datetime,
    existing_tabs: dict,
    months_back: int = MAX_HISTORY_MONTHS,
) -> list[tuple]:
    """
    Fetch A1:D200 for each available historical month tab (oldest → newest).
    Uses the already-fetched `existing_tabs` dict — no additional metadata call.
    Makes ONE data read per found tab (max `months_back` reads).
    Returns a list of (rows, dt) pairs for each month that had a tab.
    """
    result = []
    for i in range(months_back, 0, -1):   # oldest first
        dt       = _shift_months_back(prev_month_dt, i)
        tab_info = find_tab_in_tabs(existing_tabs, dt)
        if not tab_info:
            continue
        tab_name, _ = tab_info
        try:
            resp = service.spreadsheets().values().get(
                spreadsheetId=SPREADSHEET_ID,
                range=f"'{tab_name}'!A1:D200",
            ).execute()
            result.append((resp.get("values", []), dt))
        except Exception as exc:
            logger.debug(f"Skipping tab {tab_name}: {exc}")
    return result


def get_historical_spending(
    section_name: str,
    subcats: list[str],
    history_tab_data: list[tuple],   # list of (rows, dt) from _build_history_tab_data
) -> list[float]:
    """
    Extract spent amounts for `section_name` from pre-fetched historical tab data.
    Returns a list ordered oldest → newest (missing months omitted).
    """
    results = []
    for rows, _ in history_tab_data:
        spent = _extract_section_spent(rows, section_name, subcats)
        if spent is not None:
            results.append(spent)
    return results


# ---------------------------------------------------------------------------
# Anomaly detection
# ---------------------------------------------------------------------------

def detect_anomalies(
    current_sections: list[tuple],       # (name, spent, budget, balance)
    history_map: dict[str, list[float]], # section_name → [spent_oldest, ..., spent_newest]
) -> tuple[list[str], int]:
    """
    Return (anomaly_strings, months_available) where:
    - anomaly_strings: up to MAX_ANOMALIES formatted HTML bullet strings
    - months_available: how many months of history we actually found
    """
    months_available = max((len(v) for v in history_map.values()), default=0)
    scored: list[tuple[float, str]] = []

    for name, spent, budget, balance in current_sections:
        hist = history_map.get(name, [])

        # -- Tier 1: over-budget (always, if budget > 0) --
        if budget > 0 and spent > budget:
            pct = round((spent - budget) / budget * 100)
            scored.append((
                float(pct),
                f"<b>{name}</b> {pct}% over budget "
                f"({_fmt_amount(spent)} vs {_fmt_amount(budget)} budget)",
            ))

        # -- Tier 2: historical comparison --
        if len(hist) < MIN_HISTORY_MONTHS:
            continue

        mean = sum(hist) / len(hist)

        if mean > 0:
            variance = sum((x - mean) ** 2 for x in hist) / len(hist)
            std_dev  = math.sqrt(variance)
            cov      = std_dev / mean

            if cov >= HIGH_VARIANCE_COV:
                continue  # too noisy

            if spent > mean * ANOMALY_MULTIPLIER:
                # Skip if already flagged as over-budget (avoid duplication)
                already_flagged = budget > 0 and spent > budget
                if not already_flagged:
                    pct = round((spent - mean) / mean * 100)
                    scored.append((
                        float(pct),
                        f"<b>{name}</b>: {_fmt_amount(spent)} vs "
                        f"{_fmt_amount(mean)} avg last {len(hist)} months (+{pct}%)",
                    ))

        elif mean == 0 and spent > 0 and budget == 0:
            # New/unplanned spending in a category that was historically zero
            scored.append((
                100.0,
                f"<b>{name}</b>: {_fmt_amount(spent)} — new unplanned spending "
                f"(historically ₪0)",
            ))

    # Sort by severity (highest %) descending, cap at MAX_ANOMALIES
    scored.sort(key=lambda x: x[0], reverse=True)
    return [msg for _, msg in scored[:MAX_ANOMALIES]], months_available


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def format_monthly_report(prev_month_dt: datetime) -> tuple[str, object]:
    """
    Build the full (html_text, InlineKeyboardMarkup) for the monthly report.

    API call budget:
        1  — spreadsheet metadata (tab list)
        1  — current month data
      ≤12  — one per available historical month tab
      ─────
      ≤14  total  (well within the 60 req/min quota)
    """
    service = _build_service()

    # ── 1. Fetch metadata once, resolve current-month tab ──────────────────
    existing_tabs = get_spreadsheet_tabs(service)
    tab_info = find_tab_in_tabs(existing_tabs, prev_month_dt)

    if not tab_info:
        keyboard = _summary_keyboard(prev_month_dt)
        return (
            f"📅 <b>Monthly Summary — {prev_month_dt.strftime('%B %Y')}</b>\n\n"
            f"No sheet tab found for this month.",
            keyboard,
        )

    tab_name, _ = tab_info

    # ── 2. Read current month (1 API call) ─────────────────────────────────
    resp = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{tab_name}'!A1:D200",
    ).execute()
    rows = resp.get("values", [])

    sections: list[tuple] = []
    grand_spent = grand_budget = grand_balance = 0.0

    for section_name, subcats in BROAD_CATEGORIES.items():
        header_idx = _find_row_index(rows, section_name)
        if header_idx is None:
            continue
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

    # ── 3. Read historical months (1 API call per tab, reusing metadata) ───
    history_tab_data = _build_history_tab_data(service, prev_month_dt, existing_tabs)

    history_map: dict[str, list[float]] = {
        section_name: get_historical_spending(section_name, subcats, history_tab_data)
        for section_name, subcats in BROAD_CATEGORIES.items()
    }

    # ── 4. Detect anomalies & format ───────────────────────────────────────
    anomalies, months_available = detect_anomalies(sections, history_map)

    lines = [f"📅 <b>Monthly Summary — {prev_month_dt.strftime('%B %Y')}</b>\n"]

    for name, spent, budget, balance in sections:
        emoji = _section_emoji(name)
        if budget > 0:
            warning = " ⚠" if balance < 0 else ""
            lines.append(
                f"{emoji} <b>{name}</b>{warning}   {_fmt_amount(spent)} / {_fmt_amount(budget)}"
            )
        else:
            lines.append(f"{emoji} <b>{name}</b>   {_fmt_amount(spent)}")

    lines.append("")
    # Only include sections with an actual budget in the budget comparison
    budgeted_spent  = sum(s for _, s, b, _ in sections if b > 0)
    budgeted_budget = sum(b for _, _, b, _ in sections if b > 0)
    unbudgeted_spent = grand_spent - budgeted_spent

    if budgeted_budget > 0:
        overall = "⚠️" if budgeted_spent > budgeted_budget else "✅"
        lines.append(
            f"💰 <b>Total (budgeted)   {_fmt_amount(budgeted_spent)} / {_fmt_amount(budgeted_budget)}</b>  {overall}"
        )
        if unbudgeted_spent > 0:
            lines.append(f"➕ <b>Unbudgeted spending   {_fmt_amount(unbudgeted_spent)}</b>")
    else:
        lines.append(f"💰 <b>Total spent   {_fmt_amount(grand_spent)}</b>")

    lines.append("")
    if anomalies:
        lines.append("⚠ <b>Anomalies:</b>")
        for a in anomalies:
            lines.append(f"  • {a}")
    elif months_available < MIN_HISTORY_MONTHS:
        lines.append(
            f"ℹ Anomaly detection starts at month {MIN_HISTORY_MONTHS} "
            f"({months_available} month{'s' if months_available != 1 else ''} of history so far)"
        )
    else:
        lines.append("✅ No significant anomalies this month")

    text     = "\n".join(lines)
    keyboard = _summary_keyboard(prev_month_dt, section_names=[s[0] for s in sections])
    return text, keyboard


# ---------------------------------------------------------------------------
# Job callback
# ---------------------------------------------------------------------------

async def send_monthly_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    JobQueue callback — runs on the 1st of each month at 09:00 Israel time.
    Generates the report for the previous month and sends it to all subscribers.
    """
    now      = datetime.now()
    prev_dt  = _prev_month_dt(now)

    logger.info(f"Sending monthly report for {prev_dt.strftime('%B %Y')}...")

    try:
        text, keyboard = format_monthly_report(prev_dt)
    except Exception:
        logger.exception("Monthly report generation failed")
        return

    # Load subscriber list
    try:
        with open(SUBSCRIBERS_FILE) as f:
            subscribers: list[int] = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        subscribers = []

    if not subscribers:
        logger.warning("Monthly report: no subscribers found, nothing sent.")
        return

    for chat_id in subscribers:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            logger.info(f"Monthly report sent to {chat_id}")
        except Exception:
            logger.warning(f"Failed to send monthly report to {chat_id}", exc_info=True)


async def tg_test_report(update, context) -> None:
    """
    /report [MMYYYY]  — send the monthly report to the current chat immediately.

    With no argument → uses the previous calendar month.
    With an argument  → parses it as a month, e.g. /report 032026 or /report 2026-03.

    This command exists only for testing — it sends the report just to the sender,
    not to all subscribers.
    """
    from handlers.subscribers import track_subscriber
    track_subscriber(update.effective_chat.id)

    arg = " ".join(context.args).strip() if context.args else ""

    if arg:
        # Try to parse the argument as a month reference
        target_dt = _parse_month_arg(arg)
        if target_dt is None:
            await update.message.reply_text(
                "Couldn't parse that month. Try formats like:\n"
                "<code>/report</code>  (previous month)\n"
                "<code>/report 032026</code>\n"
                "<code>/report 2026-03</code>",
                parse_mode="HTML",
            )
            return
    else:
        target_dt = _prev_month_dt(datetime.now())

    msg = await update.message.reply_text(
        f"Generating report for {target_dt.strftime('%B %Y')}..."
    )

    try:
        text, keyboard = format_monthly_report(target_dt)
    except Exception:
        logger.exception("Test report generation failed")
        await msg.edit_text("❌ Report generation failed — check the logs.")
        return

    await msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


def _parse_month_arg(arg: str) -> Optional[datetime]:
    """
    Try to parse a user-supplied month string into a datetime (day=1).
    Accepts a broad range of formats: MMYYYY, MM/YYYY, YYYY-MM, etc.
    Returns None if nothing matches.
    """
    import re
    arg = arg.strip()
    patterns = [
        (r"^(\d{2})(\d{4})$",      lambda m: datetime(int(m[2]), int(m[1]), 1)),  # 032026
        (r"^(\d{1,2})/(\d{4})$",   lambda m: datetime(int(m[2]), int(m[1]), 1)),  # 3/2026
        (r"^(\d{4})-(\d{1,2})$",   lambda m: datetime(int(m[1]), int(m[2]), 1)),  # 2026-03
        (r"^(\d{2})/(\d{2})$",     lambda m: datetime(2000 + int(m[2]), int(m[1]), 1)),  # 03/26
        (r"^(\d{2})(\d{2})$",      lambda m: datetime(2000 + int(m[2]), int(m[1]), 1)),  # 0326
    ]
    for pattern, builder in patterns:
        m = re.match(pattern, arg)
        if m:
            try:
                return builder(m)
            except ValueError:
                continue
    return None
