"""
bot.py — Telegram bot entry point.

Run with:  python bot.py   (from inside the experiment/ folder)

This file only wires up the Telegram Application and starts polling.
All actual logic lives in handlers/ — nothing here should need to change
when business logic changes.
"""

import logging
import os
import threading
from datetime import datetime, time as dt_time, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import ISRAEL_TZ, TELEGRAM_BOT_TOKEN
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

logging.basicConfig(
    format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Minimal HTTP server — keeps Render (Web Service) happy by binding to PORT
# ---------------------------------------------------------------------------

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass  # suppress noisy access logs


def _start_health_server() -> None:
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    logger.info(f"Health server listening on port {port}")
    server.serve_forever()


# ---------------------------------------------------------------------------
# Idle-user cleanup — safety net for the in-memory AI history
# ---------------------------------------------------------------------------
#
# Even with a hard per-user cap on ai_history length, python-telegram-bot holds
# every user's user_data dict in memory forever (one entry per chat_id). For
# users who stop messaging entirely, that dict just sits there. This weekly job
# drops the history of anyone idle for more than IDLE_THRESHOLD_DAYS.

IDLE_THRESHOLD_DAYS = 2


async def _cleanup_idle_users(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop ai_history for users who haven't messaged in IDLE_THRESHOLD_DAYS."""
    cutoff = (datetime.now() - timedelta(days=IDLE_THRESHOLD_DAYS)).timestamp()
    cleaned = 0
    for user_data in context.application.user_data.values():
        last_seen = user_data.get("last_seen", 0)
        if last_seen < cutoff and user_data.get("ai_history"):
            user_data.pop("ai_history", None)
            cleaned += 1
    if cleaned:
        logger.info(f"Idle-cleanup: dropped ai_history for {cleaned} idle user(s)")


async def _post_init(application: Application) -> None:
    """Register scheduled jobs after the Application is fully initialised."""
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

    # Inline button callbacks (fuzzy confirm yes/no)
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Free-text messages — must be registered last
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tg_handle_message))

    return app


if __name__ == "__main__":
    threading.Thread(target=_start_health_server, daemon=True).start()
    logger.info("Starting bot...")
    create_app().run_polling()
