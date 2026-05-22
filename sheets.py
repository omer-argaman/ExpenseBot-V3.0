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
class TabLookupFailure:
    """
    Structured context for a failed month-tab lookup.

    Handed to the AI error-explanation path so it can reason about whether
    the user has a typo (e.g. '0246' instead of '0426'), a whitespace issue
    that our normalized matcher somehow still missed, or simply hasn't
    created a sheet for this month yet. We pass the FULL tab list rather
    than pre-filtering with a similarity score — an LLM is much better at
    deciding whether '0246' is a typo of '0426' or a legitimate sheet for
    a different month, because it understands MMYY date semantics.
    """
    target_month: str           # e.g. "April 2026"
    tried_formats: list[str]    # candidate names we attempted
    existing_tabs: list[str]    # ALL current tab titles, original-cased


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
    failure: Optional[TabLookupFailure] = None  # set when the failure was a missing tab


# ---------------------------------------------------------------------------
# API connection — cached singleton to avoid per-request memory growth
# ---------------------------------------------------------------------------

_service_cache = None
_tabs_cache: tuple[float, dict[str, tuple[str, int]]] | None = None
_TABS_CACHE_TTL_SECONDS = 300
_MAX_NOTE_CHARS = 12_000  # cap cell notes to limit read/write memory per log

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

def invalidate_spreadsheet_tabs_cache() -> None:
    """Clear cached tab metadata (e.g. after adding a new sheet)."""
    global _tabs_cache
    _tabs_cache = None


def get_spreadsheet_tabs(service) -> dict[str, tuple[str, int]]:
    """
    Fetch all sheet tab titles once and return a lookup dict.
    Returns:  lowercase_title -> (original_title, sheet_id)
    Use this when you need to resolve multiple months — it avoids a metadata
    API call for every individual month lookup.
    """
    global _tabs_cache
    import time as _time
    from _debug_trace import dbg  # noqa: PLC0415

    now = _time.monotonic()
    if _tabs_cache is not None:
        cached_at, tabs = _tabs_cache
        if now - cached_at < _TABS_CACHE_TTL_SECONDS:
            dbg("H2", "sheets.get_spreadsheet_tabs", "cache_hit", {"age_s": round(now - cached_at, 1)})
            return tabs

    dbg("H2", "sheets.get_spreadsheet_tabs", "cache_miss_fetch")
    metadata = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    tabs = {
        s["properties"]["title"].lower(): (
            s["properties"]["title"],
            s["properties"]["sheetId"],
        )
        for s in metadata.get("sheets", [])
    }
    _tabs_cache = (now, tabs)
    dbg("H2", "sheets.get_spreadsheet_tabs", "cache_stored", {"tab_count": len(tabs)})
    return tabs


def _normalize(s: str) -> str:
    """
    Strip whitespace and non-alphanumerics, lowercase everything.

    Crucially, digits are preserved verbatim — so '0426' and '0246' normalize
    to different strings and can NEVER collide. That is what makes this safe
    to auto-accept: any match at this layer is guaranteed to represent the
    same month as the target, just written with different whitespace or
    punctuation (' 0426', '04 26', '04-26', '04.26', 'Apr.2026', etc.).
    """
    return re.sub(r"[^a-z0-9]", "", s.lower())


def find_tab_in_tabs(
    existing_tabs: dict[str, tuple[str, int]],
    dt: datetime,
) -> Optional[tuple[str, int]]:
    """
    Find the tab for `dt` using a pre-fetched tabs dict (no API call).
    Returns (tab_name, sheet_id) or None.

    Two-pass matching, safest first:
      1. Exact lowercased match (current fastest-path behavior).
      2. Normalized match — strips whitespace and punctuation only. Safe
         because the digit content is preserved, so a different month's
         tab can never be mistaken for the target.
    Typo matching (e.g. '0246' vs '0426') is intentionally NOT performed
    here — that ambiguity is surfaced to the user via the AI error path.
    """
    candidates = _candidate_tab_names(dt)

    for candidate in candidates:
        match = existing_tabs.get(candidate.lower())
        if match:
            logger.info(f"Matched tab '{match[0]}' for {dt.strftime('%B %Y')} (exact)")
            return match

    normalized_existing = {
        _normalize(lower_title): value
        for lower_title, value in existing_tabs.items()
    }
    for candidate in candidates:
        match = normalized_existing.get(_normalize(candidate))
        if match:
            logger.info(
                f"Matched tab '{match[0]}' for {dt.strftime('%B %Y')} "
                f"(normalized — whitespace/punctuation variance)"
            )
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


def describe_tab_failure(
    existing_tabs: dict[str, tuple[str, int]],
    dt: datetime,
) -> TabLookupFailure:
    """
    Build a TabLookupFailure with the context needed by the AI error path.
    Call this ONLY when find_tab_in_tabs() has already returned None.
    """
    return TabLookupFailure(
        target_month=dt.strftime("%B %Y"),
        tried_formats=_candidate_tab_names(dt),
        existing_tabs=[original for (original, _sid) in existing_tabs.values()],
    )


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

    from _debug_trace import dbg  # noqa: PLC0415

    dbg("H2", "sheets.log_expense", "enter", {"category": category, "amount": amount})
    service = _build_service()

    # 1. Find the right month tab — fetch tabs once so we can build a rich
    #    failure object without a second metadata call.
    existing_tabs = get_spreadsheet_tabs(service)
    tab_info = find_tab_in_tabs(existing_tabs, dt)
    if tab_info is None:
        logger.warning(
            f"No tab found for {dt.strftime('%B %Y')}. "
            f"Tried: {_candidate_tab_names(dt)}"
        )
        return LogResult(
            success=False,
            category=category,
            amount_added=amount,
            new_total=0,
            tab_name="",
            row=0,
            timestamp="",
            message=f"No sheet tab found for {dt.strftime('%B %Y')}.",
            failure=describe_tab_failure(existing_tabs, dt),
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
    from _debug_trace import dbg  # noqa: PLC0415

    existing_note = _read_existing_note(service, tab_name, row)
    dbg("H3", "sheets.log_expense", "note_read", {
        "existing_note_len": len(existing_note),
        "category": category,
    })
    new_line = _build_note_line(original_text, timestamp)
    full_note = (existing_note + "\n" + new_line).strip()
    if len(full_note) > _MAX_NOTE_CHARS:
        lines = full_note.split("\n")
        full_note = "\n".join(lines[-50:]).strip()
        dbg("H3", "sheets.log_expense", "note_truncated", {"kept_lines": 50})
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
    dbg("H2", "sheets.log_expense", "exit", {"success": True, "tab": tab_name})


# ---------------------------------------------------------------------------
# Merchants tab — sheet-backed merchant -> category map.
#
# Persisted in a hidden tab so it survives Render restarts (free disk is
# ephemeral) and can be edited by hand. The tab is created lazily on first
# write. Header layout:
#
#   merchant_normalized | category | source | first_seen | last_seen | hits
#
# - source = "user" (set by an inline-button tap) or "auto" (saved by the
#   AI when its confidence cleared the threshold).
# - hits is incremented every time a transaction matches this row.
# ---------------------------------------------------------------------------

MERCHANTS_TAB_NAME = "Merchants"
_MERCHANTS_HEADERS = [
    "merchant_normalized",
    "category",
    "source",
    "first_seen",
    "last_seen",
    "hits",
]


def _ensure_merchants_tab(service) -> int:
    """
    Return the sheetId for the Merchants tab, creating it on demand.

    Cached locally so we only call the metadata API once per process.
    """
    cached = getattr(_ensure_merchants_tab, "_cached_sheet_id", None)
    if cached is not None:
        return cached

    metadata = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    for s in metadata.get("sheets", []):
        if s["properties"]["title"] == MERCHANTS_TAB_NAME:
            sheet_id = s["properties"]["sheetId"]
            _ensure_merchants_tab._cached_sheet_id = sheet_id  # type: ignore[attr-defined]
            return sheet_id

    add_resp = service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={
            "requests": [
                {
                    "addSheet": {
                        "properties": {
                            "title": MERCHANTS_TAB_NAME,
                            "hidden": True,
                            "gridProperties": {"rowCount": 1000, "columnCount": 6},
                        }
                    }
                }
            ]
        },
    ).execute()
    sheet_id = add_resp["replies"][0]["addSheet"]["properties"]["sheetId"]

    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{MERCHANTS_TAB_NAME}'!A1:F1",
        valueInputOption="RAW",
        body={"values": [_MERCHANTS_HEADERS]},
    ).execute()
    logger.info(f"Created hidden tab '{MERCHANTS_TAB_NAME}' for merchant map")

    _ensure_merchants_tab._cached_sheet_id = sheet_id  # type: ignore[attr-defined]
    return sheet_id


def read_merchant_map() -> list[dict]:
    """
    Return all merchant->category rows as a list of dicts.

    Returns [] if the tab doesn't exist yet (we don't auto-create on read,
    only on first write — keeps reads cheap and side-effect-free).
    """
    service = _build_service()
    tabs = get_spreadsheet_tabs(service)
    if MERCHANTS_TAB_NAME.lower() not in tabs:
        return []

    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{MERCHANTS_TAB_NAME}'!A2:F",
    ).execute()
    rows = result.get("values", [])

    out: list[dict] = []
    for r in rows:
        # Pad to the full width so partial rows don't IndexError
        r = (r + [""] * len(_MERCHANTS_HEADERS))[: len(_MERCHANTS_HEADERS)]
        merchant, category, source, first_seen, last_seen, hits = r
        if not merchant or not category:
            continue
        try:
            hits_n = int(hits) if hits else 0
        except ValueError:
            hits_n = 0
        out.append({
            "merchant_normalized": merchant.strip(),
            "category": category.strip(),
            "source": source.strip() or "auto",
            "first_seen": first_seen.strip(),
            "last_seen": last_seen.strip(),
            "hits": hits_n,
        })
    return out


def upsert_merchant_mapping(
    merchant_normalized: str,
    category: str,
    source: str = "user",
) -> None:
    """
    Insert or update one merchant->category mapping.

    On a hit: bumps `hits` and refreshes `last_seen`. If `source` upgrades
    from 'auto' to 'user' (i.e. the user just confirmed an AI guess), the
    new source overwrites; we never downgrade 'user' back to 'auto'.
    """
    if not merchant_normalized or not category:
        return

    service = _build_service()
    _ensure_merchants_tab(service)
    today = datetime.now().strftime("%Y-%m-%d")

    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{MERCHANTS_TAB_NAME}'!A2:F",
    ).execute()
    rows = result.get("values", [])

    # Find existing row (1-based: header is row 1, first data row is row 2)
    existing_row_index: Optional[int] = None
    existing: list[str] = []
    for i, r in enumerate(rows):
        r_padded = (r + [""] * len(_MERCHANTS_HEADERS))[: len(_MERCHANTS_HEADERS)]
        if r_padded[0].strip().lower() == merchant_normalized.lower():
            existing_row_index = i + 2  # +2 because rows[] starts at sheet row 2
            existing = r_padded
            break

    if existing_row_index is None:
        new_row = [merchant_normalized, category, source, today, today, "1"]
        service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{MERCHANTS_TAB_NAME}'!A:F",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [new_row]},
        ).execute()
        logger.info(f"Merchant map: added {merchant_normalized!r} -> {category!r} ({source})")
        return

    new_category = category
    new_source = existing[2].strip() or source
    if source == "user":
        new_source = "user"  # explicit user confirmation always wins
    new_first_seen = existing[3].strip() or today
    try:
        new_hits = int(existing[5]) + 1 if existing[5] else 1
    except ValueError:
        new_hits = 1

    updated_row = [
        merchant_normalized,
        new_category,
        new_source,
        new_first_seen,
        today,
        str(new_hits),
    ]
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{MERCHANTS_TAB_NAME}'!A{existing_row_index}:F{existing_row_index}",
        valueInputOption="RAW",
        body={"values": [updated_row]},
    ).execute()
    logger.info(
        f"Merchant map: updated {merchant_normalized!r} -> {new_category!r} "
        f"(source={new_source}, hits={new_hits})"
    )


# ---------------------------------------------------------------------------
# Pending tab — sheet-backed Telegram category asks (survive Render restarts)
#
#   pending_id | merchant_normalized | merchant_raw | amount_ils |
#   txn_date_iso | sheet_note | candidates_json | created_at
# ---------------------------------------------------------------------------

PENDING_TAB_NAME = "Pending"
_PENDING_HEADERS = [
    "pending_id",
    "merchant_normalized",
    "merchant_raw",
    "amount_ils",
    "txn_date_iso",
    "sheet_note",
    "candidates_json",
    "created_at",
]


def _ensure_pending_tab(service) -> int:
    cached = getattr(_ensure_pending_tab, "_cached_sheet_id", None)
    if cached is not None:
        return cached

    metadata = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    for s in metadata.get("sheets", []):
        if s["properties"]["title"] == PENDING_TAB_NAME:
            sheet_id = s["properties"]["sheetId"]
            _ensure_pending_tab._cached_sheet_id = sheet_id  # type: ignore[attr-defined]
            return sheet_id

    add_resp = service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={
            "requests": [
                {
                    "addSheet": {
                        "properties": {
                            "title": PENDING_TAB_NAME,
                            "hidden": True,
                            "gridProperties": {"rowCount": 500, "columnCount": 8},
                        }
                    }
                }
            ]
        },
    ).execute()
    sheet_id = add_resp["replies"][0]["addSheet"]["properties"]["sheetId"]

    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{PENDING_TAB_NAME}'!A1:H1",
        valueInputOption="RAW",
        body={"values": [_PENDING_HEADERS]},
    ).execute()
    logger.info(f"Created hidden tab '{PENDING_TAB_NAME}' for pending asks")

    _ensure_pending_tab._cached_sheet_id = sheet_id  # type: ignore[attr-defined]
    return sheet_id


def read_pending_asks() -> list[dict]:
    """Return all pending-ask rows. [] if tab missing."""
    service = _build_service()
    tabs = get_spreadsheet_tabs(service)
    if PENDING_TAB_NAME.lower() not in tabs:
        return []

    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{PENDING_TAB_NAME}'!A2:H",
    ).execute()
    rows = result.get("values", [])

    out: list[dict] = []
    for r in rows:
        r = (r + [""] * len(_PENDING_HEADERS))[: len(_PENDING_HEADERS)]
        (
            pending_id,
            merchant_normalized,
            merchant_raw,
            amount_ils,
            txn_date_iso,
            sheet_note,
            candidates_json,
            created_at,
        ) = r
        if not pending_id:
            continue
        try:
            amount = float(amount_ils) if amount_ils else 0.0
        except ValueError:
            amount = 0.0
        try:
            created = float(created_at) if created_at else 0.0
        except ValueError:
            created = 0.0
        out.append({
            "pending_id": pending_id.strip(),
            "merchant_normalized": merchant_normalized.strip(),
            "merchant_raw": merchant_raw.strip(),
            "amount_ils": amount,
            "txn_date_iso": txn_date_iso.strip(),
            "sheet_note": sheet_note.strip(),
            "candidates_json": candidates_json.strip(),
            "created_at": created,
        })
    return out


def upsert_pending_ask(row: dict) -> None:
    """Insert or update one pending ask row."""
    pending_id = (row.get("pending_id") or "").strip()
    if not pending_id:
        return

    service = _build_service()
    _ensure_pending_tab(service)

    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{PENDING_TAB_NAME}'!A2:H",
    ).execute()
    rows = result.get("values", [])

    sheet_row = [
        pending_id,
        row.get("merchant_normalized", ""),
        row.get("merchant_raw", ""),
        str(row.get("amount_ils", 0)),
        row.get("txn_date_iso", ""),
        row.get("sheet_note", ""),
        row.get("candidates_json", "[]"),
        str(row.get("created_at", 0)),
    ]

    existing_row_index: Optional[int] = None
    for i, r in enumerate(rows):
        r_padded = (r + [""] * len(_PENDING_HEADERS))[: len(_PENDING_HEADERS)]
        if r_padded[0].strip() == pending_id:
            existing_row_index = i + 2
            break

    if existing_row_index is None:
        service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{PENDING_TAB_NAME}'!A:H",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [sheet_row]},
        ).execute()
    else:
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{PENDING_TAB_NAME}'!A{existing_row_index}:H{existing_row_index}",
            valueInputOption="RAW",
            body={"values": [sheet_row]},
        ).execute()


def delete_pending_ask(pending_id: str) -> None:
    """Remove a pending ask row by pending_id."""
    if not pending_id:
        return

    service = _build_service()
    tabs = get_spreadsheet_tabs(service)
    tab_info = tabs.get(PENDING_TAB_NAME.lower())
    if not tab_info:
        return
    _, sheet_id = tab_info

    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{PENDING_TAB_NAME}'!A2:A",
    ).execute()
    rows = result.get("values", [])

    for i, r in enumerate(rows):
        if r and r[0].strip() == pending_id:
            row_index = i + 1  # 0-based within data rows; sheet row = i+2
            service.spreadsheets().batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body={
                    "requests": [
                        {
                            "deleteDimension": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "dimension": "ROWS",
                                    "startIndex": row_index,
                                    "endIndex": row_index + 1,
                                }
                            }
                        }
                    ]
                },
            ).execute()
            return
