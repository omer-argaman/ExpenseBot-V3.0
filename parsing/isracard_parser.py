"""
parsing/isracard_parser.py — Parse Isracard transaction notifications.

Accepts the plain-text body of an Isracard transaction message (SMS or the
plain-text portion of an email) and extracts the structured transaction
details. Returns a `Transaction` dataclass; the caller (transaction_handler)
decides what to do with it.

Two templates are observed in the wild:

  Format A — prepaid card (with balance line):
    שלום, בכרטיסך המסתיים בספרות 0347, אושרה עסקה בסך 224.85 ש"ח
    בבילבונג בורד שופ, בתאריך 02/05/2026, בשעה 19:12.
    יתרתך בכרטיס נכון לעכשיו היא 224.30. ...

  Format B — credit card (no balance line; date+time may be partial):
    שלום,
    בכרטיסך 4881 אושרה עסקה ב-05/05 בסך 1000.00 ש"ח בעמותת עטלף.
    למידע נוסף ...

Plus an older variant of Format B that uses `המסתיים ב- 4881,` instead of
just `בכרטיסך 4881`.

Rather than maintain two giant regexes, we extract each field with its own
small anchor. That way wording drift in one part of the message doesn't break
parsing of the other fields, and a future Format C is likely to "just work"
as long as the same Hebrew anchors are present.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Optional

ISSUER = "isracard"

Kind = Literal["charge", "refund", "declined", "unknown"]


@dataclass(frozen=True)
class Transaction:
    """Structured result of parsing one Isracard message."""
    kind: Kind
    last4: Optional[str]
    amount: Optional[float]            # absolute value in `currency`
    currency: Optional[str]            # "ILS", "USD", "EUR", etc.
    merchant_raw: Optional[str]        # as-it-appears, with original casing
    merchant_normalized: Optional[str] # for stable lookup against merchant_map
    txn_date: Optional[date]
    txn_time: Optional[str]            # "HH:MM", or None
    issuer: str = ISSUER
    raw_body: str = ""

    @property
    def is_loggable(self) -> bool:
        """True only if we have the bare minimum needed to write a row."""
        return (
            self.kind in ("charge", "refund")
            and self.amount is not None
            and self.amount > 0
            and self.merchant_normalized
        )


# ---------------------------------------------------------------------------
# Field-level patterns
# ---------------------------------------------------------------------------

# `last4`: try the most specific phrasing first, then progressively looser.
_LAST4_PATTERNS = (
    re.compile(r"המסתיים\s+בספרות\s*(\d{4})"),
    re.compile(r"המסתיים\s+ב-?\s*(\d{4})"),
    re.compile(r"בכרטיסך\s+(\d{4})\b"),
)

# Amount + currency token immediately after `בסך`. Currency is captured raw
# and normalized later (Hebrew shekels render as ש"ח, ש״ח, שח, or even ₪).
_AMOUNT_PATTERN = re.compile(r"בסך\s+([\d,]+(?:\.\d+)?)\s+(\S+)")

# Merchant: starts after `בסך AMOUNT CURRENCY ב`, ends at one of:
#   - `, בתאריך`     (Format A)
#   - newline + `למידע`  (Format B; the next line is always `למידע נוסף ...`)
#   - end of string
# We capture greedily within a single line (no DOTALL) so the merchant cannot
# accidentally swallow following lines.
_MERCHANT_PATTERN = re.compile(
    r"בסך\s+[\d,]+(?:\.\d+)?\s+\S+\s+ב(.+?)(?=,\s*בתאריך|\s*\n\s*למידע|$)"
)

# Date variants: full (Format A) and short (Format B, no year).
_DATE_FULL_PATTERN = re.compile(r"בתאריך\s+(\d{2}/\d{2}/\d{4})")
_DATE_SHORT_PATTERN = re.compile(r"אושרה\s+עסקה\s+ב-(\d{2}/\d{2})\b")
_TIME_PATTERN = re.compile(r"בשעה\s+(\d{2}:\d{2})")

_DECLINED_TOKENS = ("נדחתה", "לא אושרה", "סורבה", "לא בוצעה")
_REFUND_TOKENS = ("זיכוי", "בוטלה", "הוחזר", "החזר")
_CHARGE_TOKEN = "אושרה עסקה"
_ISSUER_TOKEN = "ישראכרט"

_SHEKEL_TOKENS = {'ש"ח', 'ש״ח', "שח", "ש'ח", "₪", "ILS"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def looks_like_isracard(text: str) -> bool:
    """Cheap pre-filter the dispatcher uses to route messages to this parser."""
    return bool(text) and _ISSUER_TOKEN in text


def looks_like_transaction_notification(text: str) -> bool:
    """
    True when the SMS is likely a real card event (approved charge, decline,
    or refund), not generic Isracard marketing / reminders that only mention
    the brand name.

    The iOS Shortcut trigger often matches any SMS containing `ישראכרט`;
    this gate drops promos and info messages **before** any AI or Telegram
    noise. Requires the issuer token plus at least one transaction anchor:
      - `אושרה עסקה` — approved purchase (most common)
      - decline tokens — `נדחתה`, `לא אושרה`, …
      - refund tokens — `זיכוי`, `בוטלה`, …
    """
    if not looks_like_isracard(text):
        return False
    if _CHARGE_TOKEN in text:
        return True
    for t in _DECLINED_TOKENS:
        if t in text:
            return True
    for t in _REFUND_TOKENS:
        if t in text:
            return True
    return False


def parse(text: str, now: Optional[datetime] = None) -> Transaction:
    """
    Parse the body of an Isracard message into a `Transaction`.

    `now` is injected for deterministic year-rollover handling in tests; in
    production it defaults to `datetime.now()`.
    """
    raw = text or ""
    if now is None:
        now = datetime.now()

    if not looks_like_isracard(raw):
        return _empty(raw, kind="unknown")

    kind = _detect_kind(raw)
    last4 = _find_last4(raw)
    amount, currency = _find_amount(raw)
    merchant_raw, merchant_normalized = _find_merchant(raw)
    txn_date = _find_date(raw, now)
    txn_time = _find_time(raw)

    return Transaction(
        kind=kind,
        last4=last4,
        amount=amount,
        currency=currency,
        merchant_raw=merchant_raw,
        merchant_normalized=merchant_normalized,
        txn_date=txn_date,
        txn_time=txn_time,
        raw_body=raw,
    )


def normalize_merchant(merchant: str) -> str:
    """
    Produce a stable lookup key from a raw merchant string.

    Steps:
      1. Trim and collapse whitespace.
      2. Drop a trailing single Hebrew letter (Isracard truncation marker, e.g.
         `יפו א` → `יפו`). Two-letter suffixes like `בע` are kept because they
         are usually part of the actual name (`בע"מ`, the Hebrew "Ltd").
      3. Drop a trailing single Latin letter that is not a real word.
      4. Uppercase Latin characters; leave Hebrew alone.

    The raw merchant is preserved separately for the sheet note column.
    """
    if not merchant:
        return ""
    s = merchant.strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s[א-ת]$", "", s)  # trailing single Hebrew letter (truncation)
    s = s.strip()
    s = "".join(ch.upper() if "a" <= ch <= "z" else ch for ch in s)
    return s


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _empty(raw: str, kind: Kind = "unknown") -> Transaction:
    return Transaction(
        kind=kind,
        last4=None,
        amount=None,
        currency=None,
        merchant_raw=None,
        merchant_normalized=None,
        txn_date=None,
        txn_time=None,
        raw_body=raw,
    )


def _detect_kind(text: str) -> Kind:
    for token in _DECLINED_TOKENS:
        if token in text:
            return "declined"
    for token in _REFUND_TOKENS:
        if token in text:
            return "refund"
    if _CHARGE_TOKEN in text:
        return "charge"
    return "unknown"


def _find_last4(text: str) -> Optional[str]:
    for pattern in _LAST4_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(1)
    return None


def _find_amount(text: str) -> tuple[Optional[float], Optional[str]]:
    m = _AMOUNT_PATTERN.search(text)
    if not m:
        return None, None
    amount_str = m.group(1).replace(",", "")
    currency_token = m.group(2)
    try:
        amount = float(amount_str)
    except ValueError:
        return None, None
    return amount, _normalize_currency(currency_token)


def _normalize_currency(token: str) -> str:
    cleaned = token.strip().rstrip(",.")
    if cleaned in _SHEKEL_TOKENS:
        return "ILS"
    if re.fullmatch(r"[A-Z]{3}", cleaned):
        return cleaned
    return cleaned  # unknown — preserve so caller can decide


def _find_merchant(text: str) -> tuple[Optional[str], Optional[str]]:
    m = _MERCHANT_PATTERN.search(text)
    if not m:
        return None, None
    raw = m.group(1)
    # Strip a trailing sentence-ender period (Format B); periods inside the
    # name (e.g. `סי.טי.אי`) are preserved.
    raw = re.sub(r"\s*\.\s*$", "", raw).strip()
    if not raw:
        return None, None
    return raw, normalize_merchant(raw)


def _find_date(text: str, now: datetime) -> Optional[date]:
    m = _DATE_FULL_PATTERN.search(text)
    if m:
        try:
            return datetime.strptime(m.group(1), "%d/%m/%Y").date()
        except ValueError:
            return None

    m = _DATE_SHORT_PATTERN.search(text)
    if m:
        day_month = m.group(1)
        try:
            d = datetime.strptime(f"{day_month}/{now.year}", "%d/%m/%Y").date()
        except ValueError:
            return None
        # Year-rollover sanity: if assuming the current year places the date
        # more than 60 days in the future, it must be from last year.
        if (d - now.date()).days > 60:
            try:
                d = d.replace(year=now.year - 1)
            except ValueError:
                pass
        return d
    return None


def _find_time(text: str) -> Optional[str]:
    m = _TIME_PATTERN.search(text)
    return m.group(1) if m else None
