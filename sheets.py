"""
sheets.py — Google Sheets integration.

Responsibilities:
  - Connect to the Sheets API using service account credentials.
  - Find the correct month tab from a wide range of supported name formats.
  - Find the row for a given category in column A.
  - Read the current amount from column C.
  - Write the new cumulative amount to column C.
  - Append a timestamped entry to the cell note on column C.

Note format per entry (appended, never overwritten):
  YYYY-MM-DD HH:MM  <full message as typed by user>
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build

from config import SPREADSHEET_ID, GOOGLE_CREDENTIALS_JSON

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


# ---------------------------------------------------------------------------
# Supported month tab name formats (all matched case-insensitively)
#
#   MMYY          →  0326
#   MM/YY         →  03/26
#   MM-YY         →  03-26
#   MM.YY         →  03.26
#   YYYY-MM       →  2026-03
#   MM/YYYY       →  03/2026
#   M/YYYY        →  3/2026
#   Month YYYY    →  March 2026
#   Mon YYYY      →  Mar 2026
#   YYYY Month    →  2026 March
#   Month YY      →  March 26
# ---------------------------------------------------------------------------

def _candidate_tab_names(dt: datetime) -> list[str]:
    """Return all plausible tab names for a given month, most specific first."""
    mm    = dt.strftime("%m")   # 03
    yy    = dt.strftime("%y")   # 26
    yyyy  = dt.strftime("%Y")   # 2026
    mon   = dt.strftime("%b")   # Mar
    month = dt.strftime("%B")   # March
    m     = str(dt.month)       # 3

    return [
        f"{mm}{yy}",           # 0326
        f"{mm}/{yy}",          # 03/26
        f"{mm}-{yy}",          # 03-26
        f"{mm}.{yy}",          # 03.26
        f"{yyyy}-{mm}",        # 2026-03
        f"{mm}/{yyyy}",        # 03/2026
        f"{m}/{yyyy}",         # 3/2026
        f"{month} {yyyy}",     # March 2026
        f"{mon} {yyyy}",       # Mar 2026
        f"{yyyy} {month}",     # 2026 March
        f"{yyyy} {mon}",       # 2026 Mar
        f"{month} {yy}",       # March 26
        f"{mon} {yy}",         # Mar 26
    ]


# ---------------------------------------------------------------------------
# Result objects
# ---------------------------------------------------------------------------

@dataclass
class LogResult:
    success: bool
    category: str
    amount_added: float
    new_total: float
    tab_name: str
    row: int
    timestamp: str        # the timestamp written into the note (used by /delete)
    message: str          # human-readable summary


# ---------------------------------------------------------------------------
# API connection — cached singleton to avoid per-request memory growth
# ---------------------------------------------------------------------------

_service_cache = None

def _build_service():
    global _service_cache
    if _service_cache is not None:
        return _service_cache
    if not GOOGLE_CREDENTIALS_JSON:
        raise EnvironmentError("GOOGLE_CREDENTIALS environment variable is not set.")
    creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = service_account.Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    _service_cache = build("sheets", "v4", credentials=creds)
    return _service_cache


# ---------------------------------------------------------------------------
# Tab resolution
# ---------------------------------------------------------------------------

def get_spreadsheet_tabs(service) -> dict[str, tuple[str, int]]:
    """
    Fetch all sheet tab titles once and return a lookup dict.
    Returns:  lowercase_title -> (original_title, sheet_id)
    Use this when you need to resolve multiple months — it avoids a metadata
    API call for every individual month lookup.
    """
    metadata = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    return {
        s["properties"]["title"].lower(): (
            s["properties"]["title"],
            s["properties"]["sheetId"],
        )
        for s in metadata.get("sheets", [])
    }


def find_tab_in_tabs(
    existing_tabs: dict[str, tuple[str, int]],
    dt: datetime,
) -> Optional[tuple[str, int]]:
    """
    Find the tab for `dt` using a pre-fetched tabs dict (no API call).
    Returns (tab_name, sheet_id) or None.
    """
    for candidate in _candidate_tab_names(dt):
        match = existing_tabs.get(candidate.lower())
        if match:
            logger.info(f"Matched tab '{match[0]}' for {dt.strftime('%B %Y')}")
            return match
    logger.debug(f"No tab found for {dt.strftime('%B %Y')}")
    return None


def find_tab_for_month(service, dt: datetime) -> Optional[tuple[str, int]]:
    """
    Find the tab name and its internal sheetId for the given month.
    Makes one metadata API call. Use find_tab_in_tabs() when resolving
    multiple months in a row to avoid repeated metadata fetches.
    Returns (tab_name, sheet_id) or None if not found.
    """
    existing_tabs = get_spreadsheet_tabs(service)
    result = find_tab_in_tabs(existing_tabs, dt)
    if not result:
        logger.warning(f"No tab found for {dt.strftime('%B %Y')}. Tried: {_candidate_tab_names(dt)}")
    return result


# ---------------------------------------------------------------------------
# Row lookup
# ---------------------------------------------------------------------------

def find_category_row(service, tab_name: str, category: str) -> Optional[int]:
    """
    Find the 1-indexed row number where column A matches `category`
    (case-insensitive). Returns None if not found.
    """
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{tab_name}'!A1:A200"
    ).execute()

    rows = result.get("values", [])
    for i, row in enumerate(rows):
        if row and row[0].strip().lower() == category.lower():
            return i + 1  # 1-indexed

    logger.warning(f"Category '{category}' not found in tab '{tab_name}'")
    return None


# ---------------------------------------------------------------------------
# Amount read / write
# ---------------------------------------------------------------------------

def _read_current_amount(service, tab_name: str, row: int) -> float:
    """Read the current numeric value from column C of the given row."""
    cell = f"'{tab_name}'!C{row}"
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=cell
    ).execute()

    values = result.get("values", [])
    if not values or not values[0]:
        return 0.0

    raw = values[0][0]
    try:
        return float(str(raw).replace("₪", "").replace(",", "").strip() or 0)
    except ValueError:
        logger.warning(f"Could not parse amount '{raw}' at {cell}, defaulting to 0")
        return 0.0


def _write_amount(service, tab_name: str, row: int, new_amount: float) -> None:
    """Write the new cumulative amount to column C of the given row."""
    cell = f"'{tab_name}'!C{row}"
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=cell,
        valueInputOption="USER_ENTERED",
        body={"values": [[new_amount]]}
    ).execute()
    logger.info(f"Amount written to {cell}: {new_amount}")


# ---------------------------------------------------------------------------
# Note read / write
#
# Notes are stored as cell notes (the small triangle pop-up), NOT cell values.
# Reading requires spreadsheets.get() with a field mask.
# Writing requires batchUpdate with updateCells + fields="note".
# Mixing up these two APIs was a common source of bugs in the old bot.
# ---------------------------------------------------------------------------

def _read_existing_note(service, tab_name: str, row: int) -> str:
    """
    Read the existing cell note from column C of the given row.
    Returns empty string if no note exists.
    Uses spreadsheets.get() with a fields mask — the only reliable way.
    """
    cell_range = f"'{tab_name}'!C{row}"
    try:
        result = service.spreadsheets().get(
            spreadsheetId=SPREADSHEET_ID,
            ranges=[cell_range],
            fields="sheets(data(rowData(values(note))))"
        ).execute()

        # Navigate the deeply nested response carefully — any level can be absent.
        note = (
            result
            .get("sheets", [{}])[0]
            .get("data", [{}])[0]
            .get("rowData", [{}])[0]
            .get("values", [{}])[0]
            .get("note", "")
        )
        return note or ""

    except Exception as e:
        logger.warning(f"Could not read existing note from {cell_range}: {e}. Treating as empty.")
        return ""


def _write_note(service, tab_name: str, row: int, sheet_id: int, full_note: str) -> None:
    """
    Write (replace) the cell note on column C of the given row.
    Must use batchUpdate with updateCells — values().update() cannot touch notes.
    The fields="note" mask ensures we ONLY touch the note, nothing else in the cell.
    """
    service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={
            "requests": [{
                "updateCells": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row - 1,   # 0-indexed, inclusive
                        "endRowIndex":   row,        # 0-indexed, exclusive
                        "startColumnIndex": 2,       # Column C
                        "endColumnIndex":   3,
                    },
                    "rows": [{"values": [{"note": full_note}]}],
                    "fields": "note"                 # ONLY update the note field
                }
            }]
        }
    ).execute()
    logger.info(f"Note written to '{tab_name}'!C{row}")


def _build_note_line(original_text: str, timestamp: str) -> str:
    """
    Build a single note entry line.
    Format:  YYYY-MM-DD HH:MM  <full message as typed by user>
    Timestamp is passed in so the caller can store it for /delete matching.
    """
    return f"{timestamp}  {original_text}"


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def log_expense(
    category: str,
    amount: float,
    original_text: str,
    dt: datetime = None,
) -> LogResult:
    """
    Log an expense to Google Sheets.

    Args:
        category:      Canonical category key (must match column A in sheet).
        amount:        Expense amount (can be negative for refunds).
        original_text: The full message the user typed — stored as-is in the note.
        dt:            Which month to target (defaults to today).

    Returns:
        LogResult with success status and details.
    """
    if dt is None:
        dt = datetime.now()

    service = _build_service()

    # 1. Find the right month tab
    tab_info = find_tab_for_month(service, dt)
    if tab_info is None:
        return LogResult(
            success=False,
            category=category,
            amount_added=amount,
            new_total=0,
            tab_name="",
            row=0,
            timestamp="",
            message=f"No sheet tab found for {dt.strftime('%B %Y')}. Please create it first.",
        )
    tab_name, sheet_id = tab_info

    # 2. Find the category row
    row = find_category_row(service, tab_name, category)
    if row is None:
        return LogResult(
            success=False,
            category=category,
            amount_added=amount,
            new_total=0,
            tab_name=tab_name,
            row=0,
            timestamp="",
            message=f"Category '{category}' not found in tab '{tab_name}'.",
        )

    # 3. Read current amount, compute new total, write it back
    current_amount = _read_current_amount(service, tab_name, row)
    new_total = current_amount + amount
    _write_amount(service, tab_name, row, new_total)

    # 4. Build the note line with a shared timestamp, then append it
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    existing_note = _read_existing_note(service, tab_name, row)
    new_line = _build_note_line(original_text, timestamp)
    full_note = (existing_note + "\n" + new_line).strip()
    _write_note(service, tab_name, row, sheet_id, full_note)

    return LogResult(
        success=True,
        category=category,
        amount_added=amount,
        new_total=new_total,
        tab_name=tab_name,
        row=row,
        timestamp=timestamp,
        message=f"✅ Added ₪{amount:g} to '{category}'. New total: ₪{new_total:g}",
    )
