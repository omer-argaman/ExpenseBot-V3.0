"""
handlers/ai_handler.py — OpenAI-powered assistant for the expense bot.

ask_ai(user_message, history) -> dict
    Returns one of:
        {"action": "log",        "category": str, "amount": float}
        {"action": "log_multiple","expenses": [{"category": str, "amount": float}, ...]}
        {"action": "delete",     "n": int}
        {"action": "reply",      "text": str}
        {"action": "reply",      "text": str, "show_summary": {"month": int, "year": int}}

Tools available to the AI:
    log_expense           — log one expense (caller handles the write)
    delete_expense        — delete/undo the last n expenses
    show_summary          — display the interactive summary UI; data is also fed
                            back to the AI so it can answer questions about it
    get_category_spending — budget + transaction history for one category
    get_all_transactions  — every logged transaction across ALL categories for a
                            month — enables emoji search, keyword search, full
                            cross-category analysis
    compare_months        — side-by-side budget vs actuals for two months
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

# Per-user history window (number of individual messages, not pairs)
AI_HISTORY_LIMIT = 12
# Safety cap on the agentic tool-call loop per response
MAX_TOOL_ITERATIONS = 6

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

You have five capabilities:
1. LOG expenses          → call log_expense for each expense you identify.
2. DELETE/UNDO expenses  → call delete_expense when the user wants to remove a \
recent entry.
3. BUDGET SUMMARY        → call show_summary for anything involving the monthly \
budget overview — whether the user wants to view it OR asks a question about it.
4. READ transaction data → call get_category_spending (one category) or \
get_all_transactions (everything) to see individual entries, search notes, \
find specific items.
5. ADVISE                → after reading data, give specific, number-backed \
recommendations.

VALID CATEGORIES (use the exact name shown):
{categories_block}

RULES — CATEGORIZATION:
- Always categorize by WHAT was purchased, not WHERE. \
  "snacks at a gas station" → Groceries or Other (Daily), NOT Fuel or Gas/Oil. \
  "coffee at a pharmacy" → Coffee, NOT Health. \
  "beer at the supermarket" → Beer / Wine, NOT Groceries.
- When a category is ambiguous, pick the one that best matches the item itself.

RULES — MULTIPLE EXPENSES:
- When a single message contains multiple expenses, call log_expense ONCE PER \
  EXPENSE in the same response. Do not stop after the first one.

RULES — DELETING:
- Call delete_expense when the user says anything like: "delete that", "undo", \
  "that was wrong", "I made a mistake", "no, remove that", "cancel", "oops". \
  Use n=1 unless the user specifies a different number.

RULES — SUMMARY:
- Call show_summary for ANY request involving the budget overview — "show me \
  the summary", "how's my budget?", "am I over budget?", "give me \
  recommendations", "budget overview", "show last month", etc.
- After calling show_summary you will receive the data. Use it to answer the \
  user's question if they asked one. If they only wanted to VIEW the summary \
  (no specific question), return NO text — the interactive screen is enough.

RULES — GENERAL:
- Logging: call log_expense immediately — zero filler text alongside the call.
- Questions about specific items, keywords, or entries (including emojis, names, \
  stores): use get_all_transactions.
- Questions about one category: use get_category_spending.
- Month comparisons: use compare_months.
- Missing amount: ask in ONE sentence only.
- Recommendations: reference real numbers.
- Currency is Israeli Shekel (₪).
- NEVER say "Got it!", "Sure!", "I've logged that", or any filler.
- Answer concisely — 1-4 sentences unless a full breakdown is requested.
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
            "Log an expense. Call the moment you know both the category and amount."
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
                    "description": "Amount in ILS (₪). Must be positive.",
                },
            },
            "required": ["category", "amount"],
        },
    },
}

DELETE_EXPENSE_TOOL = {
    "type": "function",
    "function": {
        "name": "delete_expense",
        "description": (
            "Undo / delete the most recent expense(s). Call this when the user "
            "expresses any intent to remove, undo, or cancel a recent entry — "
            "e.g. 'delete that', 'undo', 'that was wrong', 'I made a mistake', "
            "'oops', 'cancel', 'no, remove that'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "n": {
                    "type": "integer",
                    "description": (
                        "Number of recent expenses to delete. "
                        "Default is 1. Use a higher number only if the user "
                        "explicitly asks to delete multiple (e.g. 'delete the last 3')."
                    ),
                },
            },
            "required": [],
        },
    },
}

SHOW_SUMMARY_TOOL = {
    "type": "function",
    "function": {
        "name": "show_summary",
        "description": (
            "Display the interactive budget summary UI to the user AND receive the "
            "budget data so you can reason about it. Call this for ANY request "
            "involving the monthly budget — whether the user wants to view it "
            "('show me the summary', 'open summary', 'budget overview') or asks a "
            "question about it ('am I over budget?', 'where should I cut costs?', "
            "'how much did I spend in total?'). The data is returned to you; "
            "only add a text response if the user asked a specific question."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "month": {
                    "type": "integer",
                    "description": "Month 1–12. Omit for current month.",
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
            "Get budget, amount spent, remaining balance, and transaction history "
            "for a single category. Use for specific questions like 'how much did "
            "I spend on groceries?' or 'show me my dining history'."
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

GET_ALL_TRANSACTIONS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_all_transactions",
        "description": (
            "Get every individual transaction entry across ALL categories for a "
            "given month. Each entry includes its date, time, and the original "
            "message the user typed (which may include emojis, store names, notes, "
            "etc.). Use this when you need to search across categories — e.g. find "
            "emojis in notes, find all entries mentioning a specific store or word, "
            "or answer any question that requires seeing the raw transaction text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "month": {
                    "type": "integer",
                    "description": "Month 1–12. Omit for current month.",
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

COMPARE_MONTHS_TOOL = {
    "type": "function",
    "function": {
        "name": "compare_months",
        "description": (
            "Get budget vs actual spending for two months side by side. "
            "Use when the user asks how one month compares to another, asks about "
            "trends, or wants to see if spending went up or down."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "month1": {"type": "integer", "description": "First month (1–12)."},
                "year1":  {"type": "integer", "description": "First month's year."},
                "month2": {"type": "integer", "description": "Second month (1–12)."},
                "year2":  {"type": "integer", "description": "Second month's year."},
            },
            "required": ["month1", "year1", "month2", "year2"],
        },
    },
}

ALL_TOOLS = [
    LOG_EXPENSE_TOOL,
    DELETE_EXPENSE_TOOL,
    SHOW_SUMMARY_TOOL,
    GET_CATEGORY_TOOL,
    GET_ALL_TRANSACTIONS_TOOL,
    COMPARE_MONTHS_TOOL,
]

# ---------------------------------------------------------------------------
# Tool execution helpers
# ---------------------------------------------------------------------------

def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


async def _run_get_monthly_summary(month: int | None, year: int | None) -> str:
    from handlers.commands import summary
    now = datetime.now()
    dt = datetime(year or now.year, month or now.month, 1)
    text, _ = await asyncio.to_thread(summary, dt)
    return _strip_html(text)


async def _run_get_category_spending(
    category: str, month: int | None, year: int | None
) -> str:
    from handlers.commands import category as category_fn
    now = datetime.now()
    dt = datetime(year or now.year, month or now.month, 1)
    return await asyncio.to_thread(category_fn, category, dt)


async def _run_get_all_transactions(month: int | None, year: int | None) -> str:
    """
    Read every category's transaction notes for the given month in two API calls:
      1. spreadsheets.values.get  → column A rows to map categories to row numbers
      2. spreadsheets.get         → all notes from column C in one shot
    """
    from sheets import _build_service, find_tab_for_month, SPREADSHEET_ID

    now = datetime.now()
    dt = datetime(year or now.year, month or now.month, 1)

    def _fetch() -> str:
        service  = _build_service()
        tab_info = find_tab_for_month(service, dt)
        if not tab_info:
            return f"No sheet tab found for {dt.strftime('%B %Y')}."
        tab_name, _ = tab_info

        # 1. Read column A to map category names → row numbers (1-indexed)
        rows_result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{tab_name}'!A1:A200",
        ).execute()
        row_labels = [
            (r[0].strip() if r else "") for r in rows_result.get("values", [])
        ]
        cat_rows: dict[str, int] = {}
        for cat in CATEGORY_MAP:
            for i, label in enumerate(row_labels):
                if label.lower() == cat.lower():
                    cat_rows[cat] = i + 1  # convert to 1-indexed
                    break

        if not cat_rows:
            return f"No category rows found in tab '{tab_name}'."

        # 2. Read all notes from column C in one API call
        notes_result = service.spreadsheets().get(
            spreadsheetId=SPREADSHEET_ID,
            ranges=[f"'{tab_name}'!C1:C200"],
            fields="sheets(data(rowData(values(note))))",
        ).execute()
        all_row_data = (
            notes_result
            .get("sheets", [{}])[0]
            .get("data",   [{}])[0]
            .get("rowData", [])
        )

        def note_at(row_1indexed: int) -> str:
            idx = row_1indexed - 1
            if idx >= len(all_row_data):
                return ""
            return (
                all_row_data[idx]
                .get("values", [{}])[0]
                .get("note", "")
            ) or ""

        # Compile into a readable block
        lines = [f"All transactions — {dt.strftime('%B %Y')}:\n"]
        for cat, row in cat_rows.items():
            note = note_at(row).strip()
            if note:
                lines.append(f"\n[{cat}]")
                for entry in note.split("\n"):
                    if entry.strip():
                        lines.append(f"  {entry.strip()}")

        if len(lines) == 1:
            return f"No transactions recorded in {dt.strftime('%B %Y')}."
        return "\n".join(lines)

    return await asyncio.to_thread(_fetch)


async def _run_compare_months(
    month1: int, year1: int, month2: int, year2: int
) -> str:
    from handlers.commands import summary
    dt1 = datetime(year1, month1, 1)
    dt2 = datetime(year2, month2, 1)
    text1, _ = await asyncio.to_thread(summary, dt1)
    text2, _ = await asyncio.to_thread(summary, dt2)
    return (
        f"=== {dt1.strftime('%B %Y')} ===\n{_strip_html(text1)}\n\n"
        f"=== {dt2.strftime('%B %Y')} ===\n{_strip_html(text2)}"
    )


async def _execute_tool(tool_name: str, args: dict) -> str:
    try:
        if tool_name == "get_category_spending":
            return await _run_get_category_spending(
                category=args["category"],
                month=args.get("month"),
                year=args.get("year"),
            )
        if tool_name == "get_all_transactions":
            return await _run_get_all_transactions(
                month=args.get("month"), year=args.get("year")
            )
        if tool_name == "compare_months":
            return await _run_compare_months(
                month1=args["month1"], year1=args["year1"],
                month2=args["month2"], year2=args["year2"],
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

    Runs an agentic loop: the AI can call read tools, receive their output,
    and produce a natural final answer — all surfaced as one reply to the user.
    log_expense is the only write tool; it is returned immediately to the caller.

    Returns:
        {"action": "log",   "category": str, "amount": float}
        {"action": "reply", "text": str}
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history[-AI_HISTORY_LIMIT:])
    messages.append({"role": "user", "content": user_message})

    # Tracks whether the interactive summary UI should be displayed after the loop.
    # Set when the AI calls show_summary; attached to the final reply so message.py
    # can render the UI alongside any text the AI produces.
    pending_show_summary: dict | None = None

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
            all_calls = choice.message.tool_calls

            # --- Collect ALL log_expense calls (multi-expense support) ----------
            log_calls = [tc for tc in all_calls if tc.function.name == "log_expense"]
            if log_calls:
                expenses = []
                for tc in log_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                        expenses.append({
                            "category": args.get("category", ""),
                            "amount":   float(args.get("amount", 0)),
                        })
                    except (json.JSONDecodeError, ValueError, KeyError):
                        continue
                if expenses:
                    if len(expenses) == 1:
                        return {"action": "log", **expenses[0]}
                    return {"action": "log_multiple", "expenses": expenses}

            # --- Single non-log tool call ----------------------------------------
            tool_call = all_calls[0]
            tool_name = tool_call.function.name

            try:
                args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                args = {}

            # delete_expense — return to caller for execution
            if tool_name == "delete_expense":
                return {"action": "delete", "n": int(args.get("n", 1))}

            # show_summary — fetch data so AI can reason, flag UI for display,
            # then stay in the loop so the AI can formulate a text response
            if tool_name == "show_summary":
                now_dt = datetime.now()
                pending_show_summary = {
                    "month": args.get("month") or now_dt.month,
                    "year":  args.get("year")  or now_dt.year,
                }
                data = await _run_get_monthly_summary(
                    args.get("month"), args.get("year")
                )
                messages.append(choice.message)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": data,
                })
                continue

            # All other read tools: execute, feed result back, loop for final answer
            tool_result = await _execute_tool(tool_name, args)
            messages.append(choice.message)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": tool_result,
            })
            continue

        # Plain text response — done; attach show_summary if it was requested
        text = (choice.message.content or "").strip()
        result: dict = {"action": "reply", "text": text}
        if pending_show_summary:
            result["show_summary"] = pending_show_summary
        return result

    result = {
        "action": "reply",
        "text": "I had trouble processing that. Please try again.",
    }
    if pending_show_summary:
        result["show_summary"] = pending_show_summary
    return result
