"""
Persistent background asyncio loop for sync callers (Flask /ingest thread).

Avoids creating a new event loop per asyncio.run() call, which can leak
memory when OpenAI httpx clients are used repeatedly from worker threads.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TypeVar

T = TypeVar("T")

_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_init_lock = threading.Lock()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop, _thread
    with _init_lock:
        if _loop is not None and _loop.is_running():
            return _loop
        _loop = asyncio.new_event_loop()

        def _run() -> None:
            asyncio.set_event_loop(_loop)
            _loop.run_forever()

        _thread = threading.Thread(target=_run, name="async-runner", daemon=True)
        _thread.start()
        return _loop


def run_coro(coro, timeout: float = 90.0) -> T:
    """Run an async coroutine on the shared loop and block for the result."""
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)
