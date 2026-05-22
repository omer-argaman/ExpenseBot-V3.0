"""
scripts/test_isracard_parser.py — smoke test for parsing/isracard_parser.py.

Runs real Isracard SMS bodies through the parser and prints a
table of extracted fields. No assertions; the goal is a quick eye check that
every field comes out clean before any of the rest of the pipeline is wired.

Usage:
    python -m scripts.test_isracard_parser
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

# Allow running as a script from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parsing.isracard_parser import looks_like_transaction_notification, parse, looks_like_isracard  # noqa: E402

# Anchored "now" so the year-rollover heuristic is deterministic.
NOW = datetime(2026, 5, 7, 10, 0)


SAMPLES: list[tuple[str, str]] = [
    # --- Format A: prepaid card, with balance line ---
    ("A.0347.billabong", """שלום, בכרטיסך המסתיים בספרות 0347, אושרה עסקה בסך 224.85 ש"ח בבילבונג בורד שופ, בתאריך 02/05/2026, בשעה 19:12.  יתרתך בכרטיס נכון לעכשיו היא 224.30. מידע נוסף על חיובים בכרטיס אפשר למצוא כאן https://isracard.onelink.me/bajD/ksery3cj/?link=TransactionsDetails
שמחים לעזור,
ישראכרט."""),

    ("A.4888.golda", """שלום, בכרטיסך המסתיים בספרות 4888, אושרה עסקה בסך 153.65 ש"ח בגולדה דיזינגוף, בתאריך 10/04/2026, בשעה 22:05.  יתרתך בכרטיס נכון לעכשיו היא 147.27. מידע נוסף על חיובים בכרטיס אפשר למצוא כאן https://isracard.onelink.me/bajD/ksery3cj/?link=TransactionsDetails
שמחים לעזור,
ישראכרט."""),

    ("A.0347.terminalx", """שלום, בכרטיסך המסתיים בספרות 0347, אושרה עסקה בסך 550.00 ש"ח בTERMINAL X, בתאריך 31/03/2026, בשעה 22:16.  יתרתך בכרטיס נכון לעכשיו היא 449.15. מידע נוסף על חיובים בכרטיס אפשר למצוא כאן https://isracard.onelink.me/bajD/ksery3cj/?link=TransactionsDetails
שמחים לעזור,
ישראכרט."""),

    # --- Format B: credit card, no balance line ---
    ("B.4881.atalef", """שלום,
בכרטיסך 4881 אושרה עסקה ב-05/05 בסך 1000.00 ש"ח בעמותת עטלף.
למידע נוסף באפליקציה ובאתר: https://isracard.onelink.me/bajD/6x5cat4g.
לשירותך,  ישראכרט"""),

    ("B.4881.iriya", """שלום,
בכרטיסך 4881 אושרה עסקה ב-03/05 בסך 492.32 ש"ח בעיריית תל אביב יפו א.
למידע נוסף באפליקציה ובאתר: https://isracard.onelink.me/bajD/6x5cat4g.
לשירותך,  ישראכרט"""),

    ("B.4881.claude_usd", """שלום,
בכרטיסך 4881 אושרה עסקה ב-03/05 בסך 20.00 USD בCLAUDE.AI SUBSCRIPTION - UNITED STATES.
למידע נוסף באפליקציה ובאתר: https://isracard.onelink.me/bajD/6x5cat4g.
לשירותך,  ישראכרט"""),

    ("B.4881.paybox", """שלום,
בכרטיסך 4881 אושרה עסקה ב-02/05 בסך 70.00 ש"ח בPAYBOX.
למידע נוסף באפליקציה ובאתר: https://isracard.onelink.me/bajD/6x5cat4g.
לשירותך,  ישראכרט"""),

    ("B.4881.rav_motav", """שלום,
בכרטיסך 4881 אושרה עסקה ב-29/04 בסך 250.00 ש"ח במשרתי הקבע רב מוטב.
למידע נוסף באפליקציה ובאתר: https://isracard.onelink.me/bajD/6x5cat4g.
לשירותך,  ישראכרט"""),

    # Older B variant: `המסתיים ב- 4881,`
    ("B.4881.lager_bar", """שלום,
בכרטיסך המסתיים ב- 4881, אושרה עסקה ב-26/04 בסך 24.00 ש"ח בלאגר אנד אייל הרצליה.
למידע נוסף באפליקציה ובאתר: https://isracard.onelink.me/bajD/cvkxkhfb.
לשירותך,  ישראכרט"""),

    ("B.4881.cti_mobile", """שלום,
בכרטיסך המסתיים ב- 4881, אושרה עסקה ב-23/04 בסך 269.00 ש"ח בסי.טי.אי גומובייל בע.
למידע נוסף באפליקציה ובאתר: https://isracard.onelink.me/bajD/cvkxkhfb.
לשירותך,  ישראכרט"""),

    ("B.4881.passportcard", """שלום,
בכרטיסך 4881 אושרה עסקה ב-21/04 בסך 21.07 ש"ח בפספורטכארד שירותים פ.
למידע נוסף באפליקציה ובאתר: https://isracard.onelink.me/bajD/6x5cat4g.
לשירותך,  ישראכרט"""),

    ("B.4881.gett", """שלום,
בכרטיסך 4881 אושרה עסקה ב-17/04 בסך 55.10 ש"ח בGETT.
למידע נוסף באפליקציה ובאתר: https://isracard.onelink.me/bajD/6x5cat4g.
לשירותך,  ישראכרט"""),

    # Format B without merchant name
    ("B.6547.no_merchant", """שלום,
בכרטיסך 6547 אושרה עסקה ב-20/05 בסך 27.92 ש"ח.
למידע נוסף באפליקציה ובאתר: https://isracard.onelink.me/bajD/cvkxkhfb.
לשירותך,  ישראכרט"""),
]


def main() -> int:
    print(f"Parsing {len(SAMPLES)} Isracard samples (anchored now={NOW:%Y-%m-%d}):\n")

    failures: list[str] = []
    for label, body in SAMPLES:
        t = parse(body, now=NOW)
        ok = t.is_actionable
        loggable = t.is_loggable
        flag = "OK " if ok else "MISS"
        date_s = t.txn_date.isoformat() if t.txn_date else "—"
        time_s = t.txn_time or "—"
        amount_s = f"{t.amount:.2f}" if t.amount is not None else "—"
        merchant_s = t.merchant_raw or "—"
        norm_s = t.merchant_normalized or "—"

        print(f"[{flag}] {label}")
        print(f"  kind={t.kind}  last4={t.last4}  amount={amount_s} {t.currency}  "
              f"date={date_s}  time={time_s}")
        print(f"  merchant_raw       = {merchant_s!r}")
        print(f"  merchant_normalized= {norm_s!r}")
        print(f"  is_loggable={loggable}")
        print()

        if not ok:
            failures.append(label)

    print("=" * 60)
    print(f"Loggable: {len(SAMPLES) - len(failures)}/{len(SAMPLES)}")
    if failures:
        print("Failed:", failures)
        return 1

    # --- Transaction-vs-marketing filter --------------------------------
    assert not looks_like_isracard("some random text")
    assert not looks_like_transaction_notification(
        "ישראכרט מזמינים אותך לבדוק את המבצעים שלנו באפליקציה"
    )
    assert looks_like_transaction_notification(SAMPLES[0][1])  # billabong charge

    print("\nTransaction-notification filter: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
