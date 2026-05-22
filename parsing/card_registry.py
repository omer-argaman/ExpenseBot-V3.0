"""
parsing/card_registry.py — Card last4 -> friendly name + category prior.

Holds metadata for each of your physical/virtual cards. Used downstream by
the AI categorization step: the prior list narrows the model's choice when
a card is dedicated to a specific kind of expense (e.g. card 4888 is your
prepaid food card, so transactions on it are almost always Groceries,
Dining Out, Coffee, or Beer / Wine).

Lookup order:
  1. CARD_PRIORS env var (JSON: {"last4": {...}, ...}) — preferred, lets you
     edit the priors on Render without a deploy.
  2. The DEFAULT_REGISTRY hard-coded in this file as a sane fallback.

If a transaction comes in for a last4 we don't know about, the lookup
returns an empty CardInfo with no prior — the AI then categorizes purely
from the merchant name, exactly as it would have for free-text Telegram
input today.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CardInfo:
    last4: str
    name: str = ""
    is_prepaid: bool = False
    category_prior: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_prior(self) -> bool:
        return bool(self.category_prior)


# Hard-coded baseline. Override via the CARD_PRIORS env var (JSON).
DEFAULT_REGISTRY: dict[str, CardInfo] = {
    # 0347 is ignored at ingest (IGNORED_CARDS); entry kept for CARD_PRIORS overrides.
    "0347": CardInfo(
        last4="0347",
        name="Isracard prepaid - clothing/appliances (ignored)",
        is_prepaid=True,
        category_prior=(),
    ),
    # 4888 is ignored at ingest (IGNORED_CARDS); entry kept for CARD_PRIORS overrides.
    "4888": CardInfo(
        last4="4888",
        name="Isracard prepaid - food (ignored)",
        is_prepaid=True,
        category_prior=(),
    ),
    "4881": CardInfo(
        last4="4881",
        name="Isracard credit - general",
        is_prepaid=False,
        category_prior=(),  # general-purpose, AI evaluates merchant on its own
    ),
}


_cache: dict[str, CardInfo] | None = None


def _load() -> dict[str, CardInfo]:
    """
    Load the registry once. Reads CARD_PRIORS env var if set, otherwise uses
    DEFAULT_REGISTRY. Cached for the process lifetime.
    """
    global _cache
    if _cache is not None:
        return _cache

    raw = os.getenv("CARD_PRIORS", "").strip()
    if not raw:
        _cache = dict(DEFAULT_REGISTRY)
        return _cache

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "CARD_PRIORS env var is not valid JSON; falling back to defaults. (%s)",
            exc,
        )
        _cache = dict(DEFAULT_REGISTRY)
        return _cache

    registry: dict[str, CardInfo] = {}
    for last4, info in data.items():
        if not isinstance(info, dict):
            continue
        registry[str(last4)] = CardInfo(
            last4=str(last4),
            name=str(info.get("name", "")),
            is_prepaid=bool(info.get("is_prepaid", False)),
            category_prior=tuple(info.get("category_prior", []) or ()),
        )
    if not registry:
        logger.warning("CARD_PRIORS parsed but empty; falling back to defaults.")
        _cache = dict(DEFAULT_REGISTRY)
    else:
        _cache = registry
    return _cache


def lookup(last4: str | None) -> CardInfo:
    """Return the CardInfo for `last4`, or an empty CardInfo if unknown."""
    if not last4:
        return CardInfo(last4="")
    registry = _load()
    return registry.get(last4) or CardInfo(last4=last4)
