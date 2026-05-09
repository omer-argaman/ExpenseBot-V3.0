"""
handlers/transaction_handler.py — End-to-end pipeline for one ingested
credit-card transaction (Isracard for now; dispatcher is ready for more
issuers).

Flow:
  1. Parse the body using the issuer-specific parser.
  2. Skip declines, negate refunds.
  3. For non-ILS amounts: convert to ILS via parsing/fx.py and route via
     Telegram so the user can correct the estimate against the statement.
  4. Try the merchant_map (exact + fuzzy):
       - exact hit  -> auto-log + brief Telegram notification
       - strong fuzzy -> auto-log + auto-learn an exact alias for next time
  5. Otherwise call ai_categorize_merchant. If confidence >= threshold,
     auto-log + auto-learn. Else send a Telegram inline-keyboard ask and
     stash a pending entry; the callback handler in handlers/callbacks.py
     finishes the job when the user taps a button.
  6. Dedupe via message_id and a (last4, amount, date, time) window so the
     same transaction can't double-log if it arrives twice (e.g. SMS + email
     during the transition period).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Optional

from config import HIGH_CONFIDENCE_THRESHOLD
from handlers.commands import append_to_history
from handlers.notifier import send_message
from handlers.ai_handler import ai_categorize_merchant_sync
from parsing import isracard_parser
from parsing.card_registry import lookup as lookup_card
from parsing.fx import convert as fx_convert
from parsing.merchant_map import (
    FUZZY_STRONG_THRESHOLD,
    learn as learn_merchant,
    lookup as lookup_merchant,
)
from sheets import log_expense

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dedupe — small in-memory cache. Two keys per transaction:
#   1. issuer message_id (when present) — Gmail / SMS adapter unique id
#   2. (last4, amount, date_iso, time_or_empty) — secondary fallback
# ---------------------------------------------------------------------------

_DEDUPE_MAX = 200
_DEDUPE_TTL_SECONDS = 24 * 60 * 60

_dedupe_lock = threading.Lock()
_dedupe: "OrderedDict[str, float]" = OrderedDict()


def _is_duplicate(*keys: str) -> bool:
    now = time.time()
    with _dedupe_lock:
        # Evict expired
        cutoff = now - _DEDUPE_TTL_SECONDS
        while _dedupe and next(iter(_dedupe.values())) < cutoff:
            _dedupe.popitem(last=False)
        for k in keys:
            if not k:
                continue
            if k in _dedupe:
                return True
        for k in keys:
            if not k:
                continue
            _dedupe[k] = now
            _dedupe.move_to_end(k)
        while len(_dedupe) > _DEDUPE_MAX:
            _dedupe.popitem(last=False)
    return False


# ---------------------------------------------------------------------------
# Pending Telegram asks — stored by short pending_id, fetched by the
# callback handler when the user taps a button.
# ---------------------------------------------------------------------------

@dataclass
class PendingAsk:
    pending_id: str
    merchant_normalized: str
    merchant_raw: str
    amount_ils: float           # already converted if originally foreign
    txn_date: Optional[datetime]
    sheet_note: str             # full string to write into the sheet's note column
    created_at: float
    candidates: list[str] = None  # populated when the ask message is sent
    message_id: Optional[int] = None  # Telegram message id of the ask, for editing later

    def __post_init__(self):
        if self.candidates is None:
            self.candidates = []


_pending_lock = threading.Lock()
_pending: dict[str, PendingAsk] = {}


def store_pending(ask: PendingAsk) -> None:
    with _pending_lock:
        _pending[ask.pending_id] = ask
        # Evict anything older than 24h to bound memory
        cutoff = time.time() - 24 * 60 * 60
        for pid in list(_pending.keys()):
            if _pending[pid].created_at < cutoff:
                _pending.pop(pid, None)


def pop_pending(pending_id: str) -> Optional[PendingAsk]:
    with _pending_lock:
        return _pending.pop(pending_id, None)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

@dataclass
class IngestResult:
    status: str          # "logged" | "asked" | "skipped" | "duplicate" | "ignored" | "error"
    detail: str = ""
    category: Optional[str] = None
    amount_ils: Optional[float] = None
    merchant: Optional[str] = None


def process_ingest(payload: dict[str, Any]) -> IngestResult:
    """
    Entry point for the Flask /ingest handler.

    `payload` shape:
      {
        "issuer":       "isracard",
        "subject":      <email subject or empty>,
        "body":         <plain-text body / SMS text>,
        "message_id":   <unique id from Gmail / Apps Script / Shortcut>,
        "received_at":  <ISO8601 timestamp string, optional>
      }
    """
    issuer = (payload.get("issuer") or "").strip().lower()
    body = (payload.get("body") or "").strip()
    message_id = (payload.get("message_id") or "").strip()

    if not body:
        return IngestResult(status="error", detail="empty body")

    if issuer and issuer != "isracard":
        return IngestResult(
            status="error",
            detail=f"unsupported issuer: {issuer!r}",
        )

    if not isracard_parser.looks_like_isracard(body):
        return IngestResult(
            status="error",
            detail="body does not look like an Isracard message",
        )

    # Marketing / reminders often contain `ישראכרט` but not `אושרה עסקה` or
    # any decline/refund anchor. Skip silently — no Telegram, no AI, no sheet.
    if not isracard_parser.looks_like_transaction_notification(body):
        logger.info(
            "Ignoring non-transaction Isracard SMS (no charge/decline/refund anchor): %s",
            body[:160].replace("\n", " "),
        )
        return IngestResult(
            status="ignored",
            detail="not_a_transaction_notification",
        )

    txn = isracard_parser.parse(body)

    # Defense-in-depth: manual tests / jokes / truncated forwards often contain
    # `ישראכרט` but never formed a charge sentence — parser yields kind=unknown.
    # Never spam Telegram for those; real charges always include אושרה עסקה
    # upstream (looks_like_transaction_notification) but this catches edge cases
    # (encoding, older deploys, odd Unicode).
    if txn.kind == "unknown":
        logger.info(
            "Ignoring message — parser saw no charge/decline/refund sentence: %s",
            body[:160].replace("\n", " "),
        )
        return IngestResult(
            status="ignored",
            detail="unknown_transaction_kind",
        )

    if txn.kind == "declined":
        logger.info("Skipping declined transaction: %s", txn.merchant_raw)
        return IngestResult(status="skipped", detail="declined")

    if not txn.is_loggable and txn.kind != "refund":
        # Could not parse enough to log. Forward raw body to the user so they
        # can either log manually or send us the format change.
        send_message(
            "Couldn't parse this Isracard message; please log it manually:\n\n"
            f"<code>{_html_escape(body[:500])}</code>"
        )
        return IngestResult(status="error", detail="parser could not extract fields")

    last4 = txn.last4 or ""
    amount_native = txn.amount or 0.0
    currency = txn.currency or "ILS"
    sign = -1 if txn.kind == "refund" else 1

    # Convert to ILS if needed
    if currency.upper() == "ILS":
        amount_ils = amount_native
        fx_note = ""
    else:
        rate = fx_convert(amount_native, currency, "ILS")
        if rate is None:
            send_message(
                f"Foreign-currency transaction I couldn't auto-convert:\n"
                f"<b>{_html_escape(txn.merchant_raw or '?')}</b>: "
                f"{amount_native:g} {currency}\n"
                f"Card ending {last4}. Please log manually."
            )
            return IngestResult(
                status="error",
                detail=f"FX conversion failed for {currency}",
            )
        amount_ils = rate
        fx_note = f" (orig {amount_native:g} {currency})"

    amount_ils_signed = sign * amount_ils

    # Dedupe AFTER parse so we get more keys
    date_iso = txn.txn_date.isoformat() if txn.txn_date else ""
    time_part = txn.txn_time or ""
    fallback_key = f"isracard:{last4}:{amount_native:.2f}:{currency}:{date_iso}:{time_part}"
    if _is_duplicate(message_id, fallback_key):
        logger.info("Duplicate transaction skipped: %s", fallback_key)
        return IngestResult(status="duplicate", detail=fallback_key)

    sheet_note = _build_sheet_note(
        txn=txn,
        amount_ils=amount_ils,
        currency=currency,
        sign=sign,
        fx_note=fx_note,
    )

    # 1. Merchant map first
    merchant_norm = txn.merchant_normalized or ""
    map_hit = lookup_merchant(merchant_norm)
    if map_hit and (
        map_hit.method == "exact"
        or (map_hit.method == "fuzzy" and map_hit.confidence * 100 >= FUZZY_STRONG_THRESHOLD)
    ):
        category = map_hit.category
        return _do_log(
            category=category,
            amount_ils_signed=amount_ils_signed,
            sheet_note=sheet_note,
            txn=txn,
            merchant_raw=txn.merchant_raw or "",
            merchant_norm=merchant_norm,
            fx_note=fx_note,
            currency=currency,
            amount_native=amount_native,
            sign=sign,
            map_origin=f"map ({map_hit.method}, {map_hit.confidence:.0%})",
        )

    # 2. AI categorization with card prior
    card = lookup_card(last4)
    fuzzy_hint = None
    if map_hit and map_hit.method == "fuzzy":
        fuzzy_hint = (map_hit.merchant_normalized, map_hit.category, map_hit.confidence)

    ai = ai_categorize_merchant_sync(
        merchant=merchant_norm,
        amount=amount_native,
        currency=currency,
        card_prior=list(card.category_prior) if card.has_prior else None,
        card_name=card.name or None,
        fuzzy_hint=fuzzy_hint,
    )

    category = ai.get("category")
    confidence = float(ai.get("confidence", 0))
    alternatives = list(ai.get("alternatives") or [])
    reason = ai.get("reason", "")

    if category and confidence >= HIGH_CONFIDENCE_THRESHOLD:
        # Auto-log and auto-learn
        result = _do_log(
            category=category,
            amount_ils_signed=amount_ils_signed,
            sheet_note=sheet_note,
            txn=txn,
            merchant_raw=txn.merchant_raw or "",
            merchant_norm=merchant_norm,
            fx_note=fx_note,
            currency=currency,
            amount_native=amount_native,
            sign=sign,
            map_origin=f"ai-auto ({confidence:.0%})",
        )
        learn_merchant(merchant_norm, category, source="auto")
        return result

    # 3. Ask the user
    return _ask_user(
        txn=txn,
        amount_ils=amount_ils,
        amount_native=amount_native,
        currency=currency,
        sign=sign,
        sheet_note=sheet_note,
        category_guess=category,
        alternatives=alternatives,
        confidence=confidence,
        reason=reason,
        card=card,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_sheet_note(
    txn: "isracard_parser.Transaction",
    amount_ils: float,
    currency: str,
    sign: int,
    fx_note: str,
) -> str:
    """Synthetic original_text for the sheet note column — preserves all SMS info."""
    parts = []
    if txn.last4:
        parts.append(f"[card {txn.last4}]")
    if sign < 0:
        parts.append("REFUND:")
    if txn.merchant_raw:
        parts.append(txn.merchant_raw)
    parts.append(f"{amount_ils:g} ILS{fx_note}")
    if txn.txn_date:
        suffix = f" {txn.txn_time}" if txn.txn_time else ""
        parts.append(f"on {txn.txn_date.isoformat()}{suffix}")
    return " ".join(parts)


def _do_log(
    category: str,
    amount_ils_signed: float,
    sheet_note: str,
    txn: "isracard_parser.Transaction",
    merchant_raw: str,
    merchant_norm: str,
    fx_note: str,
    currency: str,
    amount_native: float,
    sign: int,
    map_origin: str,
) -> IngestResult:
    log_dt = (
        datetime(txn.txn_date.year, txn.txn_date.month, txn.txn_date.day)
        if txn.txn_date else None
    )
    log_result = log_expense(
        category=category,
        amount=amount_ils_signed,
        original_text=sheet_note,
        dt=log_dt,
    )
    if not log_result.success:
        send_message(
            "Could not write to sheet: "
            f"<code>{_html_escape(log_result.message)}</code>\n"
            f"Transaction: <b>{_html_escape(merchant_raw)}</b> "
            f"{amount_native:g} {currency}"
        )
        return IngestResult(
            status="error",
            detail=log_result.message,
            category=category,
            amount_ils=amount_ils_signed,
            merchant=merchant_raw,
        )

    append_to_history(
        category=log_result.category,
        amount=log_result.amount_added,
        tab_name=log_result.tab_name,
        row=log_result.row,
        timestamp=log_result.timestamp,
        original_text=sheet_note,
    )

    # User notification
    notif_amount = abs(log_result.amount_added)
    arrow = "→"
    sign_label = "Refund" if sign < 0 else "Logged"
    fx_label = fx_note.strip() or ""
    if fx_label:
        fx_label = f" {fx_label}"
    send_message(
        f"<b>{sign_label}: {notif_amount:g} ILS{fx_label} {arrow} {_html_escape(category)}</b>\n"
        f"{_html_escape(merchant_raw)}  ({map_origin})\n"
        f"<i>Reply with /fix to recategorize.</i>"
    )

    return IngestResult(
        status="logged",
        detail=map_origin,
        category=category,
        amount_ils=amount_ils_signed,
        merchant=merchant_raw,
    )


def _ask_user(
    txn: "isracard_parser.Transaction",
    amount_ils: float,
    amount_native: float,
    currency: str,
    sign: int,
    sheet_note: str,
    category_guess: Optional[str],
    alternatives: list[str],
    confidence: float,
    reason: str,
    card,
) -> IngestResult:
    """Send a Telegram inline-keyboard prompt and stash a pending entry."""
    pending_id = uuid.uuid4().hex[:10]
    log_dt = (
        datetime(txn.txn_date.year, txn.txn_date.month, txn.txn_date.day)
        if txn.txn_date else None
    )
    ask = PendingAsk(
        pending_id=pending_id,
        merchant_normalized=txn.merchant_normalized or "",
        merchant_raw=txn.merchant_raw or "",
        amount_ils=sign * amount_ils,
        txn_date=log_dt,
        sheet_note=sheet_note,
        created_at=time.time(),
    )
    store_pending(ask)

    # Build candidate buttons: guess first, then alternatives, then card prior fillers
    candidates: list[str] = []
    if category_guess:
        candidates.append(category_guess)
    for alt in alternatives:
        if alt not in candidates:
            candidates.append(alt)
    for prior_cat in card.category_prior:
        if prior_cat not in candidates:
            candidates.append(prior_cat)
    candidates = candidates[:4]

    keyboard: list[list[dict]] = []
    for cat in candidates:
        keyboard.append([{
            "text": cat,
            "callback_data": f"txn:{pending_id}:cat:{_short_index_of(candidates, cat)}",
        }])
    # Fallback row to expand to all categories
    keyboard.append([{
        "text": "Pick another...",
        "callback_data": f"txn:{pending_id}:more",
    }])

    fx_label = ""
    if currency.upper() != "ILS":
        fx_label = f"\n<i>(estimated from {amount_native:g} {currency})</i>"
    sign_emoji = "↩️" if sign < 0 else "💳"
    confidence_pct = int(round(confidence * 100))
    guess_line = (
        f"\nBest guess: <b>{_html_escape(category_guess)}</b> ({confidence_pct}%)"
        if category_guess else "\nNo confident guess."
    )
    reason_line = f"\n<i>{_html_escape(reason)}</i>" if reason else ""

    text = (
        f"{sign_emoji} <b>{_html_escape(txn.merchant_raw or 'Unknown merchant')}</b>\n"
        f"{abs(ask.amount_ils):g} ILS"
        + (f" • card {txn.last4}" if txn.last4 else "")
        + fx_label
        + guess_line
        + reason_line
        + "\n\nWhich category?"
    )

    ask.candidates = candidates

    msg_id = send_message(text, inline_keyboard=keyboard)
    if msg_id is None:
        # If we can't reach the user, fall back to logging under "Other (Daily)"
        # rather than dropping the transaction silently.
        logger.error("Failed to send ask message; falling back to Other (Daily)")
        return _do_log(
            category="Other (Daily)",
            amount_ils_signed=sign * amount_ils,
            sheet_note=sheet_note + " [auto-fallback: notify failed]",
            txn=txn,
            merchant_raw=txn.merchant_raw or "",
            merchant_norm=txn.merchant_normalized or "",
            fx_note="",
            currency=currency,
            amount_native=amount_native,
            sign=sign,
            map_origin="fallback",
        )

    ask.message_id = msg_id
    return IngestResult(
        status="asked",
        detail=f"pending_id={pending_id}",
        category=category_guess,
        amount_ils=sign * amount_ils,
        merchant=txn.merchant_raw,
    )


def _short_index_of(items: list[str], value: str) -> int:
    try:
        return items.index(value)
    except ValueError:
        return -1


def _html_escape(s: str | None) -> str:
    if not s:
        return ""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )
