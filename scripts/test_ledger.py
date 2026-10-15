"""
scripts/test_ledger.py — offline tests for the Transactions ledger and the
"asks never expire" behavior (no Google, Telegram or OpenAI calls).

Covers:
  - one ledger row per ingested transaction, with the right status/fields
  - old pending asks are evicted from memory but never deleted from the sheet
  - pending_still_open(): a tap after the expense MCP settled the ask is refused
  - the merchant cache re-reads the sheet after its TTL
  - /delete removes only the exact note line and marks the ledger row deleted

Run:
    python -m scripts.test_ledger
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import ingest_stubs  # noqa: E402

ingest_stubs.apply()

import handlers.transaction_handler as th  # noqa: E402
import handlers.commands as commands_mod  # noqa: E402
import parsing.merchant_map as merchant_map_mod  # noqa: E402
import sheets as sheets_mod  # noqa: E402
from scripts.test_isracard_parser import SAMPLES as PARSER_SAMPLES  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok    " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# 1. Ledger row per ingest
# ---------------------------------------------------------------------------

print("Ledger rows per ingested transaction:")
bodies = dict(PARSER_SAMPLES)
expected = {
    "A.0347.billabong": "ignored",
    "B.4881.atalef": "asked",
    "B.4881.lager_bar": "logged",
    "B.4881.claude_usd": "asked",
}
for i, (label, status) in enumerate(expected.items()):
    ingest_stubs.LEDGER.clear()
    result = th.process_ingest({"issuer": "isracard", "body": bodies[label], "message_id": f"ledger-{i}"})
    rows = list(ingest_stubs.LEDGER)
    check(len(rows) == 1, f"{label}: exactly one ledger row")
    if not rows:
        continue
    row = rows[0]
    check(row["status"] == status, f"{label}: status {row['status']!r} == {status!r}")
    check(len(row["txn_id"]) == 12 and row["source"] == "sms", f"{label}: txn_id + source")
    check(row["received_at"].endswith("+03:00") or row["received_at"].endswith("+02:00"),
          f"{label}: received_at in Israel time ({row['received_at']})")
    if status == "asked":
        check(bool(row["pending_id"]) and row["pending_id"] == result.pending_id, f"{label}: pending_id stored")
        check(bool(row["note_text"]), f"{label}: note_text stored for later logging")
    if status == "logged":
        check(row["category"] == "Beer / Wine", f"{label}: category stored")
        check(row["category_origin"] == "ai_auto", f"{label}: category_origin ai_auto")
        check(row["sheet_tab"] == "0526" and row["note_line"].endswith(row["note_text"]),
              f"{label}: sheet_tab + note_line stored")

# Duplicate of an already-seen message
ingest_stubs.LEDGER.clear()
th.process_ingest({"issuer": "isracard", "body": bodies["B.4881.lager_bar"], "message_id": "ledger-2"})
check(ingest_stubs.LEDGER and ingest_stubs.LEDGER[0]["status"] == "duplicate", "duplicate SMS recorded as duplicate")

# A ledger write failure never breaks ingest
orig_append = th.append_ledger_row
th.append_ledger_row = lambda row: (_ for _ in ()).throw(RuntimeError("sheets down"))
res = th.process_ingest({"issuer": "isracard", "body": bodies["B.4881.gett"], "message_id": "ledger-fail"})
check(res.status == "logged", "ingest still logs when the ledger write fails")
th.append_ledger_row = orig_append

# ---------------------------------------------------------------------------
# 2. Old asks stay in the Pending sheet
# ---------------------------------------------------------------------------

print("\nPending asks never expire from the sheet:")
ingest_stubs.DELETED_PENDING.clear()
old = th.PendingAsk(
    pending_id="oldask0001", merchant_normalized="BAR", merchant_raw="BAR",
    amount_ils=50.0, txn_date=None, sheet_note="BAR 50 ILS",
    created_at=time.time() - 3 * 24 * 3600,
)
th._pending[old.pending_id] = old
th.store_pending(th.PendingAsk(
    pending_id="newask0001", merchant_normalized="X", merchant_raw="X",
    amount_ils=10.0, txn_date=None, sheet_note="X 10 ILS", created_at=time.time(),
))
check("oldask0001" not in th._pending, "old ask evicted from memory")
check("oldask0001" not in ingest_stubs.DELETED_PENDING, "old ask NOT deleted from the Pending sheet")

orig_read_pending = th.read_pending_asks
th.read_pending_asks = lambda: [{
    "pending_id": "oldask0001", "merchant_normalized": "BAR", "merchant_raw": "BAR",
    "amount_ils": 50.0, "txn_date_iso": "", "sheet_note": "BAR 50 ILS",
    "candidates_json": "[]", "created_at": time.time() - 3 * 24 * 3600,
}]
check(th.preload_pending_asks() == 0, "preload skips old asks in memory")
check("oldask0001" not in ingest_stubs.DELETED_PENDING, "preload does not delete old asks")
check(th.reload_pending_from_sheet("oldask0001") is not None, "a late tap can still reload an old ask")

# ---------------------------------------------------------------------------
# 3. pending_still_open
# ---------------------------------------------------------------------------

print("\nTap after the MCP settled the ask:")
check(th.pending_still_open("oldask0001"), "row still in Pending -> open")
th.read_pending_asks = lambda: []
ingest_stubs.LEDGER[:] = [{"pending_id": "gone000001", "status": "settled"}]
check(not th.pending_still_open("gone000001"), "row gone + ledger settled -> refused")
ingest_stubs.LEDGER[:] = [{"pending_id": "gone000001", "status": "asked"}]
check(th.pending_still_open("gone000001"), "row gone + ledger still asked -> allowed")
ingest_stubs.LEDGER[:] = []
check(th.pending_still_open("gone000001"), "row gone + no ledger row (pre-ledger ask) -> allowed")
th.read_pending_asks = lambda: (_ for _ in ()).throw(RuntimeError("sheets down"))
check(th.pending_still_open("gone000001"), "read failure -> allowed (old behavior)")
th.read_pending_asks = orig_read_pending

ingest_stubs.LEDGER[:] = [{"pending_id": "tapped0001", "status": "asked"}]
th.mark_ask_settled("tapped0001", "Coffee", "1026", "2026-10-15 10:00  X 10 ILS")
row = ingest_stubs.LEDGER[0]
check(row["status"] == "settled" and row["category_origin"] == "telegram_user"
      and row["category"] == "Coffee" and row["sheet_tab"] == "1026",
      "Telegram tap marks the ledger row settled")

# ---------------------------------------------------------------------------
# 4. Merchant cache TTL
# ---------------------------------------------------------------------------

print("\nMerchant cache refresh:")
reads = []


def _fake_read_merchant_map():
    reads.append(1)
    return [{"merchant_normalized": f"SHOP{len(reads)}", "category": "Coffee",
             "source": "user", "first_seen": "", "last_seen": "", "hits": 1}]


merchant_map_mod.read_merchant_map = _fake_read_merchant_map
merchant_map_mod.reset_cache_for_tests()
merchant_map_mod._ensure_loaded()
merchant_map_mod._ensure_loaded()
check(len(reads) == 1, "cache is reused within the TTL")
merchant_map_mod._loaded_at -= merchant_map_mod._CACHE_TTL_SECONDS + 1
cache = merchant_map_mod._ensure_loaded()
check(len(reads) == 2 and "shop2" in cache, "cache re-reads the sheet after the TTL")
merchant_map_mod.read_merchant_map = lambda: (_ for _ in ()).throw(RuntimeError("down"))
merchant_map_mod._loaded_at -= merchant_map_mod._CACHE_TTL_SECONDS + 1
check("shop2" in merchant_map_mod._ensure_loaded(), "refresh failure keeps the old cache")

# ---------------------------------------------------------------------------
# 5. /delete removes only the exact line
# ---------------------------------------------------------------------------

print("\n/delete exact-line removal:")
note_state = {
    "note": "2026-10-15 09:00  coffee 20\n2026-10-15 10:00  coffee 45 oat\n"
            "2026-10-15 10:00  [card 1234] CAFE 30 ILS on 2026-10-14",
    "amount": 95.0,
}


class _FakeExec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _FakeSpreadsheets:
    def get(self, **kwargs):
        return _FakeExec({"sheets": [{"properties": {"title": "1026", "sheetId": 7}}]})


class _FakeService:
    def spreadsheets(self):
        return _FakeSpreadsheets()


saved_history = []
commands_mod._build_service = lambda: _FakeService()
commands_mod.load_history = lambda: [{
    "category": "Coffee", "amount": 45.0, "tab_name": "1026", "row": 30,
    "timestamp": "2026-10-15 10:00", "original_text": "coffee 45 oat",
}]
commands_mod.save_history = lambda h: saved_history.append(h)
commands_mod._read_current_amount = lambda service, tab, row: note_state["amount"]
commands_mod._write_amount = lambda service, tab, row, amt: note_state.update(amount=amt)
commands_mod._read_existing_note = lambda service, tab, row: note_state["note"]
commands_mod._write_note = lambda service, tab, row, sid, note: note_state.update(note=note)
ingest_stubs.LEDGER[:] = [{"sheet_tab": "1026", "note_line": "2026-10-15 10:00  coffee 45 oat", "status": "logged"}]
sheets_mod.update_ledger_where = ingest_stubs._stub_update_ledger_where

commands_mod.delete(1)
check(note_state["amount"] == 50.0, "amount subtracted")
check(note_state["note"] == "2026-10-15 09:00  coffee 20\n2026-10-15 10:00  [card 1234] CAFE 30 ILS on 2026-10-14",
      "only the exact line removed; same-minute entry kept")
check(ingest_stubs.LEDGER[0]["status"] == "deleted", "ledger row marked deleted")

print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED")
    sys.exit(1)
print("All ledger checks passed.")
