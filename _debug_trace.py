"""Debug-session NDJSON trace. Fold regions in callers."""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

_ROOT = os.path.dirname(os.path.abspath(__file__))
_LOG_PATH = os.path.join(_ROOT, ".cursor", "debug-b23fa3.log")
_SESSION = "b23fa3"
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


def mem_snapshot() -> dict[str, Any]:
    """Lightweight in-process memory counters for leak hypotheses."""
    snap: dict[str, Any] = {"rss_mb": _rss_mb()}
    try:
        import handlers.transaction_handler as th

        snap["dedupe"] = len(th._dedupe)
        snap["pending"] = len(th._pending)
    except Exception:
        pass
    try:
        import parsing.merchant_map as mm

        snap["merchant_map"] = len(mm._cache) if mm._cache else 0
    except Exception:
        pass
    try:
        import sheets as sh

        snap["tabs_cache"] = len(sh._tabs_cache[1]) if sh._tabs_cache else 0
    except Exception:
        pass
    try:
        import handlers.async_runner as ar

        loop = ar._loop
        if loop is not None and loop.is_running():
            snap["async_tasks"] = len(asyncio_all_tasks(loop))
        else:
            snap["async_tasks"] = 0
    except Exception:
        pass
    try:
        import handlers.ai_handler as ah

        snap["openai_client"] = ah._client is not None
    except Exception:
        pass
    return snap


# Object-type prefixes we suspect leak per expense. Counting the whole heap
# by module prefix is the cheapest way to tell H1 (openai/httpx) apart from
# H2 (google sheets) apart from H6 (telegram/requests) in production logs.
_CENSUS_PREFIXES = (
    "httpx.",
    "httpcore.",
    "openai.",
    "ssl.",
    "h11.",
    "anyio.",
    "googleapiclient.",
    "google.",
    "httplib2.",
    "telegram.",
    "requests.",
    "urllib3.",
)


def gc_census() -> dict[str, Any]:
    """Heap census by object type — pinpoints which subsystem accumulates.

    Returns counts of live objects whose type's module starts with a suspect
    prefix, plus a couple of coarse totals. Slightly heavy (walks the heap),
    so only call from periodic/low-frequency instrumentation, not hot paths.
    """
    import gc

    counts: dict[str, int] = {}
    coros = 0
    total = 0
    try:
        gc.collect()
        for obj in gc.get_objects():
            total += 1
            try:
                t = type(obj)
                mod = getattr(t, "__module__", "") or ""
            except Exception:
                continue
            if mod.startswith("asyncio") and t.__name__ in ("Task", "Future"):
                coros += 1
            for pre in _CENSUS_PREFIXES:
                if mod.startswith(pre):
                    key = pre.rstrip(".")
                    counts[key] = counts.get(key, 0) + 1
                    break
    except Exception:
        pass
    out: dict[str, Any] = {f"obj_{k}": v for k, v in counts.items()}
    out["obj_total"] = total
    out["obj_asyncio_tasks"] = coros
    try:
        out["gc_garbage"] = len(gc.garbage)
    except Exception:
        pass
    return out


def asyncio_all_tasks(loop) -> set:
    import asyncio

    return asyncio.all_tasks(loop)


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
        "data": {**mem_snapshot(), **(data or {})},
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
