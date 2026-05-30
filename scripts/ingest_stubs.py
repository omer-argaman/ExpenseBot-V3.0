"""
Shared stubs for offline ingest/memory tests — import this, do NOT import test_ingest.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("INGEST_SECRET", "test-secret")
os.environ.setdefault("OWNER_CHAT_ID", "1")
os.environ.setdefault("HIGH_CONFIDENCE_THRESHOLD", "0.85")

import sheets as sheets_mod  # noqa: E402
import handlers.notifier as notifier_mod  # noqa: E402
import handlers.ai_handler as ai_handler_mod  # noqa: E402
import handlers.commands as commands_mod  # noqa: E402
import parsing.merchant_map as merchant_map_mod  # noqa: E402
import parsing.fx as fx_mod  # noqa: E402

LOGGED: list[dict] = []
NOTIFICATIONS: list[str] = []
LEARNED: list[tuple[str, str, str]] = []
AI_CALLS: list[dict] = []


def _stub_log_expense(category, amount, original_text, dt=None):
    LOGGED.append({
        "category": category,
        "amount": amount,
        "original_text": original_text,
        "dt": dt.isoformat() if dt else None,
    })
    from sheets import LogResult
    return LogResult(
        success=True, category=category, amount_added=amount, new_total=amount,
        tab_name="0526", row=1,
        timestamp=time.strftime("%Y-%m-%d %H:%M"),
        message=f"Added {amount} to {category}",
    )


def _stub_send_message(text, chat_id=None, inline_keyboard=None, parse_mode="HTML"):
    NOTIFICATIONS.append(text)
    return 12345


def _stub_append_to_history(**kwargs):
    pass


def _stub_learn(merchant, category, source="user"):
    LEARNED.append((merchant, category, source))


def _stub_lookup(merchant):
    return None


def _stub_ai_categorize(merchant, amount, currency="ILS",
                        card_prior=None, card_name=None, fuzzy_hint=None):
    AI_CALLS.append({
        "merchant": merchant, "amount": amount, "currency": currency,
        "card_prior": card_prior, "card_name": card_name,
    })
    rules = {
        "בילבונג בורד שופ": ("Other (Daily)", 0.92),
        "גולדה דיזינגוף":   ("Coffee",         0.78),
        "TERMINAL X":       ("Other (Daily)", 0.95),
        "עמותת עטלף":       ("Other (Daily)", 0.55),
        "עיריית תל אביב יפו": ("Property Tax", 0.97),
        "CLAUDE.AI SUBSCRIPTION - UNITED STATES": ("Education", 0.6),
        "PAYBOX":           ("Other (Daily)", 0.4),
        "משרתי הקבע רב מוטב": ("Other (Daily)", 0.45),
        "לאגר אנד אייל הרצליה": ("Beer / Wine", 0.9),
        "סי.טי.אי גומובייל בע": ("Internet", 0.88),
        "פספורטכארד שירותים": ("Life Insurance", 0.7),
        "GETT":              ("Public Transportation", 0.96),
    }
    cat, conf = rules.get(merchant, (None, 0.0))
    return {
        "category": cat, "confidence": conf,
        "reason": "stub", "alternatives": [],
    }


def _stub_fx_convert(amount, currency_from, currency_to="ILS"):
    if currency_from.upper() == "USD" and currency_to == "ILS":
        return round(amount * 3.65, 2)
    return None


def _stub_read_pending_asks():
    return []


def _stub_upsert_pending_ask(row):
    pass


def _stub_delete_pending_ask(pending_id):
    pass


def apply() -> None:
    """Monkey-patch external I/O boundaries for offline tests."""
    sheets_mod.log_expense = _stub_log_expense  # type: ignore[assignment]
    sheets_mod.read_pending_asks = _stub_read_pending_asks  # type: ignore[assignment]
    sheets_mod.upsert_pending_ask = _stub_upsert_pending_ask  # type: ignore[assignment]
    sheets_mod.delete_pending_ask = _stub_delete_pending_ask  # type: ignore[assignment]
    notifier_mod.send_message = _stub_send_message  # type: ignore[assignment]
    commands_mod.append_to_history = _stub_append_to_history  # type: ignore[assignment]
    merchant_map_mod.learn = _stub_learn  # type: ignore[assignment]
    merchant_map_mod.lookup = _stub_lookup  # type: ignore[assignment]
    ai_handler_mod.ai_categorize_merchant_sync = _stub_ai_categorize  # type: ignore[assignment]
    fx_mod.convert = _stub_fx_convert  # type: ignore[assignment]

    import importlib
    import handlers.transaction_handler as th_mod
    importlib.reload(th_mod)
