# SMS / Email expense pipeline — setup guide

This document is the one-time wiring for the credit-card-driven pipeline.
Once these steps are done, swiping a card -> Isracard email -> Render
server -> Google Sheet logs the expense. You only ever interact with the
bot when a brand-new merchant appears AND the AI's confidence is below the
threshold.

Code: see `parsing/isracard_parser.py`, `handlers/transaction_handler.py`,
`server.py`, `integrations/gmail_apps_script.gs`.

## 1. Render env vars

Add the following to your existing Render service's Environment tab:

| Name                       | Required | Example / default | Notes |
|----------------------------|----------|-------------------|-------|
| `INGEST_SECRET`            | yes      | `<long random string>` | Shared with the Apps Script. Rotate by changing both ends together. |
| `OWNER_CHAT_ID`            | recommended | `123456789` | Get it by sending `/whoami` to the bot once after deploy. If unset, falls back to the first id in `subscribers.json`. |
| `CARD_PRIORS`              | optional | see below | JSON map of last4 -> card metadata. Defaults are in `parsing/card_registry.py`. |
| `HIGH_CONFIDENCE_THRESHOLD`| optional | `0.85`            | Below this the bot asks via Telegram instead of auto-logging. Use `0.99` for "always confirm" mode while training. |
| `ALLOWED_ISSUERS`          | optional | `isracard`        | Comma-separated. Reject any other issuer at the door. |

The default `CARD_PRIORS` already has 0347 (clothing prepaid), 4888 (food
prepaid), and 4881 (general credit). Override only if your priors change:

```json
{
  "0347": {
    "name": "Isracard prepaid - clothing/appliances",
    "is_prepaid": true,
    "category_prior": ["Other (Daily)", "Maintenance/Improvements"]
  },
  "4888": {
    "name": "Isracard prepaid - food",
    "is_prepaid": true,
    "category_prior": ["Groceries", "Dining Out", "Coffee", "Beer / Wine"]
  },
  "4881": {
    "name": "Isracard credit - general",
    "is_prepaid": false,
    "category_prior": []
  }
}
```

`PORT` is already set by Render automatically; you don't need to touch it.

## 2. Get your Telegram chat id

Deploy the new code, send `/whoami` to your bot, copy the number it replies
with, and put it in `OWNER_CHAT_ID` on Render. (If you skip this step the
pipeline falls back to the first chat id in `subscribers.json`, which is
fine in a single-user setup but explicit is safer.)

## 3. Enable Isracard email alerts

In the Isracard app or website, turn on **email** notifications for
transaction approvals. SMS can stay on too — the dedupe layer prevents
double-logging if the same transaction arrives by both channels during the
transition.

## 4. Gmail setup

1. Create two labels in Gmail:
   - `expense-bot/incoming`
   - `expense-bot/processed`
   (Apps Script will auto-create `expense-bot/error` on the first failure.)
2. Create a Gmail filter:
   - From: `*@isracard.co.il` (adjust once you have a real email and see
     the exact sender)
   - Apply label: `expense-bot/incoming`
   - Optionally: skip the inbox so the inbox stays clean.
3. Open https://script.google.com and create a new project.
4. Replace the default file with the contents of
   [`integrations/gmail_apps_script.gs`](../integrations/gmail_apps_script.gs).
5. **Project Settings → Script Properties** — add:
   - `INGEST_URL` = `https://<your-render-host>/ingest`
   - `INGEST_SECRET` = same value as the `INGEST_SECRET` env var on Render
6. From the Apps Script editor, run `pollIncoming` once manually so Google
   prompts you for permissions (read Gmail, fetch external URLs).
7. **Triggers** (clock icon) → **Add Trigger**:
   - Function: `pollIncoming`
   - Event source: time-driven
   - Type: minutes timer
   - Interval: every 5 minutes (or 1 minute for snappier latency)
8. Optional: run `testIngest` in the Apps Script editor — it POSTs a
   hand-built sample message and you should get a Telegram message asking
   you to categorize "עמותת עטלף". This proves the round-trip works
   without waiting for a real swipe.

## 5. Verify locally before deploying (optional)

The repo contains two stand-alone scripts that don't touch any external
service:

```bash
# Just the parser — runs the 12 real SMS samples through it
python -m scripts.test_isracard_parser

# Full HTTP pipeline — boots the Flask app in-process with stubbed
# Sheets / OpenAI / Telegram and POSTs all 12 samples plus edge cases
python -m scripts.test_ingest
```

Both should report 12/12 OK.

## 6. Deploy

Same as today: push to GitHub, Render auto-deploys. The new dependency
`flask` is in `requirements.txt`. The bot will:

- Start the Flask server on `PORT` (replacing the previous bare HTTP
  health server).
- Continue polling Telegram for messages and slash-command interactions.
- Auto-create a hidden `Merchants` tab in your Google Sheet on the first
  successful auto-learn or button-tap.

## 7. Operating notes

- **First two weeks** — set `HIGH_CONFIDENCE_THRESHOLD` to `0.99` to force
  the bot to ask before every auto-log. Each tap is one merchant added to
  the map. After ~30-50 transactions across distinct merchants you can
  drop the threshold back to `0.85` (or lower) and silent operation
  takes over.
- **Recategorize a mistake** — reply `/delete` (existing command) and
  re-log via Telegram free text the right way. The merchant map will be
  updated the next time that merchant arrives via SMS and you tap a
  different button.
- **New issuer** — add a `parsing/<issuer>_parser.py` and a one-line
  dispatcher; everything else (card registry, merchant map, AI, Telegram
  flow) is issuer-agnostic.
