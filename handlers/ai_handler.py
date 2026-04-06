"""
handlers/ai_handler.py — OpenAI-powered assistant for the expense bot.

ask_ai(user_message, history) -> dict
    Returns one of:
        {"action": "log",   "category": str, "amount": float}
        {"action": "reply", "text": str}

Tools available to the AI:
    log_expense           — log an expense (executed by the caller, not here)
    get_monthly_summary   — read a month's full budget vs actuals from the sheet
    get_category_spending — read detailed spending + history for one category

When the AI calls get_monthly_summary or get_category_spending the result is
fed back into the conversation so the AI can formulate a natural response
(recommendations, comparisons, etc.).  log_expense is returned immediately
to the caller so it can confirm the write to the user.
"""

import asyncio
import json
import logging
import re
from datetime import datetime

from openai import AsyncOpenAI

from config import OPENAI_API_KEY
from parsing.category_map import CATEGORY_MAP

logger = logging.getLogger(__name__)

# Per-user history cap (number of messages, not pairs)
AI_HISTORY_LIMIT = 6
# Safety cap on the agentic tool-call loop
MAX_TOOL_ITERATIONS = 5

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    """Return the shared AsyncOpenAI client, creating it on first use."""
    global _client
    if _client is None:
        if not OPENAI_API_KEY:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Add it to your environment variables."
            )
        _client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _client


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def _build_system_prompt() -> str:
    now = datetime.now()
    category_lines = [
        f"  - {cat}: {', '.join(kws[:8])}"
        for cat, kws in CATEGORY_MAP.items()
    ]
    categories_block = "\n".join(category_lines)

    return f"""You are a smart, concise expense-tracking assistant for a household \
budget bot. Today is {now.strftime('%B %d, %Y')}.

You have three capabilities:
1. LOG expenses   → call log_expense when you know category + amount.
2. READ data      → call get_monthly_summary or get_category_spending to look up \
spending, then answer naturally using the data you get back.
3. ADVISE         → after reading data, give specific, number-backed recommendations.

VALID CATEGORIES (use the exact name shown):
{categories_block}

RULES:
- Logging: call log_expense immediately — zero filler text alongside it.
- Questions about spending: use your read tools, then answer in plain language. \
  Reference real numbers. Keep it under 5 sentences unless a full breakdown is asked for.
- Missing amount for a log: ask in ONE sentence only.
- Ambiguous category: pick the most likely one, log it.
- Recommendations: be specific — say which category is over budget and by how much, \
  or where there is room to save.
- Currency is Israeli Shekel (₪).
- NEVER say "Got it!", "Sure!", "I've logged that", or any filler.
- If you genuinely cannot answer without data, call the appropriate read tool first.
"""

SYSTEM_PROMPT = _build_system_prompt()

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

LOG_EXPENSE_TOOL = {
    "type": "function",
    "function": {
        "name": "log_expense",
        "description": (
            "Log an expense to the tracking system. "
            "Call this the moment you know both the category and the amount."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": list(CATEGORY_MAP.keys()),
                    "description": "Exact canonical category name.",
                },
                "amount": {
                    "type": "number",
                    "description": "Expense amount in ILS (₪). Must be positive.",
                },
            },
            "required": ["category", "amount"],
        },
    },
}

GET_SUMMARY_TOOL = {
    "type": "function",
    "function": {
        "name": "get_monthly_summary",
        "description": (
            "Get the full budget vs actual spending breakdown for a given month. "
            "Use this to answer questions about overall spending, budget status, "
            "over-budget sections, or to produce recommendations."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "month": {
                    "type": "integer",
                    "description": "Month number 1–12. Omit for current month.",
                },
                "year": {
                    "type": "integer",
                    "description": "4-digit year. Omit for current year.",
                },
            },
            "required": [],
        },
    },
}

GET_CATEGORY_TOOL = {
    "type": "function",
    "function": {
        "name": "get_category_spending",
        "description": (
            "Get the budget, amount spent, remaining balance, and full transaction "
            "history for a single category. Use this for specific questions like "
            "'how much did I spend on groceries?' or 'show me my dining history'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": list(CATEGORY_MAP.keys()),
                    "description": "Exact canonical category name.",
                },
                "month": {
                    "type": "integer",
                    "description": "Month 1–12. Omit for current month.",
                },
                "year": {
                    "type": "integer",
                    "description": "4-digit year. Omit for current year.",
                },
            },
            "required": ["category"],
        },
    },
}

ALL_TOOLS = [LOG_EXPENSE_TOOL, GET_SUMMARY_TOOL, GET_CATEGORY_TOOL]

# ---------------------------------------------------------------------------
# Tool execution helpers
# ---------------------------------------------------------------------------

def _strip_html(text: str) -> str:
    """Remove HTML tags so the AI receives clean plain text."""
    return re.sub(r"<[^>]+>", "", text)


async def _run_get_monthly_summary(month: int | None, year: int | None) -> str:
    from handlers.commands import summary
    now = datetime.now()
    dt = datetime(year or now.year, month or now.month, 1)
    # summary() is sync and makes network calls — run in a thread
    text, _ = await asyncio.to_thread(summary, dt)
    return _strip_html(text)


async def _run_get_category_spending(
    category: str, month: int | None, year: int | None
) -> str:
    from handlers.commands import category as category_fn
    now = datetime.now()
    dt = datetime(year or now.year, month or now.month, 1)
    return await asyncio.to_thread(category_fn, category, dt)


async def _execute_tool(tool_name: str, args: dict) -> str:
    try:
        if tool_name == "get_monthly_summary":
            return await _run_get_monthly_summary(
                month=args.get("month"),
                year=args.get("year"),
            )
        if tool_name == "get_category_spending":
            return await _run_get_category_spending(
                category=args["category"],
                month=args.get("month"),
                year=args.get("year"),
            )
        return f"Unknown tool: {tool_name}"
    except Exception as exc:
        logger.error("Tool %s failed: %s", tool_name, exc)
        return f"Error fetching data: {exc}"


# ---------------------------------------------------------------------------
# Main coroutine
# ---------------------------------------------------------------------------

async def ask_ai(user_message: str, history: list[dict]) -> dict:
    """
    Send user_message to GPT-4o-mini with recent conversation history.

    Runs an agentic loop so the AI can call read tools, receive their output,
    and produce a natural final response — all in one user-visible reply.

    Returns:
        {"action": "log",   "category": str, "amount": float}
        {"action": "reply", "text": str}
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history[-AI_HISTORY_LIMIT:])
    messages.append({"role": "user", "content": user_message})

    for _ in range(MAX_TOOL_ITERATIONS):
        try:
            response = await _get_client().chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                tools=ALL_TOOLS,
                tool_choice="auto",
                temperature=0,
            )
        except Exception as exc:
            logger.error("OpenAI API error: %s", exc)
            return {
                "action": "reply",
                "text": "Sorry, I couldn't process that right now. Please try again.",
            }

        choice = response.choices[0]

        if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
            tool_call = choice.message.tool_calls[0]
            tool_name = tool_call.function.name

            try:
                args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                args = {}

            # log_expense is a write — return immediately so the caller handles it
            if tool_name == "log_expense":
                return {
                    "action": "log",
                    "category": args.get("category", ""),
                    "amount": float(args.get("amount", 0)),
                }

            # Read tools: execute, inject result, loop back for AI's final answer
            tool_result = await _execute_tool(tool_name, args)
            messages.append(choice.message)   # assistant message with tool_calls field
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": tool_result,
            })
            continue

        # Plain text response — we're done
        text = (choice.message.content or "").strip()
        return {"action": "reply", "text": text or "Could you say that differently?"}

    return {
        "action": "reply",
        "text": "I had trouble processing that. Please try again.",
    }
