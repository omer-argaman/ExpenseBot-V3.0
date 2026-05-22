"""
parsing/merchant_map.py — In-memory cache over the sheet-backed merchant map.

Reads the `Merchants` tab from the Google Sheet once at startup, then keeps
a process-local copy. Lookups are exact (case-insensitive) first, then
fuzzy via fuzzywuzzy. All writes go through `learn()`, which updates both
the in-memory cache and the underlying sheet so the bot can survive a
restart without losing state.

Two confidence regimes:

  exact match            → 1.00
  fuzzy match >= 88      → score / 100  (high enough to act without asking)
  fuzzy match in 70..87  → score / 100  (returned but caller should treat as a hint, not a decision)
  no match               → None
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Optional

from fuzzywuzzy import process as fuzz_process

from sheets import read_merchant_map, upsert_merchant_mapping

logger = logging.getLogger(__name__)

EXACT_CONFIDENCE = 1.0
FUZZY_STRONG_THRESHOLD = 88
FUZZY_HINT_THRESHOLD = 70


@dataclass(frozen=True)
class MerchantMatch:
    merchant_normalized: str
    category: str
    source: str           # "user" or "auto"
    confidence: float     # 0.0 - 1.0
    method: str           # "exact" or "fuzzy"


_lock = threading.Lock()
_cache: dict[str, dict] | None = None  # key: lowercased merchant_normalized


def _ensure_loaded() -> dict[str, dict]:
    global _cache
    with _lock:
        if _cache is None:
            try:
                rows = read_merchant_map()
            except Exception as exc:
                logger.warning("Could not read Merchants tab; starting empty. (%s)", exc)
                rows = []
            _cache = {row["merchant_normalized"].lower(): row for row in rows}
            logger.info("Merchant map loaded: %d entr%s",
                        len(_cache), "y" if len(_cache) == 1 else "ies")
        return _cache


def lookup(merchant_normalized: str) -> Optional[MerchantMatch]:
    """
    Find the best match for `merchant_normalized` in the cached map.

    Returns None if there is not even a weak fuzzy match. Returns a
    MerchantMatch with `method="exact"` for case-insensitive exact hits and
    `method="fuzzy"` for everything else above FUZZY_HINT_THRESHOLD. The
    caller decides whether `confidence` is high enough to act on.
    """
    if not merchant_normalized:
        return None
    cache = _ensure_loaded()
    key = merchant_normalized.lower()

    # 1. Exact (case-insensitive) match
    hit = cache.get(key)
    if hit:
        return MerchantMatch(
            merchant_normalized=hit["merchant_normalized"],
            category=hit["category"],
            source=hit["source"],
            confidence=EXACT_CONFIDENCE,
            method="exact",
        )

    # 2. Fuzzy fallback — only meaningful if we already have some entries
    if not cache:
        return None
    result = fuzz_process.extractOne(key, cache.keys())
    if not result:
        return None
    matched_key, score = result
    if score < FUZZY_HINT_THRESHOLD:
        return None
    hit = cache[matched_key]
    return MerchantMatch(
        merchant_normalized=hit["merchant_normalized"],
        category=hit["category"],
        source=hit["source"],
        confidence=score / 100.0,
        method="fuzzy",
    )


def learn(merchant_normalized: str, category: str, source: str = "user") -> None:
    """
    Persist a new mapping (or refresh an existing one) to the sheet AND the
    in-memory cache.

    Called from two places:
      - the `txn:` callback handler when the user taps a category button,
      - the transaction handler when the AI's confidence cleared the
        HIGH_CONFIDENCE_THRESHOLD (source='auto').
    """
    if not merchant_normalized or not category:
        return
    try:
        upsert_merchant_mapping(merchant_normalized, category, source=source)
    except Exception as exc:
        logger.warning("Could not write Merchants tab: %s", exc)
        # Still update the local cache so the next ingest of the same
        # merchant won't ask again in this process.

    cache = _ensure_loaded()
    key = merchant_normalized.lower()
    existing = cache.get(key, {})
    new_source = "user" if source == "user" else (existing.get("source") or source)
    cache[key] = {
        "merchant_normalized": merchant_normalized,
        "category": category,
        "source": new_source,
        "first_seen": existing.get("first_seen", ""),
        "last_seen": "",
        "hits": existing.get("hits", 0) + 1,
    }


def preload() -> int:
    """Eagerly load the merchant map from Sheets. Returns entry count."""
    return len(_ensure_loaded())


def reset_cache_for_tests() -> None:
    """Drop the in-memory cache so the next lookup re-reads from Sheets."""
    global _cache
    with _lock:
        _cache = None
