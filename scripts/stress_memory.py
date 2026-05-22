"""
Simulate several /ingest cycles and record RSS after each (debug session).

Run from repo root:
  python -m scripts.stress_memory

Uses test_ingest stubs (no real Sheets/Telegram/OpenAI). Measures whether
asyncio.run vs shared loop and ingest orchestration grow RSS in-process.
"""

from __future__ import annotations

import gc
import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("INGEST_SECRET", "test-secret")
os.environ.setdefault("OWNER_CHAT_ID", "1")

# Reuse test_ingest stubs
from scripts import test_ingest as ti  # noqa: E402

import handlers.transaction_handler as th_mod  # noqa: E402

importlib.reload(th_mod)

from _debug_trace import _rss_mb, dbg  # noqa: E402
from scripts.test_isracard_parser import SAMPLES as PARSER_SAMPLES  # noqa: E402

bodies = {label: body for (label, body) in PARSER_SAMPLES}

# Three 4881 samples that trigger AI + ask or log path (not ignored cards)
SEQUENCE = [
    ("B.4881.lager_bar", "stress-1"),
    ("B.4881.gett", "stress-2"),
    ("B.4881.paybox", "stress-3"),
    ("B.4881.atalef", "stress-4"),
    ("B.4881.iriya", "stress-5"),
]


def main() -> int:
    dbg("ALL", "stress_memory", "start", {"rss_mb": _rss_mb()}, run_id="stress")
    for label, msg_id in SEQUENCE:
        gc.collect()
        before = _rss_mb()
        result = th_mod.process_ingest({
            "issuer": "isracard",
            "body": bodies[label],
            "message_id": msg_id,
        })
        gc.collect()
        after = _rss_mb()
        dbg("ALL", "stress_memory", "after_ingest", {
            "label": label,
            "status": result.status,
            "rss_before": before,
            "rss_after": after,
            "delta_mb": after - before if after >= 0 and before >= 0 else None,
        }, run_id="stress")
        print(f"{label}: {result.status}  RSS {before} -> {after} MB (delta {after - before:+d})")

    print("\nSee .cursor/debug-1d64c2.log for NDJSON trace.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
