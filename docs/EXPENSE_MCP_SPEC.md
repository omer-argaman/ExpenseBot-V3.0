# Expense MCP — Technical Spec (PRD + Design)

Status: ready to build · Owner: Omer · Written: 2026-10-15
Source of truth for the existing system: `omer-argaman/ExpenseBot-V3.0` (public, Python).
Every statement about the bot below was checked against that repo's code at commit `11ba983`. Where this spec says "mirror X", open that function and copy its behavior exactly.

---

## 0. How to read this document

The work is three phases, built in order:

| Phase | Where | What |
|---|---|---|
| **A. Bot changes** | existing repo `ExpenseBot-V3.0` | Small changes so nothing is silently lost, and so the bot and the MCP don't step on each other. **Must ship before Phase B touches the real sheet.** |
| **B. MCP server** | new private repo `omer-argaman/expense-mcp` | A Python MCP server on Render, reached from Claude via Composio. |
| **C. Daily review** | Claude scheduled task | Every day at 16:00 Israel time, Claude opens a chat, lists everything that needs Omer's decision, and settles it with him. |

Words used throughout:
- **Sheet**: the one Google Sheet (`SPREADSHEET_ID`).
- **Month tab**: a tab like `1026` (Oct 2026).
- **Entry**: one line in the note on column C of a category row: `YYYY-MM-DD HH:MM  <text>` (two spaces between time and text).
- **Ask**: the bot's Telegram question "Which category?" for a merchant it doesn't know.
- **Ledger**: the new hidden `Transactions` tab (Phase A1), one row per card transaction the bot received.
- **Settle**: resolve a review item (log it to a category, skip it, or confirm/fix its category).

---

## 1. Goal and non-goals

### Goal
Omer never loses an expense and never has to dig through Telegram. Specifically:

1. **Daily review (primary).** Every day at 16:00 (Asia/Jerusalem) Claude checks for every card transaction that hasn't been correctly logged and confirmed, and asks Omer about each one. Examples: an unanswered ask (at any age, including older than 24h), a sheet-write or parse failure, a fallback to "Other (Daily)", or an AI auto-guess. Omer picks the category himself, and Claude settles it.
2. **Omer always chooses the category.** Claude may show the bot's guess and options, but never logs an unknown-merchant expense without Omer's explicit choice. Once he chooses, the merchant is remembered (same as tapping the Telegram button), so he's asked once per business.
3. **Ask from any Claude chat**, read-only: "how much did I spend on food in September?", "what did I buy at Shufersal this month?", "compare Aug vs Sep".
4. **Never break the bot.** Same write format, same tabs. `/summary`, `/category`, `/delete`, the monthly report and the SMS pipeline keep working.

### Non-goals
- Reading Telegram chat history. Telegram's Bot API cannot fetch past messages. The ledger + Pending tab replace that.
- Detecting purchases whose SMS never reached the bot (Shortcut didn't fire, phone off). The bot can't know about them. A card-statement import could cover this later.
- Recovering asks that already expired *before* Phase A ships. The bot deleted them and left no trace.
- Changing how expenses are parsed, categorized or logged by the bot. The Telegram text format (`supermarket 15 milk`) stays as is.
- Editing budgets (column B), the Format tab or the sheet layout.
- OAuth, multi-user, or any UI besides Claude.
- Foreign-currency conversion inside the MCP (the bot already does it; see §6.6).

---

## 2. The existing system (facts the builder must respect)

### 2.1 Sheet layout
- Month tabs are resolved by `sheets._candidate_tab_names` + `sheets.find_tab_in_tabs` (exact lowercase match first, then match ignoring whitespace/punctuation). Supported names: `MMYY`, `MM/YY`, `MM-YY`, `MM.YY`, `YYYY-MM`, `MM/YYYY`, `M/YYYY`, `Month YYYY`, `Mon YYYY`, `YYYY Month`, `YYYY Mon`, `Month YY`, `Mon YY`. **Copy these two functions verbatim.**
- Missing month: the bot creates it by duplicating the tab named `Format` (case-insensitive lookup `"format"`) with the new name `MMYY` (`sheets._ensure_month_tab`). Mirror this.
- Columns: A = category / section label, B = budget, C = spent (running total, a plain number written by the bot), D = remaining.
- Category row: first row in `A1:A200` whose trimmed, lowercased A equals the category, lowercased (`sheets.find_category_row`).
- Sections (`parsing/category_map.py: BROAD_CATEGORIES`): Home, Transportation, Daily Living, Other. Section total row index (0-based within `A1:D200`) = header index + `len(BROAD_CATEGORIES[section])` + 1. Read B/C/D from that row, as `handlers/commands.summary` does.
- Valid loggable categories = keys of `CATEGORY_MAP` (currently 25: Public Transportation, Fuel, Parking, Other (Trans), Rent, Electricity, Gas/Oil, Water, Property Tax, Internet, House Committee, Maintenance/Improvements, Groceries, Coffee, Dining Out, Beer / Wine, Other (Daily), Entertainment, Vacation, Education, Health, Life Insurance, Emergency Fund, Omer, Gil). Note that `Mortgage` appears in `BROAD_CATEGORIES["Home"]` but is **not** loggable. Education, Emergency Fund, Omer and Gil are loggable but not in any section.
- Currency parsing of B/C/D values: strip `₪` and `,`, then `float`; empty or unparseable → 0 (`commands._parse_currency`).

### 2.2 How the bot writes an expense (`sheets.log_expense`) — the format to mirror
1. Resolve the month tab from the **transaction date** (`dt`), creating it from Format if missing.
2. Find the category row. If missing → fail and write nothing.
3. Read C (strip `₪` and `,`, else 0), add the amount (negative for refunds), and write the new number with `valueInputOption=USER_ENTERED`.
4. `timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")`, i.e. **the time of logging, not the purchase date**.
5. Read the existing note on C (`spreadsheets.get` with `fields="sheets(data(rowData(values(note))))"`). New note = `(existing + "\n" + f"{timestamp}  {original_text}").strip()`. If longer than 12,000 chars, keep only the last 50 lines.
6. Write the note with `batchUpdate` → `updateCells`, `fields="note"`.

For SMS transactions, `original_text` is the `sheet_note` built by `transaction_handler._build_sheet_note`, for example:
`[card 1234] REFUND: SHUFERSAL DEAL 86.5 ILS (orig 23 USD) on 2026-10-12 14:03`
(parts are optional: card, `REFUND:`, merchant, `<amount> ILS`, FX note, `on <date> [time]`).

### 2.3 How SMS transactions flow today (`handlers/transaction_handler.process_ingest`)
| Outcome | What the bot does today |
|---|---|
| Declined | Skipped silently. |
| Unparseable SMS | Telegram "please log manually". **Nothing recorded.** |
| Card in `IGNORED_CARDS` (default 4888, 0347) | Skipped silently. |
| FX conversion failed | Telegram "please log manually". **Nothing recorded.** |
| Duplicate (in-memory 24h dedupe) | Skipped. |
| Merchant-map hit (exact, or fuzzy ≥ strong threshold) | Logged + Telegram "Logged". `map_origin="map (exact\|fuzzy, NN%)"`. |
| AI confidence ≥ `HIGH_CONFIDENCE_THRESHOLD` (0.85) | Logged + merchant learned with `source="auto"`. `map_origin="ai-auto (NN%)"`. |
| Otherwise, or no merchant name | **Ask** in Telegram with up to 4 buttons + "Pick another...". Saved to hidden `Pending` tab. |
| Ask couldn't be sent | Logged to `Other (Daily)` with ` [auto-fallback: notify failed]` appended to the note. `map_origin="fallback"`. |
| `log_expense` failed (tab/category missing) | Telegram "Could not write to sheet". **Nothing recorded.** |

**Asks today:** the `Pending` tab columns are `pending_id | merchant_normalized | merchant_raw | amount_ils | txn_date_iso | sheet_note | candidates_json | created_at` (`created_at` = Unix seconds). When an ask is older than 24h, its row is **deleted without logging** the next time a new ask is stored or the bot restarts (`store_pending`, `preload_pending_asks`). Those expenses are lost. Phase A fixes this.

**Answering an ask** (`callbacks._commit_txn`): `log_expense(category, ask.amount_ils, ask.sheet_note, dt=txn_date)` → `pop_pending` (deletes the Pending row) → `append_to_history` → `learn_merchant(merchant_normalized, category, source="user")`.

**Merchants tab** (hidden): `merchant_normalized | category | source | first_seen | last_seen | hits`; upsert logic in `sheets.upsert_merchant_mapping` (`source="user"` always wins over `"auto"`). The bot holds an **in-memory cache** of this tab (`parsing/merchant_map.py`) that is loaded once and never refreshed.

**`/delete`** uses `expense_history.json` on Render's local disk (last 10 entries, wiped on every deploy/restart). It removes note lines with `line.startswith(timestamp)`, so it removes *every* line logged in the same minute, in that cell.

---

## 3. Phase A — bot changes (repo `ExpenseBot-V3.0`)

Keep each change small and covered by a test in `scripts/` (follow the style of `scripts/test_ingest.py`, which uses `scripts/ingest_stubs.py`). Ship A1–A5 together, then A6 separately.

### A1. Transactions ledger tab (new hidden tab `Transactions`)
Created lazily like `Merchants`/`Pending` (hidden, header row). One row per `process_ingest` call that got past the "is this a transaction SMS at all" check (i.e. skip only the "no charge/decline/refund anchor" messages).

Columns (A→R), all written with `valueInputOption=RAW`:

| Col | Name | Content |
|---|---|---|
| A | `txn_id` | `uuid4().hex[:12]`, unique |
| B | `received_at` | ISO local time, Asia/Jerusalem, e.g. `2026-10-15T14:03:22+03:00` |
| C | `source` | `sms` (bot ingest) or `mcp_manual` (Phase B manual log) |
| D | `card_last4` | e.g. `1234`, may be empty |
| E | `merchant_raw` | as in the SMS |
| F | `merchant_normalized` | may be empty |
| G | `amount_ils` | signed number (refund negative), empty if unknown (FX failure) |
| H | `currency` | e.g. `ILS`, `USD` |
| I | `amount_native` | number in original currency |
| J | `txn_date` | `YYYY-MM-DD`, empty if the SMS had none |
| K | `status` | see state table below |
| L | `category` | category logged under, empty if not logged |
| M | `category_origin` | `map_user`, `map_auto`, `map_fuzzy`, `ai_auto`, `fallback`, `telegram_user`, `claude_user` |
| N | `pending_id` | for asks |
| O | `sheet_tab` | tab title written to (e.g. `1026`) |
| P | `note_line` | the **exact full note line** written, e.g. `2026-10-15 14:03  [card 1234] SHUFERSAL 86.5 ILS on 2026-10-15` |
| Q | `detail` | free text: error message, skip reason, raw SMS excerpt (max 300 chars) for parse errors |
| R | `updated_at` | ISO local time of last change |

`status` values and who sets them:

| status | Set by | Meaning |
|---|---|---|
| `logged` | bot | Written to the sheet automatically (map/AI/fallback). |
| `asked` | bot | Ask sent, waiting for a category. |
| `settled` | bot (Telegram tap) or MCP | An ask or error was logged with a category Omer chose. |
| `confirmed` | MCP | Omer reviewed an auto-categorized `logged` row and kept the category. |
| `skipped` | MCP | Omer decided not to log it (reason in `detail`). |
| `deleted` | bot `/delete` or MCP undo | Entry removed from the sheet after being logged. |
| `error` | bot | Couldn't log: parse failure, FX failure, or `log_expense` failed. |
| `duplicate` | bot | Dedupe hit. |
| `ignored` | bot | Ignored card or declined. |

`category_origin` mapping from existing code: exact map hit with merchant `source=user` → `map_user`; exact hit with `source=auto` → `map_auto`; fuzzy hit → `map_fuzzy`; `ai-auto` branch → `ai_auto`; notify-failed fallback → `fallback`; Telegram tap → `telegram_user`.

Rules:
- Ledger writes are **best effort**: wrap them in try/except, log a warning, never block or fail the expense flow.
- To update an existing row, find it by `txn_id` in column A, then `values().update` the single row. Never delete ledger rows.
- Store `txn_id` on the `PendingAsk` and add a 9th Pending column `txn_id` (append to `_PENDING_HEADERS`; old rows without it remain readable).
- To get `note_line`, `log_expense` must return the full line it wrote. Add a field `note_line: str` to `LogResult` (it's `_build_note_line(original_text, timestamp)`).

### A2. Asks no longer expire
- Remove the 24h deletion in `store_pending` and `preload_pending_asks`. Pending rows stay until settled (by tap or by MCP).
- Keep evicting the **in-memory** copy after 24h (memory hygiene), but **never** delete the sheet row on expiry.
- Old Telegram buttons keep working at any age: `reload_pending_from_sheet` already doesn't check age.
- Change `_EXPIRED_MSG` (`handlers/callbacks.py`) to: `"This one was already handled (in Claude or another tap). Nothing was logged twice."`

### A3. A Telegram tap must re-check before logging
In `callbacks._commit_txn`, before `log_expense`: re-read the Pending tab (`read_pending_asks`) and confirm the `pending_id` row still exists. If it doesn't, drop the in-memory copy and reply with `_EXPIRED_MSG`. This prevents a double log when the MCP settled it first. After a successful log, set the ledger row (`txn_id` from the ask) to `status=settled`, `category`, `category_origin=telegram_user`, `sheet_tab`, `note_line`.

### A4. Merchant cache refresh
In `parsing/merchant_map.py`, reload the cache from the sheet when it is older than 5 minutes (store a load timestamp next to `_cache`). This way a merchant Omer categorizes through Claude is recognized by the bot on the next SMS, without a restart.

### A5. `/delete` updates the ledger
When `commands.delete` removes an entry, find ledger rows with the same `sheet_tab` and a `note_line` that starts with the deleted `timestamp` and whose text equals the history entry's `original_text`, and set them to `status=deleted`. Best effort.

### A6. Timezone check (verify first, then decide)
The bot uses `datetime.now()`, which on Render is **UTC unless the service has a `TZ` env var**. The builder must check: compare the newest entry timestamp in the sheet with the real time of that purchase (ask Omer). If timestamps are UTC, ask Omer before adding `TZ=Asia/Jerusalem` to the bot's Render env. Note that this also changes "today" for the bot's default month at 00:00–03:00. The MCP always uses Asia/Jerusalem (§7).

---

## 4. Phase B — MCP server architecture

### 4.1 Repo layout (`omer-argaman/expense-mcp`, private)
```
expense_mcp/
  server.py          # MCPServer instance, tool registration, ASGI app, auth middleware
  config.py          # env vars (§8), constants, TZ = ZoneInfo("Asia/Jerusalem")
  sheets_client.py   # Google API wrapper (one cached service object) — the ONLY module that calls Google
  tabs.py            # copied verbatim: _candidate_tab_names, _normalize, find_tab_in_tabs; plus ensure_month_tab
  category_map.py    # copied verbatim from ExpenseBot-V3.0/parsing/category_map.py (header comment: source commit)
  notes.py           # parse note → entries; build line; remove exact line
  ledger.py          # read/update Transactions, Pending, Merchants tabs
  writes.py          # log / remove / move operations (§6), all under one write lock
  reads.py           # summary, category details, search, compare
  review.py          # review-item computation (§5.1)
  errors.py          # ToolError + codes (§9)
tests/
  fake_sheet.py      # in-memory fake of the Sheets API surface used (§10)
  test_*.py
scripts/smoke_test.py
requirements.txt     # mcp (v2), google-api-python-client, google-auth, uvicorn, starlette
render.yaml          # optional
README.md
```
Do **not** import from the bot repo at runtime. Copy the listed functions and note the source commit in a comment.

### 4.2 Runtime
- Python 3.12, official MCP Python SDK v2: `from mcp.server.mcpserver import MCPServer`. Streamable HTTP transport mounted at `/mcp`. Stateless HTTP mode with JSON responses if the SDK offers it (Composio doesn't need sessions).
- Served by `uvicorn` with **1 worker** (the write lock is in-process). Bind `0.0.0.0:$PORT`.
- `GET /healthz` → `200 {"ok": true}` with **no auth and no Google call** (for Render health checks).
- All Google calls are synchronous; run them in a thread (`anyio.to_thread.run_sync`) from async tools.
- One process-wide `threading.Lock` (`WRITE_LOCK`) around every write operation, from the first read of C to the final verification.
- Tab-metadata cache: 60s TTL, invalidated after any tab creation.

### 4.3 Concurrency with the bot (accepted risk, mitigated)
The bot and the MCP both do read-modify-write on C and its note, with no shared lock. A collision needs an SMS to arrive within the same ~1s on the same category. Mitigation: every write ends with a **verify step**. Re-read C and the note; confirm the note contains the new line and C equals `old + amount`. If not, return a `WRITE_VERIFY_FAILED` error (§9) with what was observed and **do not retry automatically**. The ledger row is left unchanged, so the item reappears in the next review.

---

## 5. Tools

Conventions for all tools:
- `month` is a string `"YYYY-MM"`; when omitted → the current month in Asia/Jerusalem. Claude resolves "last month", "September" and so on before calling. Every response includes `"today": "YYYY-MM-DD"` (Asia/Jerusalem) so Claude can resolve relative dates correctly.
- `category` input is matched case-insensitively against `CATEGORY_MAP` keys. A non-exact input is an error (`UNKNOWN_CATEGORY`) listing the valid names. **No fuzzy guessing on writes.**
- Amounts are ILS numbers. Outputs use plain numbers (no `₪`), rounded to 2 decimals.
- Errors: tools raise an MCP tool error whose text is JSON `{"code": "...", "message": "...", "details": {...}}` (§9).
- Every tool description (the docstring seen by Claude) must include the key rule for that tool. These texts are part of the spec, given in each section below.

### 5.1 Review & settle (the daily-review core)

#### `get_review_items` — READ-ONLY
Purpose: everything that needs Omer's decision. The daily review starts with this.

Inputs:
| name | type | req | default | notes |
|---|---|---|---|---|
| `days` | int | no | 30 | Look-back window by `received_at` for the `auto_categorized` and `missing_in_sheet` kinds. `unanswered` and `failed` are **always returned regardless of age**. 1–180. |

Item kinds (computed from the ledger + Pending + sheet):
| kind | Condition |
|---|---|
| `unanswered` | ledger `status=asked`, **or** a Pending row with no ledger row (asks created before A1 shipped). |
| `failed` | ledger `status=error`. |
| `fallback` | ledger `status=logged` and `category_origin=fallback`. |
| `auto_categorized` | ledger `status=logged` and `category_origin` in (`ai_auto`, `map_auto`, `map_fuzzy`), within `days`. |
| `missing_in_sheet` | ledger `status` in (`logged`,`settled`,`confirmed`), within `days`, but `note_line` is not found in the note of that category row on `sheet_tab`. `hint` says the note may have been trimmed if the note has ≥50 lines. |

Output:
```json
{
  "today": "2026-10-15",
  "counts": {"unanswered": 2, "failed": 1, "fallback": 0, "auto_categorized": 3, "missing_in_sheet": 0},
  "items": [
    {
      "item_id": "t:3f9a1c2b7d10",          // "t:<txn_id>" or "p:<pending_id>" for Pending rows with no ledger row
      "kind": "unanswered",
      "txn_date": "2026-10-12",
      "received_at": "2026-10-12T21:40:05+03:00",
      "age_days": 3,
      "merchant": "BAR MITZI TLV",
      "amount_ils": 86.0,
      "is_refund": false,
      "card_last4": "1234",
      "current_category": null,
      "suggested_categories": ["Beer / Wine", "Dining Out", "Entertainment", "Other (Daily)"],
      "note_text": "[card 1234] BAR MITZI TLV 86 ILS on 2026-10-12 21:39",
      "detail": "",
      "allowed_actions": ["log", "skip"]
    }
  ]
}
```
Order: `unanswered`, `failed`, `fallback`, `missing_in_sheet`, `auto_categorized`; within a kind, oldest `txn_date` first.
`allowed_actions` per kind: unanswered/failed → `log`, `skip`; fallback/auto_categorized → `confirm`, `recategorize`, `skip`; missing_in_sheet → `log` (re-log), `skip`.
`suggested_categories` comes from Pending `candidates_json`, or the logged category for logged rows, or `[]`.
For `failed` items with an FX failure, `amount_ils` is `null` and `detail` says so; settling requires `amount_ils` (below).

Description text: *"Lists card transactions that need Omer's decision: unanswered category questions (any age), failures, fallbacks, AI auto-categorizations and entries missing from the sheet. Present each item and ask Omer which category. Never choose a category yourself."*

#### `settle_item` — WRITE
Purpose: resolve one review item with Omer's decision.

Inputs:
| name | type | req | default | notes |
|---|---|---|---|---|
| `item_id` | string | yes | | From `get_review_items`. |
| `action` | enum `log` \| `skip` \| `confirm` \| `recategorize` | yes | | Must be in the item's `allowed_actions`. |
| `category` | string | for `log`, `recategorize` | | Exact category name. |
| `remember_merchant` | bool | no | `true` | `true` saves `merchant_normalized → category` with `source="user"` (only if the merchant name is non-empty). Set `false` when Omer says it's a one-off. |
| `amount_ils` | number | only for `failed` items whose amount is unknown | | Positive. Sign follows the item's refund flag. |
| `reason` | string | for `skip` | | Stored in ledger `detail`, e.g. "reimbursed by work". |

Behavior per action (all under `WRITE_LOCK`; re-read the item's ledger/Pending row first and fail with `ALREADY_SETTLED` if its state changed since listing):
- **log**: write the entry exactly like `sheets.log_expense` (§2.2) with `original_text = note_text` (the stored `sheet_note`), `amount = amount_ils` (signed), month from `txn_date` (if empty → month of `received_at`), timestamp = now in Asia/Jerusalem. Then: delete the Pending row (if any), set ledger `status=settled`, `category`, `category_origin=claude_user`, `sheet_tab`, `note_line`, `updated_at` (create the ledger row if the item came from a `p:` id). Then remember the merchant if requested. For `missing_in_sheet`, re-log the same `note_text` with a new timestamp.
- **skip**: no sheet write. Delete the Pending row (if any). Ledger `status=skipped`, `detail=reason`. If the item was `logged`/`fallback`/`auto_categorized`, **first remove its entry** (see `recategorize` step 1), then set `skipped`.
- **confirm**: no sheet write. Ledger `status=confirmed`, `category_origin=claude_user`. If `remember_merchant`, upsert the merchant with `source="user"` (this upgrades an `auto` mapping).
- **recategorize** (move): (1) remove the old entry: in `sheet_tab`, on the old category row, subtract `amount_ils` from C and delete **exactly one** note line equal to `note_line` (full-line equality, last occurrence). If the line isn't found → `ENTRY_NOT_FOUND`, change nothing. (2) log to the new category as in **log**. (3) Ledger: `category`, `note_line` (new), `category_origin=claude_user`, `status=settled`. If step 2 fails after step 1 succeeded, immediately re-log the old entry under the old category, then return the error.

Output:
```json
{"today":"2026-10-15","item_id":"t:3f9a1c2b7d10","result":"logged",
 "category":"Beer / Wine","amount_ils":86.0,"sheet_tab":"1026",
 "category_total_after":412.0,"merchant_remembered":true,
 "note_line":"2026-10-15 16:04  [card 1234] BAR MITZI TLV 86 ILS on 2026-10-12 21:39"}
```
`result` ∈ `logged`, `skipped`, `confirmed`, `moved`.

Description text: *"Settles one review item. Only call with a category Omer explicitly chose in this conversation. Never guess or pick one yourself, even when the suggestion looks obvious."*

#### `undo_settle` — WRITE
Purpose: undo the last `settle_item` on an item (wrong button, typo).

Inputs: `item_id` (string, required).
Allowed only if ledger `category_origin=claude_user` and `status` in (`settled`, `skipped`, `confirmed`), with `updated_at` within the last 7 days. Otherwise `UNDO_NOT_ALLOWED`.
Behavior: for `settled` → remove the entry (exact `note_line`, subtract amount), then restore the previous state. The ledger stores the previous state as JSON in `detail` under key `prev` at settle time (`{"prev": {"status": "...", "category": "...", "note_line": "...", "category_origin": "..."}}`). If the prev state was a logged entry (recategorize), re-log it under the old category. If it was an ask, re-create its Pending row from the ledger fields (same `pending_id`, `candidates_json` from `prev`). Merchant memory is **not** reverted. Instead, the response says `"merchant_mapping_kept": "<merchant> → <category>"` so Claude can offer `set_merchant_category`.
Output: `{"today":..., "item_id":..., "restored_status":"asked"}`.

### 5.2 Read-only questions

#### `list_categories` — READ-ONLY
No inputs. Output: `{"sections": {"Home": ["Rent", ...], ...}, "unsectioned": ["Education","Emergency Fund","Omer","Gil"], "loggable": [...25 names...], "not_loggable": ["Mortgage"]}`. Built from `category_map.py` only (no Google call).

#### `get_month_summary` — READ-ONLY
Inputs: `month` (string `YYYY-MM`, optional).
One read of `A1:D200`. Output:
```json
{"today":"...","month":"2026-09","tab":"0926",
 "sections":[{"name":"Daily Living","budget":3000,"spent":3412.5,"remaining":-412.5,"over_budget":true,
              "categories":[{"name":"Groceries","budget":2000,"spent":2100,"remaining":-100,"over_budget":true}, ...]}],
 "totals":{"budget":...,"spent":...,"remaining":...},
 "over_budget":["Daily Living","Groceries"]}
```
Section numbers come from the section total row (§2.1); category numbers from each category's row. `over_budget` = remaining < 0. Missing tab → `MONTH_TAB_NOT_FOUND` (reads **never create tabs**).

#### `get_category_details` — READ-ONLY
Inputs: `category` (string, required), `month` (optional).
Output: budget/spent/remaining + `entries` parsed from the note:
```json
{"category":"Coffee","month":"2026-10","budget":200,"spent":145,"remaining":55,
 "entries":[{"logged_at":"2026-10-03 08:12","text":"coffee 45 oat milk","amount_guess":45.0,"is_refund":false}],
 "entries_sum_guess":145.0,"note_trimmed_possible":false}
```
Entry parsing (`notes.py`): split the note on `\n`, skip blank lines; regex `^(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s{2}(.*)$`. Non-matching lines → `logged_at: null, text: line`. `amount_guess`: if the text matches `(-?\d+(?:\.\d+)?) ILS` use that (negative if the text contains `REFUND:`); else the first standalone number in the text (`(?<![\w.])\d+(?:\.\d+)?(?![\w.])`); else `null`. `note_trimmed_possible` = the note has ≥ 50 lines. **C (spent) is authoritative**; entries are best-effort detail.

#### `search_entries` — READ-ONLY
Purpose: "what did I buy at X", "recent entries", "all bar expenses since July".
Inputs:
| name | type | req | default |
|---|---|---|---|
| `text` | string | no | null — case-insensitive substring match on entry text |
| `category` | string | no | null |
| `month_from` | string `YYYY-MM` | no | current month |
| `month_to` | string `YYYY-MM` | no | = `month_from` |
| `limit` | int 1–200 | no | 50 |
Max 12 months per call (`RANGE_TOO_LARGE`). Per month: one `A1:A200` read + one notes read of `C1:C200` (as `ai_handler._run_get_all_transactions` does). Output: entries `{month, category, logged_at, text, amount_guess}` sorted newest `logged_at` first, plus `{"total_matches": n, "amount_guess_sum": x, "months_missing": ["2026-02"]}`.

#### `compare_months` — READ-ONLY
Inputs: `months` (array of 2–12 `YYYY-MM` strings, required), `level` (enum `section` | `category`, default `section`).
Output: one row per section or category with spent per month, plus budget per month, e.g. `{"rows":[{"name":"Daily Living","spent":{"2026-08":2900,"2026-09":3412.5},"budget":{...}}], "months_missing":[]}`.

#### `list_merchants` — READ-ONLY
Inputs: `text` (optional substring), `category` (optional), `limit` (default 50).
Output: rows from the Merchants tab: `{merchant, category, source, first_seen, last_seen, hits}`.

### 5.3 Other writes

#### `set_merchant_category` — WRITE
Purpose: change what a merchant maps to **for future SMSes** (the missing `/fix`). It doesn't touch past entries; to move a past one, use `settle_item` → `recategorize` if it's a review item, or say so.
Inputs: `merchant_normalized` (string, required, must exist in the Merchants tab unless `create=true`), `category` (required), `create` (bool, default false).
Behavior: mirror `sheets.upsert_merchant_mapping(..., source="user")`. Output: the row after the change.

#### `log_manual_expense` — WRITE (low priority, build last)
Purpose: "add 45 coffee yesterday, oat milk".
Inputs:
| name | type | req | default | notes |
|---|---|---|---|---|
| `category` | string | yes | | exact name |
| `amount_ils` | number | yes | | > 0 |
| `note` | string | no | "" | free text |
| `date` | string `YYYY-MM-DD` | no | today (Asia/Jerusalem) | picks the month tab; not more than 1 day in the future |
| `is_refund` | bool | no | false | writes a negative amount |
Note text, mirroring the Telegram format "what amount note": `"{category} {amount:g} {note}".strip()`, then ` on {date}` if `date` ≠ today, and the prefix `REFUND: ` if `is_refund`. Example line: `2026-10-15 09:12  Coffee 45 oat milk on 2026-10-14`.
Also creates a ledger row (`source=mcp_manual`, `status=settled`, `category_origin=claude_user`) so `undo_settle` works on it (`item_id` returned).

### 5.4 Deliberately not included
- No generic "delete any entry" tool. Removal only happens through the ledger (exact `note_line`), so bot-made Telegram-text entries are never touched.
- No budget editing, no tab renaming, no Format edits.

---

## 6. Write safety (rules every write follows)

1. **Only through the ledger.** The MCP removes or moves only entries whose exact `note_line` is in a ledger row. It never edits a note line it didn't find by full-line equality, and it removes only one occurrence. (The bot's `/delete` uses `startswith(timestamp)`; the MCP must not.)
2. **Exact bot format.** C gets a plain number via `USER_ENTERED`. Note lines are `YYYY-MM-DD HH:MM` + two spaces + text. Same 12,000-char / last-50-lines trim. Same Format duplication with `MMYY` name.
3. **Missing month tab on write**: create it from `Format`, as the bot does. If `Format` is missing → `FORMAT_TAB_MISSING`, nothing written. Reads never create tabs.
4. **Category row missing in that month's tab** → `CATEGORY_NOT_IN_TAB` (lists the tab name), nothing written.
5. **Refunds**: amounts in the ledger/Pending are already signed. The MCP writes them as-is. Refund note text keeps the bot's `REFUND:` marker. `log_manual_expense` uses `is_refund`.
6. **Currency**: ILS only. FX-failed items need Omer to give the ILS amount (`amount_ils` on `settle_item`). Claude may not compute it on its own.
7. **Verify after write** (§4.3). Write the ledger **after** a successful sheet write and verify. If the ledger write fails, return success with `"warning": "ledger_update_failed"`; the next review then shows the item as stale and Claude must tell Omer.
8. **Idempotency**: `settle_item` re-reads state under the lock and refuses with `ALREADY_SETTLED` if the item isn't in a state that allows the action. So a retried call can't double-log.
9. **Never log without Omer's choice**: enforced through tool descriptions plus the daily-review prompt (§11). The server can't verify it, so the descriptions must say it plainly.

---

## 7. Dates and timezone
- All MCP "now"/"today" values use `ZoneInfo("Asia/Jerusalem")`, including note timestamps, `received_at`/`updated_at`, defaults for `month`/`date`, and `age_days`.
- The month tab for a settle is the **transaction date's** month, not today's (a Sept 30 purchase settled on Oct 3 goes to the Sept tab). Same as the bot.
- Relative words ("yesterday", "last month", "September") are resolved by Claude using `today` from any tool response. If a month name is given without a year, use the most recent past or current occurrence. The server only accepts explicit `YYYY-MM` / `YYYY-MM-DD`.
- Input validation: `month` must match `^\d{4}-(0[1-9]|1[0-2])$`; `date` must be a real date.

---

## 8. Auth and secrets
Env vars on the MCP Render service:
| var | content |
|---|---|
| `GOOGLE_CREDENTIALS` | same service-account JSON as the bot (copy from the bot's Render env) |
| `SPREADSHEET_ID` | real sheet ID (or the **copy's** ID during smoke testing) |
| `MCP_API_KEY` | static bearer key |
| `MCP_API_KEY_NEXT` | optional; also accepted (for rotation) |
| `LOG_LEVEL` | default `INFO` |

- Auth middleware on everything except `/healthz`: header `Authorization: Bearer <key>`. Compare with `hmac.compare_digest` against `MCP_API_KEY` and (if set) `MCP_API_KEY_NEXT`. Missing/wrong → `401 {"error":"unauthorized"}`. Startup fails if `MCP_API_KEY` is unset or shorter than 32 chars.
- Generate the key: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Store it only in Render env and Composio.
- Rotate: set `MCP_API_KEY_NEXT`=new → deploy → update Composio to the new key → test → move new into `MCP_API_KEY`, clear `_NEXT` → deploy.
- Never log headers, the key, credentials, or full note contents. Log per call: tool name, duration, result code, and for writes `item_id`/category/amount.
- `.gitignore`: `.env`, `*.json` credentials, `__pycache__`. No secrets in `render.yaml` (use `sync: false`).

---

## 9. Error handling
Error JSON: `{"code": str, "message": str (one sentence Claude can relay), "details": {...}}`.

| code | when | details |
|---|---|---|
| `BAD_INPUT` | invalid month/date format, negative amount, missing required field for action | `field` |
| `UNKNOWN_CATEGORY` | category not in `CATEGORY_MAP` | `valid_categories` |
| `MONTH_TAB_NOT_FOUND` | read on a month with no tab | `month`, `existing_tabs` (first 24 titles) |
| `FORMAT_TAB_MISSING` | write needs a new month but `Format` is gone | `month` |
| `CATEGORY_NOT_IN_TAB` | category row not found in the month tab | `tab`, `category` |
| `ITEM_NOT_FOUND` | unknown `item_id` | |
| `ACTION_NOT_ALLOWED` | action not in item's `allowed_actions` | `allowed_actions` |
| `ALREADY_SETTLED` | item state changed since listing | `current_status` |
| `ENTRY_NOT_FOUND` | exact `note_line` not in the cell (was removed or edited by hand) | `tab`, `category`, `note_line` |
| `AMOUNT_REQUIRED` | FX-failed item settled without `amount_ils` | |
| `UNDO_NOT_ALLOWED` | not an MCP settle, or older than 7 days | |
| `RANGE_TOO_LARGE` | > 12 months | |
| `WRITE_VERIFY_FAILED` | post-write check mismatch | `expected`, `observed` |
| `GOOGLE_API_ERROR` | `HttpError` after retries | `status` |

Google errors: retry 429 and 5xx up to 3 times with exponential backoff (1s, 2s, 4s) **for reads only**. For writes, retry only the first read of C. Never retry the C write or note write blindly; instead go through verify (§4.3). 403/404 on the spreadsheet → `GOOGLE_API_ERROR` with message "service account can't open the spreadsheet; check SPREADSHEET_ID and sharing".

---

## 10. Testing plan

### 10.1 Unit tests (pytest, no network)
`tests/fake_sheet.py` implements the subset of `googleapiclient` used: `spreadsheets().get(...)` (metadata + notes field mask), `values().get/update/append`, `batchUpdate` (`updateCells` note, `duplicateSheet`, `addSheet`, `deleteDimension`). It must return the same nested shapes as the real API, including missing keys for empty cells and no trailing empty rows. Seed fixture: `Format`, `0926`, `1026` with the four sections and all categories (mirror `BROAD_CATEGORIES` layout: header, categories, total row), plus `Merchants`, `Pending`, `Transactions` tabs.

Required cases:
1. Tab matching for every supported name format + the punctuation-normalized pass; `0246` ≠ `0426`.
2. Log on a missing month creates `MMYY` from `Format`; `Format` missing → `FORMAT_TAB_MISSING`, nothing written.
3. Log format byte-for-byte equals what `sheets.log_expense` produces (same note, same C), including refund negative and the 12,000-char/50-line trim.
4. `settle_item log` on an ask: C updated, note line appended, Pending row deleted, ledger `settled`, merchant upserted `source=user`; second call → `ALREADY_SETTLED`.
5. `p:` item (Pending without ledger row) creates a ledger row.
6. Recategorize removes **only** the exact line when two lines share the same minute timestamp; C on both rows correct; failure in step 2 restores the old entry.
7. `skip` on a logged item removes its entry; `confirm` upgrades `auto` merchant to `user`.
8. `undo_settle` restores an ask (Pending row back with same `pending_id`) and a moved entry.
9. Review kinds: each kind detected; `unanswered`/`failed` ignore `days`; `missing_in_sheet` detection.
10. Entry parsing incl. non-matching lines, `REFUND:`, `orig USD`, number heuristics.
11. Summary section totals via the header + n + 1 rule; `Mortgage` row counted in Home's n.
12. Auth: no header / wrong key → 401; `MCP_API_KEY_NEXT` accepted; `/healthz` open.
13. Verify failure path returns `WRITE_VERIFY_FAILED` (simulate a concurrent write in the fake).

Phase A bot changes get their own tests in the bot repo: ledger rows written for each `process_ingest` outcome; asks survive >24h; tap after MCP settle logs nothing; merchant cache reload after 5 min.

### 10.2 Live smoke test against a COPY of the sheet
1. In Google Drive: make a copy of the real sheet ("ExpenseBot COPY – smoke"), share it with the service account's `client_email` as Editor.
2. Run locally with `SPREADSHEET_ID=<copy id>`: `python scripts/smoke_test.py`. The script refuses to run if `SPREADSHEET_ID` equals the value in env var `REAL_SPREADSHEET_ID` (set it locally to the real ID as a guard).
3. The script: lists categories; summary of the current month; seeds one fake ask (Pending + ledger row) for 1 ILS; `get_review_items` shows it; `settle_item log` to `Other (Daily)` with `remember_merchant=false`; checks C +1 and the note line; `undo_settle`; checks C and note restored and Pending row back; `skip` it with reason "smoke"; `log_manual_expense` 1 ILS + `undo_settle`. Prints PASS/FAIL per step.
4. Open the copy in the browser and eyeball the note on the touched cell.
5. Then deploy to Render still pointing at the copy and repeat steps 3–4 **from Claude via Composio**. Only then switch `SPREADSHEET_ID` to the real sheet.

---

## 11. Phase C — daily review at 16:00

### Mechanism
A **Claude scheduled task** (claude.ai cloud), set up after the MCP works from Claude:
- Schedule: every day 16:00 Asia/Jerusalem (`CRON_TZ=Asia/Jerusalem 0 16 * * *`).
- Each run starts a **fresh session** with the Composio connector attached and a push notification to Omer's phone when there's something to settle. The session appears in the Claude app, and Omer replies there.
- Fallback if scheduled sessions can't use the Composio connector: Omer opens any Claude chat and says "daily review". The prompt below works the same way.

### Prompt for the scheduled task (store verbatim)
> Run my daily expense review using the expense MCP (through Composio).
> 1. Call `get_review_items` (default window). If there are no items, reply with one line, "Nothing to settle today ✅", and stop.
> 2. Otherwise show me a short numbered list grouped by kind: date, merchant, amount, and the bot's suggestions if any. Unanswered and failed items come first.
> 3. Ask me what to do with each one. I choose every category myself. Never pick or assume a category, even an obvious one. Accept short answers like "1 beer, 2 groceries, 3 skip – reimbursed, 4-6 ok".
> 4. For each answer call `settle_item` (`ok` on an auto item = `confirm`). Use `remember_merchant=true` unless I say it's a one-off.
> 5. Finish with a one-line recap: how many were logged, skipped and confirmed, plus any errors in plain words. If a tool returns an error, don't retry writes. Tell me and move on.

---

## 12. Deployment and launch checklist

**Phase A (bot)**
1. Implement A1–A5 on a branch of `ExpenseBot-V3.0` + tests. Merge → Render auto-deploys the bot.
2. Send a real small purchase (or replay a stored SMS via `scripts/test_ingest.py` against a test setup) and check that a `Transactions` row appears.
3. Do A6: check the timezone and ask Omer before changing anything.

**Phase B (MCP)**
1. GitHub: create **private** repo `omer-argaman/expense-mcp`. Push the code.
2. Locally: `pytest` green; smoke test against the **copy** (§10.2) green.
3. GitHub → Settings → Applications → Installed GitHub Apps → Render → Configure → add `expense-mcp` to the repo access list.
4. Render → New → Web Service → `expense-mcp`; Runtime Python; Build `pip install -r requirements.txt`; Start `uvicorn expense_mcp.server:app --host 0.0.0.0 --port $PORT --workers 1`; Health check path `/healthz`; Instance: Free.
5. Env vars: `GOOGLE_CREDENTIALS` (copy from bot service), `SPREADSHEET_ID` = **copy** ID, `MCP_API_KEY` (generated, §8). Deploy.
6. `curl https://<service>.onrender.com/healthz` → 200. `curl -X POST .../mcp` without auth → 401.
7. Composio: dashboard.composio.dev → Add Custom MCP → URL `https://<service>.onrender.com/mcp`, auth "API key", header `Authorization`, prefix `Bearer`, value = key. Check that the tool list shows all tools.
8. From a Claude chat (Composio connector on): "list my expense categories", "summary for this month", then settle a seeded 1 ILS test item on the copy and undo it. Check the copy in the browser.
9. Switch Render `SPREADSHEET_ID` to the **real** sheet → redeploy. In Claude: run the review prompt (§11) once by hand; settle one real item; confirm in Telegram/sheet that `/summary` and `/category` show it.
10. Free tier note: the service sleeps after ~15 min idle, so the first call can take 30–60s. If Composio times out on cold starts, upgrade to the smallest paid instance (or add an uptime ping to `/healthz`).

**Phase C**
11. Create the 16:00 scheduled task with the §11 prompt and the Composio connector. Trigger it once manually and check the push and the conversation.

---

## 13. Open questions for Omer (with defaults the builder uses unless told otherwise)

1. **Auto-categorized items in the daily review.** You said you want to choose categories yourself. Today the bot also auto-logs new merchants when the AI is ≥85% sure, and on fuzzy name matches. Default: these show up in the review as `auto_categorized` for a quick "ok" (last 30 days). Alternative: leave them out.
2. **Timezone of existing note timestamps (A6).** Are they Israel time or UTC (3 hours behind in summer)? Default: don't change the bot until checked.
3. **Old asks lost before Phase A.** These can't be recovered automatically. If you remember roughly when you stopped answering, Claude can help you compare your card statement against the sheet manually, once.
4. **Pre-ledger asks still in Pending.** Any ask younger than 24h at the moment Phase A ships will survive and appear as `p:` items. Default: yes, include them.
5. **Repo name** `expense-mcp` and Render service name: OK, or another name?
6. **Review window**: 30 days for `auto_categorized`/`missing_in_sheet`. OK?
