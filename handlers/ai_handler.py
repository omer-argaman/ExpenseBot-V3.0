"""
handlers/ai_handler.py — OpenAI-powered fallback for messages the rule-based
parser could not confidently handle.

ask_ai(user_message, history) -> dict
    Returns one of:
        {"action": "log",   "category": str, "amount": float}
        {"action": "reply", "text": str}

The AI receives:
  - A tightly scoped system prompt that lists every valid category + its
    trigger keywords, so it can map natural language to the right category.
  - The last AI_HISTORY_LIMIT messages from this user's conversation so that
    bare follow-ups like "150" after "How much was it?" work correctly.
  - A single tool — log_expense — which it MUST call whenever it can identify
    both a category and an amount. When it calls the tool it must NOT add any
    surrounding text (the confirmation message is built by the caller).
"""

import json
import logging

from openai import AsyncOpenAI

from config import OPENAI_API_KEY
from parsing.category_map import CATEGORY_MAP

logger = logging.getLogger(__name__)

# How many past messages (user + assistant turns) to keep in context.
# 6 = 3 full back-and-forth exchanges — enough for follow-up amounts while
# staying cheap.
AI_HISTORY_LIMIT = 6

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    """Return the shared AsyncOpenAI client, creating it on first use."""
    global _client
    if _client is None:
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set. Add it to your environment variables.")
        _client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _client

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def _build_system_prompt() -> str:
    category_lines = []
    for cat, keywords in CATEGORY_MAP.items():
        kw_sample = ", ".join(keywords[:8])
        category_lines.append(f"  - {cat}: {kw_sample}")
    categories_block = "\n".join(category_lines)

    return f"""You are an expense-logging assistant for a household budget tracker. \
Your job is to help users record their expenses quickly and naturally.

VALID CATEGORIES (use the exact name as shown):
{categories_block}

RULES — follow these strictly:
1. When you can identify both a category and an amount from the message (or from \
recent conversation context) → call the log_expense tool immediately. Do NOT add \
any text alongside the tool call.
2. When you can identify a category but the amount is missing → reply with ONE \
short sentence asking only for the amount. Example: "How much was it?"
3. When the message is ambiguous between two categories → pick the most likely one \
and call log_expense. Do not ask for confirmation.
4. When the message has nothing to do with an expense (e.g. a question, greeting) \
→ answer helpfully in one sentence maximum.
5. Currency is Israeli Shekel (₪). Accept any numeric format (120, 1,200, 1200.50).
6. NEVER be chatty. NEVER add filler like "Got it!", "Sure!", or "I've logged that". \
NEVER explain what you are about to do.
"""

SYSTEM_PROMPT = _build_system_prompt()

# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------

LOG_EXPENSE_TOOL = {
    "type": "function",
    "function": {
        "name": "log_expense",
        "description": (
            "Log an expense to the tracking system. "
            "Call this as soon as you know the category and amount."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": list(CATEGORY_MAP.keys()),
                    "description": "The exact canonical category name.",
                },
                "amount": {
                    "type": "number",
                    "description": "The expense amount in ILS (₪). Must be positive.",
                },
            },
            "required": ["category", "amount"],
        },
    },
}

# ---------------------------------------------------------------------------
# Main coroutine
# ---------------------------------------------------------------------------

async def ask_ai(user_message: str, history: list[dict]) -> dict:
    """
    Send user_message to GPT-4o-mini with recent conversation history.

    Returns:
        {"action": "log",   "category": str, "amount": float}  — AI wants to log
        {"action": "reply", "text": str}                        — AI wants to reply
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history[-AI_HISTORY_LIMIT:])
    messages.append({"role": "user", "content": user_message})

    try:
        response = await _get_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            tools=[LOG_EXPENSE_TOOL],
            tool_choice="auto",
            temperature=0,
        )
    except Exception as exc:
        logger.error("OpenAI API error: %s", exc)
        return {"action": "reply", "text": "Sorry, I couldn't process that right now. Please try again."}

    choice = response.choices[0]

    if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
        tool_call = choice.message.tool_calls[0]
        try:
            args = json.loads(tool_call.function.arguments)
            return {
                "action": "log",
                "category": args["category"],
                "amount": float(args["amount"]),
            }
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            logger.error("Failed to parse tool call arguments: %s", exc)
            return {"action": "reply", "text": "I understood the expense but had trouble logging it. Could you rephrase?"}

    text = (choice.message.content or "").strip()
    return {"action": "reply", "text": text or "Could you say that differently?"}
