"""Debug-session NDJSON trace (session 1d64c2). Fold regions in callers."""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

_ROOT = os.path.dirname(os.path.abspath(__file__))
_LOG_PATH = os.path.join(_ROOT, ".cursor", "debug-1d64c2.log")
_SESSION = "1d64c2"
_logger = logging.getLogger("debug_trace")


def _rss_mb() -> int:
    try:
        import resource
        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            rss_kb = rss_kb // 1024
        return rss_kb // 1024
    except Exception:
        return -1


def dbg(
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict[str, Any] | None = None,
    run_id: str = "pre-fix",
) -> None:
    # #region agent log
    payload = {
        "sessionId": _SESSION,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": {**(data or {}), "rss_mb": _rss_mb()},
        "timestamp": int(time.time() * 1000),
        "runId": run_id,
    }
    line = json.dumps(payload, ensure_ascii=False)
    _logger.info("memtrace %s %s %s", hypothesis_id, message, payload.get("data"))
    try:
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    # #endregion
