"""
server.py — Flask app exposing /healthz and /ingest.

Replaces the inline BaseHTTPRequestHandler health server in bot.py. Bound to
the same PORT, so Render is happy with a single web service.

Endpoints:
  GET  /healthz   liveness probe (no auth)
  POST /ingest    auth: Authorization: Bearer <INGEST_SECRET>
                  body: JSON
                    {
                      "issuer":      "isracard",
                      "subject":     "...",            (optional)
                      "body":        "...",            (required)
                      "message_id":  "...",            (optional, used for dedupe)
                      "received_at": "ISO8601"         (optional)
                    }
                  returns: { "status": "<...>", ... }

The ingestion path is synchronous; the merchant/AI pipeline does at most a
couple of OpenAI/Sheets/FX requests so it completes well within 30s. Run in
a daemon thread alongside the python-telegram-bot polling loop.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from flask import Flask, jsonify, request

from config import ALLOWED_ISSUERS, HTTP_PORT, INGEST_SECRET
from handlers.transaction_handler import process_ingest

logger = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/healthz")
    def healthz() -> Any:
        return "OK", 200, {"Content-Type": "text/plain"}

    # Render's default route — keep returning 200 OK so any uptime monitor
    # hitting / doesn't see a 404.
    @app.get("/")
    def root() -> Any:
        return "OK", 200, {"Content-Type": "text/plain"}

    @app.post("/ingest")
    def ingest() -> Any:
        # Auth — accept Bearer header first, then ?secret= query param
        # (the query-param form lets iOS Shortcuts work with a single
        # "Get Contents of URL" action and no header configuration).
        if not INGEST_SECRET:
            logger.error("INGEST_SECRET is not set; refusing all /ingest requests.")
            return jsonify(status="error", detail="server not configured"), 503

        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth else ""
        if not token:
            token = request.args.get("secret", "").strip()
        if token != INGEST_SECRET:
            logger.warning("Bad auth on /ingest from %s", request.remote_addr)
            return jsonify(status="error", detail="unauthorized"), 401

        # Parse the payload. Three sources, in priority order:
        #   1. JSON body  (preferred; used by the Gmail Apps Script)
        #   2. Form body  (for legacy form-encoded adapters)
        #   3. URL query params (iOS Shortcut convenience — `body=...&issuer=...`)
        # `secret` is excluded from the merged payload so it never reaches the
        # parser, downstream logs, or the sheet note column.
        payload = request.get_json(silent=True) or {}
        if not payload and request.form:
            payload = request.form.to_dict()
        if not isinstance(payload, dict):
            return jsonify(status="error", detail="payload must be a JSON object"), 400
        if not payload.get("body") and request.args:
            args_payload = {k: v for k, v in request.args.items() if k != "secret"}
            payload = {**args_payload, **payload}  # JSON/form wins over URL params

        issuer = (payload.get("issuer") or "isracard").strip().lower()
        if issuer not in ALLOWED_ISSUERS:
            return jsonify(
                status="error",
                detail=f"issuer {issuer!r} is not allowed",
            ), 400
        payload["issuer"] = issuer

        try:
            result = process_ingest(payload)
        except Exception as exc:
            logger.exception("process_ingest crashed: %s", exc)
            return jsonify(status="error", detail=f"server error: {exc}"), 500

        body = {
            "status":   result.status,
            "detail":   result.detail,
            "category": result.category,
            "amount_ils": result.amount_ils,
            "merchant": result.merchant,
        }
        http_code = 200 if result.status in (
            "logged", "asked", "skipped", "duplicate", "ignored",
        ) else 400
        return jsonify(body), http_code

    return app


def start_in_thread() -> threading.Thread:
    """
    Boot the Flask app in a daemon thread on HTTP_PORT.

    Uses Flask's built-in WSGI server. Single-threaded is fine here:
    /ingest is called at most a handful of times per day. Health checks
    are negligible.
    """
    app = create_app()

    def run() -> None:
        logger.info("Flask server listening on port %d", HTTP_PORT)
        # use_reloader=False is critical inside a thread; threaded=True
        # keeps health checks from blocking on a slow /ingest call.
        app.run(
            host="0.0.0.0",
            port=HTTP_PORT,
            debug=False,
            use_reloader=False,
            threaded=True,
        )

    t = threading.Thread(target=run, name="flask-server", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)s  %(name)s  %(message)s")
    create_app().run(host="0.0.0.0", port=HTTP_PORT, debug=False, threaded=True)
