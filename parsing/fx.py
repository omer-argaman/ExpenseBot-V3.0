"""
parsing/fx.py — Tiny FX-rate cache for foreign-currency transactions.

Most credit-card SMSes are in ILS. Occasionally Isracard sends one in USD
(or another currency, e.g. for an online subscription). The sheet is
denominated in ILS, so we estimate the ILS amount at the time of swipe
using a free, no-auth API and store the original currency + amount in
the note column for later reconciliation.

API: https://api.frankfurter.dev (no key, public, ECB rates).
The same `requests` library is already pulled in for Telegram messaging.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_TTL_SECONDS = 6 * 60 * 60  # 6 hours
_TIMEOUT = 5

_lock = threading.Lock()
_cache: dict[tuple[str, str], tuple[float, float]] = {}  # (from, to) -> (rate, fetched_at)


def get_rate(currency_from: str, currency_to: str = "ILS") -> Optional[float]:
    """
    Return how many `currency_to` units one `currency_from` unit is worth.
    None on any failure — the caller is expected to fall back gracefully
    (e.g. log without conversion and explicitly notify the user).
    """
    if not currency_from or not currency_to:
        return None
    if currency_from.upper() == currency_to.upper():
        return 1.0

    key = (currency_from.upper(), currency_to.upper())
    now = time.time()
    with _lock:
        cached = _cache.get(key)
        if cached and (now - cached[1]) < _TTL_SECONDS:
            return cached[0]

    url = "https://api.frankfurter.dev/v1/latest"
    try:
        resp = requests.get(
            url,
            params={"base": key[0], "symbols": key[1]},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        rate = float(data["rates"][key[1]])
    except Exception as exc:
        logger.warning("FX fetch failed for %s->%s: %s", key[0], key[1], exc)
        return None

    with _lock:
        _cache[key] = (rate, now)
    logger.info("FX rate %s->%s = %f (cached for %ds)",
                key[0], key[1], rate, _TTL_SECONDS)
    return rate


def convert(amount: float, currency_from: str, currency_to: str = "ILS") -> Optional[float]:
    """Convert `amount` from `currency_from` to `currency_to`. None on failure."""
    rate = get_rate(currency_from, currency_to)
    if rate is None:
        return None
    return round(amount * rate, 2)
