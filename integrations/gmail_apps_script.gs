/**
 * Gmail Apps Script — forwards Isracard transaction emails to the bot's
 * /ingest endpoint.
 *
 * Setup:
 *   1. Open https://script.google.com and create a new project.
 *   2. Replace the default file with this one (Code.gs).
 *   3. Project Settings -> Script Properties, add:
 *        INGEST_URL    = https://<your-render-host>/ingest
 *        INGEST_SECRET = same value as the INGEST_SECRET env var on Render
 *        INCOMING_LABEL  = "expense-bot/incoming"     (default below)
 *        PROCESSED_LABEL = "expense-bot/processed"    (default below)
 *        ERROR_LABEL     = "expense-bot/error"        (default below)
 *   4. In Gmail, create a filter for Isracard emails (e.g. from:isracard.co.il)
 *      and apply the INCOMING_LABEL.
 *   5. Triggers (clock icon in the Apps Script editor) -> Add Trigger:
 *        Function:  pollIncoming
 *        Event source: Time-driven
 *        Type:      Minutes timer
 *        Interval:  Every 5 minutes  (or 1 minute if you want it snappier)
 *   6. Run `pollIncoming` once manually so Google asks for permissions.
 *
 * Behavior:
 *   - Processes every thread labeled INCOMING_LABEL.
 *   - For each message in the thread that hasn't been seen yet (per its
 *     getId()), strips HTML, POSTs the plain-text body to /ingest, and
 *     re-labels the thread on success.
 *   - On failure, the thread keeps INCOMING_LABEL and gains ERROR_LABEL so
 *     it shows up in your inbox view; the next poll will retry.
 *   - Subject is also forwarded for context, though the parser doesn't
 *     currently use it.
 */

const DEFAULT_INCOMING = "expense-bot/incoming";
const DEFAULT_PROCESSED = "expense-bot/processed";
const DEFAULT_ERROR = "expense-bot/error";
const ISSUER = "isracard";

function _props() {
  const p = PropertiesService.getScriptProperties();
  return {
    url: p.getProperty("INGEST_URL"),
    secret: p.getProperty("INGEST_SECRET"),
    incomingLabel: p.getProperty("INCOMING_LABEL") || DEFAULT_INCOMING,
    processedLabel: p.getProperty("PROCESSED_LABEL") || DEFAULT_PROCESSED,
    errorLabel: p.getProperty("ERROR_LABEL") || DEFAULT_ERROR,
  };
}

function _getOrCreateLabel(name) {
  let label = GmailApp.getUserLabelByName(name);
  if (!label) label = GmailApp.createLabel(name);
  return label;
}

function _stripHtml(html) {
  // Very small HTML-to-text shim; Gmail's getPlainBody() is preferred
  // but some senders only set the HTML part.
  if (!html) return "";
  return html
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/p>/gi, "\n")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/[ \t]+/g, " ")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function _postIngest(url, secret, payload) {
  const resp = UrlFetchApp.fetch(url, {
    method: "post",
    contentType: "application/json",
    headers: { Authorization: "Bearer " + secret },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  });
  const code = resp.getResponseCode();
  const body = resp.getContentText();
  return { code: code, body: body };
}

function pollIncoming() {
  const cfg = _props();
  if (!cfg.url || !cfg.secret) {
    throw new Error(
      "Set INGEST_URL and INGEST_SECRET in Script Properties before running."
    );
  }

  const incoming = _getOrCreateLabel(cfg.incomingLabel);
  const processed = _getOrCreateLabel(cfg.processedLabel);
  const error = _getOrCreateLabel(cfg.errorLabel);

  const threads = incoming.getThreads(0, 25);
  if (threads.length === 0) return;

  threads.forEach(function (thread) {
    const messages = thread.getMessages();
    let allOk = true;
    let anyOk = false;

    messages.forEach(function (msg) {
      const messageId = msg.getId();
      const subject = msg.getSubject() || "";
      let body = msg.getPlainBody() || "";
      if (!body) body = _stripHtml(msg.getBody());
      body = body.trim();

      if (!body) {
        allOk = false;
        return;
      }

      const payload = {
        issuer: ISSUER,
        subject: subject,
        body: body,
        message_id: messageId,
        received_at: msg.getDate().toISOString(),
      };

      const res = _postIngest(cfg.url, cfg.secret, payload);
      if (res.code >= 200 && res.code < 300) {
        anyOk = true;
        Logger.log("OK " + messageId + " " + res.body);
      } else {
        allOk = false;
        Logger.log("FAIL " + messageId + " HTTP " + res.code + " " + res.body);
      }
    });

    if (allOk) {
      thread.removeLabel(incoming);
      thread.removeLabel(error);
      thread.addLabel(processed);
    } else if (anyOk) {
      // Partial: keep error label so we notice; leave incoming so unprocessed
      // messages get retried next minute.
      thread.addLabel(error);
    } else {
      thread.addLabel(error);
    }
  });
}

/**
 * Manual smoke test — run this once from the Apps Script UI to confirm the
 * /ingest endpoint is reachable and authenticated.
 */
function testIngest() {
  const cfg = _props();
  const sample =
    "שלום,\n" +
    "בכרטיסך 4881 אושרה עסקה ב-05/05 בסך 1000.00 ש\"ח בעמותת עטלף.\n" +
    "למידע נוסף באפליקציה ובאתר: https://example\n" +
    "לשירותך, ישראכרט";
  const res = _postIngest(cfg.url, cfg.secret, {
    issuer: ISSUER,
    subject: "Test",
    body: sample,
    message_id: "manual-test-" + new Date().toISOString(),
  });
  Logger.log("HTTP " + res.code + ": " + res.body);
}
