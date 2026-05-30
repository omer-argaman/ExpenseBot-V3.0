"""
Simulate many /ingest cycles and record RSS after each (debug session).

Run from repo root:
  python -m scripts.stress_memory

Uses ingest_stubs (no real Sheets/Telegram/OpenAI). Measures whether
ingest orchestration grows RSS in-process across repeated SMS simulations.
"""

from __future__ import annotations

import gc
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import ingest_stubs as stubs  # noqa: E402

stubs.apply()

import handlers.transaction_handler as th_mod  # noqa: E402

from _debug_trace import dbg, mem_snapshot  # noqa: E402
from scripts.test_isracard_parser import SAMPLES as PARSER_SAMPLES  # noqa: E402

bodies = {label: body for (label, body) in PARSER_SAMPLES}

SEQUENCE = [
    ("B.4881.lager_bar", "stress-1"),
    ("B.4881.gett", "stress-2"),
    ("B.4881.paybox", "stress-3"),
    ("B.4881.atalef", "stress-4"),
    ("B.4881.iriya", "stress-5"),
    ("B.4881.claude_usd", "stress-6"),
    ("B.4881.cti_mobile", "stress-7"),
    ("B.4881.passportcard", "stress-8"),
]

ROUNDS = 5


def main() -> int:
    dbg("ALL", "stress_memory", "start", {}, run_id="stress")
    start_rss = mem_snapshot()["rss_mb"]
    for round_n in range(ROUNDS):
        for label, msg_id in SEQUENCE:
            gc.collect()
            before = mem_snapshot()["rss_mb"]
            result = th_mod.process_ingest({
                "issuer": "isracard",
                "body": bodies[label],
                "message_id": f"{msg_id}-r{round_n}",
            })
            gc.collect()
            after = mem_snapshot()["rss_mb"]
            delta = after - before if after >= 0 and before >= 0 else None
            dbg("ALL", "stress_memory", "after_ingest", {
                "round": round_n,
                "label": label,
                "status": result.status,
                "rss_before": before,
                "rss_after": after,
                "delta_mb": delta,
            }, run_id="stress")
            print(
                f"r{round_n} {label}: {result.status}  "
                f"RSS {before} -> {after} MB (delta {delta:+d})"
            )

    final = mem_snapshot()["rss_mb"]
    print(f"\nStart RSS: {start_rss} MB  Final RSS: {final} MB  "
          f"(+{final - start_rss} MB over {ROUNDS * len(SEQUENCE)} ingests)")
    print("See .cursor/debug-b23fa3.log for NDJSON trace.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
