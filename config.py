"""
config.py — All environment variables and constants in one place.

Every other module imports from here instead of touching os.getenv directly.
"""

import os
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Google Sheets
SPREADSHEET_ID          = os.getenv("SPREADSHEET_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS")

# Path to the expense history file used by /delete.
# Kept at the project root so it survives folder refactors.
HISTORY_FILE = os.path.join(os.path.dirname(__file__), "expense_history.json")

# How many recent expenses /delete can reach back to.
HISTORY_LIMIT = 10

# Subscriber list — chat_ids that receive the monthly report.
SUBSCRIBERS_FILE = os.path.join(os.path.dirname(__file__), "subscribers.json")

# Timezone for scheduled jobs (monthly report fires at 09:00 local time).
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


# ---------------------------------------------------------------------------
# Ingestion pipeline (SMS / email -> /ingest)
# ---------------------------------------------------------------------------

# Shared secret for authenticating POSTs to /ingest.
# Long random string. Same value must be configured in the Gmail Apps Script.
INGEST_SECRET = os.getenv("INGEST_SECRET", "")

# Telegram numeric chat id where the bot sends transaction notifications and
# inline-button asks. If unset, falls back to the first id in subscribers.json.
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID", "").strip()

# Comma-separated list of accepted issuers. Anything else is rejected by /ingest.
ALLOWED_ISSUERS = tuple(
    s.strip().lower()
    for s in os.getenv("ALLOWED_ISSUERS", "isracard").split(",")
    if s.strip()
)

# AI categorization auto-log threshold. Below this -> ask the user via Telegram.
try:
    HIGH_CONFIDENCE_THRESHOLD = float(os.getenv("HIGH_CONFIDENCE_THRESHOLD", "0.85"))
except (TypeError, ValueError):
    HIGH_CONFIDENCE_THRESHOLD = 0.85

# HTTP port for the Flask server (Render injects PORT). Used by server.py.
HTTP_PORT = int(os.getenv("PORT", "10000"))
