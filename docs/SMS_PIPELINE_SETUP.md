# SMS / Email expense pipeline — setup guide

This document is the one-time wiring for the credit-card-driven pipeline.
Once these steps are done, swiping a card -> Isracard SMS (or email) ->
Render server -> Google Sheet logs the expense. You only ever interact with
the bot when a brand-new merchant appears AND the AI's confidence is below
the threshold.

Two ingestion channels are supported and can run side-by-side:

  - **iOS Shortcut → SMS** (recommended; section 4A below)
  - **Gmail Apps Script → email** (section 4B below)

Both POST to the same `/ingest` endpoint; everything downstream is identical.
Server-side dedupe handles the case where the same transaction arrives via
both channels.

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
| `IGNORED_CARDS`            | optional | `4888,0347`       | Comma-separated last4 values to skip silently (no Telegram, no sheet). |

Prepaid cards **4888** (food) and **0347** (clothing/appliances) are ignored
by default. The default `CARD_PRIORS` only needs **4881** (general credit).
Override `CARD_PRIORS` only if your priors change:

```json
{
  "4881": {
    "name": "Isracard credit - general",
    "is_prepaid": false,
    "category_prior": []
  }
}
```

`PORT` is already set by Render automatically; you don't need to touch it.

### Memory and persistence (Render)

- **Merchant → category** mappings live in a hidden Google Sheet tab **Merchants**
  (created on first save). They survive bot restarts; check this tab if a merchant
  keeps being re-asked after a crash.
- **Pending category asks** are stored in a hidden **Pending** tab so inline
  buttons still work after a Render restart (until 24h expiry).
- Startup logs `Merchant map preloaded (N entries)` and `Memory RSS: X MB` for
  monitoring. If memory still hits 512 MB, consider a larger Render instance.

## 2. Get your Telegram chat id

Deploy the new code, send `/whoami` to your bot, copy the number it replies
with, and put it in `OWNER_CHAT_ID` on Render. (If you skip this step the
pipeline falls back to the first chat id in `subscribers.json`, which is
fine in a single-user setup but explicit is safer.)

## 3. Enable Isracard transaction notifications

In the Isracard app or website, make sure **SMS** transaction notifications
are on (they're on by default). If you want email as a backup channel, turn
that on too — the dedupe layer prevents double-logging when the same
transaction arrives by both channels.

## 4A. iOS Shortcut (recommended) — SMS direct to /ingest

This is the simplest, fastest, and most reliable path on iOS 17+. The phone
fires the Shortcut the moment an Isracard SMS arrives; the Shortcut POSTs
the message body straight to your Render server; the bot replies with a
confirmation in your normal Telegram chat.

### One-time Shortcut setup

1. Open **Shortcuts** → bottom tab **Automation** → **+** → **Message**.
2. **Sender**: leave empty (some Israeli senders show as a short-code that
   isn't easy to pin); rely on the text filter instead.
3. **Message contains**: `ישראכרט`. (Optional but recommended; the parser
   filters out non-Isracard messages anyway, but this saves a round-trip.)
4. **Run Immediately**: ON. **Notify When Run**: your call (ON gives a
   small "Running automation" banner per fire).
5. Tap **Next** -> **New Blank Automation**.
6. Add these four actions in order:

   1. **Get text from Shortcut Input**
      - The Shortcut Input on a Message trigger is the SMS body.
   2. **URL Encode** (the *Text* variable from step 1; choose action **URL Encode**)
   3. **Expand URL** (also called **Text** -> *URL* in some iOS versions; the action that lets you build a URL with magic variables interpolated):
      ```
      https://<your-render-host>/ingest?secret=<INGEST_SECRET>&issuer=isracard&body=<URL Encoded Text>
      ```
      Replace `<your-render-host>` and `<INGEST_SECRET>` with the real values.
      The `<URL Encoded Text>` is the magic variable from step 2.
   4. **Get Contents of URL** (the URL from step 3)
      - **Method**: POST
      - Headers: none needed
      - Request Body: doesn't matter (everything's in the URL)

That's it. Save the automation.

### Trigger vs. server-side relevance

Your automation trigger can stay broad (e.g. **Message contains** `ישראכרט`).
That catches **marketing** and **reminders** too. On the server we only
continue if the text looks like a **real card event**: it must contain
`ישראכרט` **and** one of:

- `אושרה עסקה` (approved purchase — almost all charges), or  
- a **declined** phrase (`נדחתה`, `לא אושרה`, …), or  
- a **refund / reversal** phrase (`זיכוי`, `בוטלה`, …).

Anything else (promos, “download our app”, generic notices) returns HTTP 200
with `{"status":"ignored","detail":"not_a_transaction_notification"}` —
no sheet write, no Telegram, no OpenAI. You’ll still see one line in Render
logs at INFO level.

**Optional iOS tightening:** you could set **Message contains** to
`ישראכרט אושרה` so fewer irrelevant SMS fire the Shortcut — but you might
miss rare decline/refund wordings. Recommended: keep the broad trigger and
let the server filter noise.

### Verify

1. Send yourself an SMS that mentions Isracard **without** an approval line,
   e.g. `ישראכרט מבצע מיוחד`. `/ingest` should respond with
   `status: ignored` — no Telegram message.
2. Send yourself `ישראכרט אושרה עסקה test` — passes the relevance gate but
   isn’t a parseable charge; you may still get an error-style response or no
   log (depending on parser); that confirms the POST path works.
3. Wait for a real card swipe — Telegram should confirm or ask within seconds.

### Notes on this setup

- **The secret rides in the URL**, which lands in Render's request log.
  HTTPS still encrypts it on the wire. For a personal single-user tool this
  is acceptable; rotate `INGEST_SECRET` on both Render and the Shortcut
  whenever you want.
- **The Authorization-header path still works** if you ever want to switch
  to a more standard form: replace step 3's URL with a JSON body and add
  `Authorization: Bearer <secret>` as a header in step 4. The server
  accepts both.
- **The Telegram bot stays uninvolved in the SMS hop.** Earlier you might
  have built a Shortcut that called `api.telegram.org/bot.../sendMessage`
  to make the bot post the SMS into the chat — that didn't work because
  Telegram bots ignore their own messages and so the parser/log path was
  never invoked. The new flow bypasses Telegram entirely on the way in;
  Telegram is only the *output* channel for confirmations.

## 4B. Gmail Apps Script (alternative / backup) — email to /ingest

Use this if you can't get the iOS Shortcut working, or if you want a
phone-independent backup channel running in parallel.

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

## 8. Troubleshooting

### `telegram.error.Conflict: terminated by other getUpdates request`

Telegram only allows **one** client per bot token to call `getUpdates` (long
polling). If two processes use the same token, they fight and you see this
error.

Typical causes:

- The bot runs on **Render** but you also started `python bot.py` **locally**
  — stop the local process.
- Two **Render web services** both run this repo with the same
  `TELEGRAM_BOT_TOKEN` — remove or pause the duplicate.

Fix: ensure **exactly one** running instance polls Telegram with your bot token.

### Junk SMS like `ישראכרט 536678` caused Telegram noise / HTTP 400

That text mentions Isracard but is **not** a real transaction SMS (no
`אושרה עסקה`, decline, or refund wording). The server should respond with
HTTP **200** and `{"status":"ignored"}` — **no** Telegram message.

If you still get `Couldn't parse this Isracard message...`, your Render
service is probably running an **older** deploy — check the dashboard that
the latest commit from `main` deployed successfully, then **Manual Deploy**
if needed.
