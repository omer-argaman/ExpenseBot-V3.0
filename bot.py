"""
bot.py — Telegram bot entry point.

Run with:  python bot.py   (from inside the experiment/ folder)

This file only wires up the Telegram Application and starts polling.
All actual logic lives in handlers/ — nothing here should need to change
when business logic changes.
"""

import logging
import resource
from datetime import datetime, time as dt_time, timedelta

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import ISRAEL_TZ, TELEGRAM_BOT_TOKEN
from parsing.merchant_map import preload as preload_merchant_map
from handlers.transaction_handler import preload_pending_asks
from handlers.callbacks import handle_callback
from handlers.commands import (
    tg_balance,
    tg_categories,
    tg_category,
    tg_delete,
    tg_help,
    tg_keywords,
    tg_summary,
)
from handlers.message import tg_handle_message
from handlers.monthly_report import send_monthly_report, tg_test_report
from server import start_in_thread as start_flask_server

logging.basicConfig(
    format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# /whoami — returns the user's chat id so they can set OWNER_CHAT_ID on Render
# ---------------------------------------------------------------------------

async def tg_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    name = user.full_name if user else "<unknown>"
    await update.message.reply_text(
        f"Your Telegram chat id: <code>{chat_id}</code>\n"
        f"Name: {name}\n\n"
        f"Set this on Render as <code>OWNER_CHAT_ID</code> so the SMS pipeline "
        f"knows where to send transaction notifications.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Idle-user cleanup — safety net for the in-memory AI history
# ---------------------------------------------------------------------------
#
# Even with a hard per-user cap on ai_history length, python-telegram-bot holds
# every user's user_data dict in memory forever (one entry per chat_id). For
# users who stop messaging entirely, that dict just sits there. This weekly job
# drops the history of anyone idle for more than IDLE_THRESHOLD_DAYS.

IDLE_THRESHOLD_DAYS = 2
_rss_log_counter = 0


def _log_memory_rss() -> None:
    """Log process RSS (KB) for Render memory monitoring."""
    try:
        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS reports bytes; Linux reports KB
        import sys
        if sys.platform == "darwin":
            rss_kb = rss_kb // 1024
        logger.info("Memory RSS: %d MB", rss_kb // 1024)
    except Exception as exc:
        logger.debug("Could not read RSS: %s", exc)


async def _cleanup_idle_users(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop idle user_data dicts to limit PTB per-chat memory growth."""
    global _rss_log_counter
    cutoff = (datetime.now() - timedelta(days=IDLE_THRESHOLD_DAYS)).timestamp()
    cleaned = 0
    to_remove: list[int] = []
    for chat_id, user_data in list(context.application.user_data.items()):
        last_seen = user_data.get("last_seen", 0)
        if last_seen < cutoff:
            to_remove.append(chat_id)
            cleaned += 1
    for chat_id in to_remove:
        context.application.user_data.pop(chat_id, None)
    if cleaned:
        logger.info(
            "Idle-cleanup: removed user_data for %d idle chat(s)", cleaned
        )
    _rss_log_counter += 1
    if _rss_log_counter % 7 == 0:
        _log_memory_rss()
    from _debug_trace import dbg  # noqa: PLC0415

    dbg("H6", "bot._cleanup_idle_users", "tick", {
        "user_data_count": len(context.application.user_data),
        "cleaned": cleaned,
    })


async def _post_init(application: Application) -> None:
    """Register scheduled jobs after the Application is fully initialised."""
    try:
        n_merchants = preload_merchant_map()
        logger.info("Startup: merchant map preloaded (%d entries)", n_merchants)
    except Exception as exc:
        logger.warning("Startup: merchant map preload failed: %s", exc)
        n_merchants = -1
    try:
        n_pending = preload_pending_asks()
        logger.info("Startup: pending asks preloaded (%d entries)", n_pending)
    except Exception as exc:
        logger.warning("Startup: pending asks preload failed: %s", exc)
        n_pending = -1
    _log_memory_rss()
    from _debug_trace import dbg  # noqa: PLC0415

    dbg("ALL", "bot._post_init", "startup_complete", {
        "n_merchants": n_merchants,
        "n_pending": n_pending,
    })

    application.job_queue.run_monthly(
        send_monthly_report,
        when=dt_time(hour=9, minute=0, second=0, tzinfo=ISRAEL_TZ),
        day=1,
    )
    logger.info("Monthly report job registered: 1st of each month at 09:00 IST")

    application.job_queue.run_repeating(
        _cleanup_idle_users,
        interval=timedelta(days=1),
        first=timedelta(hours=1),
    )
    logger.info("Idle-user cleanup job registered: runs daily, "
                f"drops history for users idle > {IDLE_THRESHOLD_DAYS} days")

    application.job_queue.run_repeating(
        _memtrace_tick,
        interval=timedelta(minutes=10),
        first=timedelta(minutes=1),
    )
    logger.info("Memtrace snapshot job registered: every 10 min")


async def _memtrace_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic heap census so we can see idle-period memory creep."""
    from _debug_trace import dbg, gc_census  # noqa: PLC0415

    dbg("IDLE", "bot._memtrace_tick", "snapshot", {
        "user_data_count": len(context.application.user_data),
        **gc_census(),
    })


def create_app() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise EnvironmentError("TELEGRAM_BOT_TOKEN is not set in .env")

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )

    # Slash commands
    app.add_handler(CommandHandler("start",      tg_help))
    app.add_handler(CommandHandler("help",       tg_help))
    app.add_handler(CommandHandler("categories", tg_categories))
    app.add_handler(CommandHandler("keywords",   tg_keywords))
    app.add_handler(CommandHandler("summary",    tg_summary))
    app.add_handler(CommandHandler("category",   tg_category))
    app.add_handler(CommandHandler("balance",    tg_balance))
    app.add_handler(CommandHandler("delete",      tg_delete))
    app.add_handler(CommandHandler("report", tg_test_report))
    app.add_handler(CommandHandler("whoami", tg_whoami))

    # Inline button callbacks (fuzzy confirm yes/no)
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Free-text messages — must be registered last
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tg_handle_message))

    return app


if __name__ == "__main__":
    # Flask serves /healthz (Render keepalive) and /ingest (SMS/email webhook).
    # Daemon thread shares the process with the python-telegram-bot polling loop.
    start_flask_server()
    logger.info("Starting bot...")
    create_app().run_polling()
