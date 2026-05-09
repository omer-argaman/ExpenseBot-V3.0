"""
scripts/test_ingest.py — smoke test for the /ingest endpoint.

Runs the Flask server in-process with stubbed-out external dependencies
(Sheets, OpenAI, Telegram), then POSTs all 12 real Isracard samples plus a
duplicate, foreign-currency, and an unknown-issuer test case to confirm the
end-to-end orchestration: auth, dispatch, parse, dedupe, FX-or-skip, and
either a "logged" / "asked" / "duplicate" / "skipped" decision per item.

This is a real HTTP test against the Flask app, but no external service is
contacted — all I/O at module boundaries is monkey-patched to a recorder.

Run:
    python -m scripts.test_ingest
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from urllib import request as urlreq, error as urlerr

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Configure environment BEFORE any project module is imported.
import os
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("INGEST_SECRET", "test-secret")
os.environ.setdefault("OWNER_CHAT_ID", "1")
os.environ.setdefault("HIGH_CONFIDENCE_THRESHOLD", "0.85")
os.environ.setdefault("PORT", "18765")

import logging
logging.basicConfig(level=logging.WARNING)

# --- Stub external dependencies BEFORE handlers import them ---
import sheets as sheets_mod  # noqa: E402
import handlers.notifier as notifier_mod  # noqa: E402
import handlers.ai_handler as ai_handler_mod  # noqa: E402
import handlers.commands as commands_mod  # noqa: E402
import parsing.merchant_map as merchant_map_mod  # noqa: E402
import parsing.fx as fx_mod  # noqa: E402

LOGGED: list[dict] = []
NOTIFICATIONS: list[str] = []
LEARNED: list[tuple[str, str, str]] = []
AI_CALLS: list[dict] = []


def _stub_log_expense(category, amount, original_text, dt=None):
    LOGGED.append({
        "category": category,
        "amount": amount,
        "original_text": original_text,
        "dt": dt.isoformat() if dt else None,
    })
    from sheets import LogResult
    return LogResult(
        success=True, category=category, amount_added=amount, new_total=amount,
        tab_name="0526", row=1,
        timestamp=time.strftime("%Y-%m-%d %H:%M"),
        message=f"Added {amount} to {category}",
    )


def _stub_send_message(text, chat_id=None, inline_keyboard=None, parse_mode="HTML"):
    NOTIFICATIONS.append(text)
    return 12345


def _stub_append_to_history(**kwargs):
    pass


def _stub_learn(merchant, category, source="user"):
    LEARNED.append((merchant, category, source))


def _stub_lookup(merchant):
    return None  # Always miss the map; force AI path


def _stub_ai_categorize(merchant, amount, currency="ILS",
                        card_prior=None, card_name=None, fuzzy_hint=None):
    AI_CALLS.append({
        "merchant": merchant, "amount": amount, "currency": currency,
        "card_prior": card_prior, "card_name": card_name,
    })
    # Hand-tuned per-merchant deterministic answers so we can predict the
    # outcome (logged vs asked) without calling OpenAI.
    rules = {
        "בילבונג בורד שופ": ("Other (Daily)", 0.92),
        "גולדה דיזינגוף":   ("Coffee",         0.78),
        "TERMINAL X":       ("Other (Daily)", 0.95),
        "עמותת עטלף":       ("Other (Daily)", 0.55),
        "עיריית תל אביב יפו": ("Property Tax", 0.97),
        "CLAUDE.AI SUBSCRIPTION - UNITED STATES": ("Education", 0.6),
        "PAYBOX":           ("Other (Daily)", 0.4),
        "משרתי הקבע רב מוטב": ("Other (Daily)", 0.45),
        "לאגר אנד אייל הרצליה": ("Beer / Wine", 0.9),
        "סי.טי.אי גומובייל בע": ("Internet", 0.88),
        "פספורטכארד שירותים": ("Life Insurance", 0.7),
        "GETT":              ("Public Transportation", 0.96),
    }
    cat, conf = rules.get(merchant, (None, 0.0))
    return {
        "category": cat, "confidence": conf,
        "reason": "stub", "alternatives": [],
    }


def _stub_fx_convert(amount, currency_from, currency_to="ILS"):
    if currency_from.upper() == "USD" and currency_to == "ILS":
        return round(amount * 3.65, 2)
    return None


# Apply stubs
sheets_mod.log_expense = _stub_log_expense  # type: ignore[assignment]
notifier_mod.send_message = _stub_send_message  # type: ignore[assignment]
commands_mod.append_to_history = _stub_append_to_history  # type: ignore[assignment]
merchant_map_mod.learn = _stub_learn  # type: ignore[assignment]
merchant_map_mod.lookup = _stub_lookup  # type: ignore[assignment]
ai_handler_mod.ai_categorize_merchant_sync = _stub_ai_categorize  # type: ignore[assignment]
fx_mod.convert = _stub_fx_convert  # type: ignore[assignment]

# Re-import transaction_handler so it picks up the stubbed callables
import importlib  # noqa: E402
import handlers.transaction_handler as th_mod  # noqa: E402
importlib.reload(th_mod)
import server  # noqa: E402
importlib.reload(server)


# --- Boot the server in a background thread ---
PORT = int(os.environ["PORT"])
_app = server.create_app()


def _run_server():
    _app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False, threaded=True)


threading.Thread(target=_run_server, daemon=True).start()
# Wait for the socket to come up
for _ in range(50):
    try:
        urlreq.urlopen(f"http://127.0.0.1:{PORT}/healthz", timeout=1).read()
        break
    except (urlerr.URLError, ConnectionError):
        time.sleep(0.05)


def _post(payload, secret="test-secret"):
    body = json.dumps(payload).encode("utf-8")
    req = urlreq.Request(
        f"http://127.0.0.1:{PORT}/ingest",
        data=body,
        headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlreq.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urlerr.HTTPError as e:
        return e.code, json.loads(e.read())


SAMPLES = [
    ("A.0347.billabong",   "msg-1",  "Other (Daily)", "logged"),
    ("A.4888.golda",        "msg-2",  None, "asked"),
    ("A.0347.terminalx",    "msg-3",  "Other (Daily)", "logged"),
    ("B.4881.atalef",       "msg-4",  None, "asked"),
    ("B.4881.iriya",        "msg-5",  "Property Tax", "logged"),
    ("B.4881.claude_usd",   "msg-6",  None, "asked"),
    ("B.4881.paybox",       "msg-7",  None, "asked"),
    ("B.4881.rav_motav",    "msg-8",  None, "asked"),
    ("B.4881.lager_bar",    "msg-9",  "Beer / Wine", "logged"),
    ("B.4881.cti_mobile",   "msg-10", "Internet", "logged"),
    ("B.4881.passportcard", "msg-11", None, "asked"),
    ("B.4881.gett",         "msg-12", "Public Transportation", "logged"),
]

from scripts.test_isracard_parser import SAMPLES as PARSER_SAMPLES  # noqa: E402

bodies = {label: body for (label, body) in PARSER_SAMPLES}

print("Posting 12 samples to /ingest …\n")
results: list[tuple[str, str, dict]] = []
for label, msg_id, expected_cat, expected_status in SAMPLES:
    body = bodies[label]
    code, data = _post({
        "issuer": "isracard",
        "body": body,
        "message_id": msg_id,
    })
    results.append((label, expected_status, data))
    flag = "OK " if data["status"] == expected_status else "X  "
    cat = data.get("category") or "—"
    print(f"  [{flag}] {label:30}  http={code:>3}  status={data['status']:<10}"
          f"  cat={cat}  detail={data.get('detail') or ''}")

# --- Edge cases ---
print("\nEdge cases:")

# 1. Duplicate
code, data = _post({"issuer": "isracard", "body": bodies["B.4881.atalef"], "message_id": "msg-4"})
print(f"  duplicate replay     http={code}  status={data['status']}")

# 2. Bad auth
code, data = _post({"issuer": "isracard", "body": bodies["B.4881.gett"], "message_id": "x"},
                   secret="WRONG")
print(f"  bad auth             http={code}  status={data['status']}")

# 3. Empty body
code, data = _post({"issuer": "isracard", "body": "", "message_id": "y"})
print(f"  empty body           http={code}  status={data['status']}")

# 4. Non-Isracard text
code, data = _post({"issuer": "isracard", "body": "hello world", "message_id": "z"})
print(f"  non-isracard body    http={code}  status={data['status']}")

# 5. Disallowed issuer
code, data = _post({"issuer": "max", "body": bodies["B.4881.gett"], "message_id": "w"})
print(f"  disallowed issuer    http={code}  status={data['status']}")

# 6. URL-param style (iOS Shortcut shape) — secret + body in the URL, no auth header.
from urllib.parse import quote_plus  # noqa: E402

ios_body = bodies["A.0347.terminalx"]
ios_url = (
    f"http://127.0.0.1:{PORT}/ingest"
    f"?secret=test-secret"
    f"&issuer=isracard"
    f"&body={quote_plus(ios_body)}"
)
ios_req = urlreq.Request(ios_url, data=b"", method="POST")
try:
    with urlreq.urlopen(ios_req, timeout=5) as resp:
        ios_code, ios_data = resp.status, json.loads(resp.read())
except urlerr.HTTPError as e:
    ios_code, ios_data = e.code, json.loads(e.read())
print(f"  iOS URL-param style  http={ios_code}  status={ios_data['status']}  cat={ios_data.get('category') or '—'}")
assert ios_data["status"] == "duplicate", \
    f"expected duplicate (terminalx already logged above) but got {ios_data['status']}"

# 7. URL-param with WRONG ?secret= must reject
bad_secret_url = (
    f"http://127.0.0.1:{PORT}/ingest?secret=NOPE&issuer=isracard&body=hello"
)
try:
    with urlreq.urlopen(urlreq.Request(bad_secret_url, data=b"", method="POST"), timeout=5) as resp:
        bad_code = resp.status
except urlerr.HTTPError as e:
    bad_code = e.code
print(f"  iOS URL bad secret   http={bad_code}")
assert bad_code == 401, f"expected 401, got {bad_code}"

# 8. URL-param with secret in URL but a NEW message via the body param — should log/ask cleanly
fresh_body = bodies["A.4888.golda"].replace("בסך 153.65", "בסך 199.99")  # change amount so dedupe doesn't trigger
fresh_url = (
    f"http://127.0.0.1:{PORT}/ingest"
    f"?secret=test-secret"
    f"&issuer=isracard"
    f"&body={quote_plus(fresh_body)}"
)
try:
    with urlreq.urlopen(urlreq.Request(fresh_url, data=b"", method="POST"), timeout=5) as resp:
        fresh_code, fresh_data = resp.status, json.loads(resp.read())
except urlerr.HTTPError as e:
    fresh_code, fresh_data = e.code, json.loads(e.read())
print(f"  iOS URL fresh txn    http={fresh_code}  status={fresh_data['status']}  cat={fresh_data.get('category') or '—'}")
assert fresh_data["status"] in ("logged", "asked"), \
    f"expected logged or asked, got {fresh_data['status']}"

# --- Summary ---
print("\nSummary:")
print(f"  /ingest calls reaching parse: {len(results)}")
print(f"  Sheet writes (LOGGED):        {len(LOGGED)}")
print(f"  Telegram notifications:       {len(NOTIFICATIONS)}")
print(f"  Auto-learned mappings:        {len(LEARNED)}")
print(f"  AI categorize calls:          {len(AI_CALLS)}")

# Validate expected outcomes
mismatches = [r for r in results if r[2]["status"] != r[1]]
if mismatches:
    print("\nMISMATCHES:")
    for label, expected, data in mismatches:
        print(f"  {label}: expected={expected} got={data['status']}")
    sys.exit(1)

print("\nAll 12 samples produced expected status. End-to-end OK.")
