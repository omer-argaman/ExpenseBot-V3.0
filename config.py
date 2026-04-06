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
