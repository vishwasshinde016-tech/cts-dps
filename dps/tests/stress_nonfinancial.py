"""Non-financial covenant wording stress test: each covenant type in the wordings real agreements use, in a numbered
clause and as a plain paragraph. Outcome: found (the type is proposed) or missed.

  python tests/stress_nonfinancial.py
"""

from __future__ import annotations

import itertools

from dps.extraction.agreement import extract_terms
from dps.parsing import Page

SENTENCES: dict[str, list[str]] = {
    "REPORTING_COVENANT": [
        "Within 120 days after the end of each fiscal year, the Borrower shall deliver to the Lender its audited "
        "consolidated balance sheet and statements of income and cash flows.",
        "The Borrower shall furnish to the Agent, within 45 days after the end of each fiscal quarter, its unaudited "
        "financial statements for such quarter.",
        "The Borrower shall submit audited financial statements within 180 days from the close of each financial year.",
        "The Borrower shall provide quarterly financial statements to the Lender within 60 days of the quarter end.",
        "The Borrower shall deliver annual audited accounts to the Bank within six months of the financial year end.",
    ],
    "COMPLIANCE_CERTIFICATE": [
        "Concurrently with each delivery of financial statements, the Borrower shall deliver a Compliance Certificate "
        "signed by a Financial Officer.",
        "The Borrower shall provide a certificate of compliance with the financial covenants every quarter.",
    ],
    "NEGATIVE_PLEDGE": [
        "The Borrower shall not create, incur, assume or permit to exist any Lien on any of its property or assets.",
        "The Borrower shall not, without the prior written consent of the Lender, create any charge or encumbrance on "
        "its assets.",
        "The Borrower will not create or permit to subsist any Security Interest over any of its assets.",
        "The Borrower shall not pledge, mortgage or otherwise encumber its assets.",
    ],
    "DEBT_INCURRANCE_RESTRICTION": [
        "The Borrower shall not incur additional indebtedness without the consent of the Lender.",
        "The Borrower will not create, incur, assume or suffer to exist any Indebtedness, except Permitted "
        "Indebtedness.",
        "The Borrower shall not avail of any further borrowing from any other bank without the Bank's consent.",
    ],
    "ASSET_DISPOSAL_RESTRICTION": [
        "The Borrower shall not sell, transfer, lease or otherwise dispose of any of its assets other than in the "
        "ordinary course of business.",
        "The Borrower will not convey, sell, lease, transfer or otherwise dispose of all or substantially all of its "
        "property.",
    ],
    "CHANGE_OF_CONTROL": [
        "A Change of Control shall constitute an Event of Default.",
        "The Borrower shall ensure that the Promoters hold not less than 51% of its share capital and retain "
        "management control.",
        "The Borrower shall not permit any change in its ownership or control without the prior consent of the Lender.",
    ],
    "INSURANCE_MAINTENANCE": [
        "The Borrower shall maintain insurance with financially sound and reputable insurers in such amounts as are "
        "customarily carried.",
        "The Borrower shall keep its assets adequately insured against fire, theft and other risks.",
        "The Borrower shall at all times keep insured all its properties and assets.",
    ],
    "BOOKS_AND_RECORDS": [
        "The Borrower shall keep proper books of record and account in accordance with GAAP.",
        "The Borrower shall maintain proper books of account and permit the Lender to inspect them.",
    ],
}
LAYOUTS = ["6.{n} Covenant. {s}", "{s}", "(b) {s}"]
FILLER = "ARTICLE VI AFFIRMATIVE AND NEGATIVE COVENANTS\nThe Borrower agrees as follows.\n"


def results() -> dict:
    out = {"found": 0, "missed": 0, "bad": []}
    for metric, sentences in SENTENCES.items():
        for (i, s), layout in itertools.product(enumerate(sentences), LAYOUTS):
            text = FILLER + layout.format(n=i + 1, s=s) + "\nThe Borrower shall comply with this Agreement.\n"
            got = {t.metric for t in extract_terms([Page(7, text, "TEXT", 1.0)]) if t.category == "NON_FINANCIAL"}
            if metric in got:
                out["found"] += 1
            else:
                out["missed"] += 1
                out["bad"].append((metric, layout[:8], s[:80]))
    return out


if __name__ == "__main__":
    r = results()
    print({k: v for k, v in r.items() if k != "bad"})
    for b in r["bad"]:
        print("  ", b)
