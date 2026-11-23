"""Money in any currency (Phase 1, 22 Nov 2026): one currency list in dps.money, used by every reader. Before, eight
places each kept their own list (2 to 12 currencies), so a JPY, HKD or CHF amount was missed or read as rupees."""

from __future__ import annotations

import pytest

from dps import money
from dps.analyze.identify import find_identifiers
from dps.extraction.agreement import extract_terms
from dps.parse.engine import Page
from dps.skills.covenants.obligation import baskets

SAMPLES = [
    ("JPY", "a facility of JPY 5,000 million"), ("JPY", "¥ 1,200 million"), ("CNY", "RMB 300 million"),
    ("HKD", "HK$ 25 million"), ("AUD", "A$ 10 million"), ("CAD", "CAD 40 million"), ("CHF", "CHF 12,000,000"),
    ("NZD", "NZ$ 3 million"), ("ZAR", "ZAR 150 million"), ("MYR", "RM 20 million"), ("IDR", "IDR 9,000,000,000"),
    ("THB", "THB 500 million"), ("KRW", "KRW 2,000 million"), ("SEK", "SEK 75 million"), ("NOK", "NOK 75 million"),
    ("DKK", "DKK 75 million"), ("BRL", "R$ 40 million"), ("MXN", "MXN 90 million"), ("KES", "KES 800 million"),
    ("NGN", "NGN 4,000 million"), ("EGP", "EGP 100 million"), ("KWD", "KWD 5 million"), ("OMR", "OMR 5 million"),
    ("BHD", "BHD 5 million"), ("VND", "VND 900,000 million"), ("ILS", "ILS 60 million"), ("PLN", "PLN 30 million"),
    ("TRY", "TRY 45 million"), ("PHP", "PHP 1,000 million"), ("MUR", "MUR 300 million"),
    # the ones DPS knew before keep working
    ("USD", "US$ 10 million"), ("INR", "INR 500 crore"), ("LKR", "LKR 8,000 Mn"), ("AED", "AED 50 million"),
    ("EUR", "€ 20 million"), ("GBP", "£ 20 million"), ("SGD", "S$ 20 million"),
]


@pytest.mark.parametrize(("code", "text"), SAMPLES)
def test_every_currency_is_recognised(code, text):
    assert money.detect_currency(text) == code
    assert money.currency_before_amount(text) == code


def test_plain_words_are_not_currencies():
    for text in ("Try again later", "the PHP code", "Real estate", "a Rand Merchant", "RM form 3", "kr"):
        assert money.detect_currency(text) is None, text


def test_a_dollar_sign_of_another_country_is_not_usd():
    assert money.currency_before_amount("HK$ 25 million") == "HKD"
    assert money.currency_before_amount("$ 25 million") == "USD"
    assert money.currency_before_amount("Rs. 25 crore") == "INR"  # the loan's country decides elsewhere


def test_a_covenant_amount_keeps_its_currency():
    text = ("ARTICLE VII NEGATIVE COVENANTS\n7.1 Tangible Net Worth.\nThe Borrower shall maintain a Tangible Net Worth "
            "of not less than JPY 50,000 million.\n")
    tnw = next(t for t in extract_terms([Page(1, text, "TEXT", 1.0)]) if t.category == "FINANCIAL")
    assert tnw.threshold == 50000 and tnw.currency == "JPY"


def test_a_basket_in_swiss_francs():
    found = baskets("Liens securing Indebtedness not exceeding CHF 5,000,000 in aggregate at any time")
    assert found and found[0]["amount"] == 5_000_000 and found[0]["currency"] == "CHF"


def test_the_documents_currency_is_found():
    model = {"pages": [{"no": 1, "text": "FACILITY AGREEMENT for AUD 150,000,000 between ...", "blocks": []}]}
    assert [f.value for f in find_identifiers(model) if f.kind == "CURRENCY"] == ["AUD"]


def test_a_sanction_letter_in_hong_kong_dollars():
    from test_phase3_analyze import model_of

    from dps.skills import load

    pages = model_of("SANCTION LETTER\nSanctioned Limit: HKD 25 million\nTenor: 36 months\n")["pages"]
    amount = next(f for f in load("sanction").rules(pages, {}) if f["name"] == "SANCTIONED_AMOUNT")
    assert amount["value"]["currency"] == "HKD"


def test_every_way_of_writing_sri_lankan_rupees():
    for text in ("LKR 500,000,000", "SLRs 10,000,000", "SLR 25 mn", "SL Rs 25 mn", "SL Rs. 25 mn",
                 "Sri Lanka Rupees 1,000,000", "Sri Lankan Rupees Five Hundred Million"):
        assert money.detect_currency(text) == "LKR", text
    assert money.detect_currency("the SLA shall apply") is None


def test_a_bare_rs_is_the_rupee_the_document_names():
    """A Sri Lankan agreement names "Sri Lankan Rupees" once and then writes "Rs. 500 mn": that is LKR, not INR."""
    assert money.document_rupee("Facility of Sri Lankan Rupees 1,000 million (LKR 1,000 mn). Rs. 50 mn ...") == "LKR"
    assert money.document_rupee("Facility of Rs. 50 crore") == "INR"
    assert money.currency_before_amount("Rs. 500 mn", "LKR") == "LKR"
    text = ("Facility of Sri Lankan Rupees One Thousand Million (LKR 1,000 mn).\nARTICLE VII FINANCIAL COVENANTS\n"
            "7.1 Tangible Net Worth.\nThe Borrower shall maintain a Tangible Net Worth of not less than Rs. 500 "
            "million.\n")
    tnw = next(t for t in extract_terms([Page(1, text, "TEXT", 1.0)]) if t.category == "FINANCIAL")
    assert tnw.currency == "LKR"


def test_a_bare_dollar_or_yen_is_the_currency_the_document_names():
    """"$" is the US, Australian, Canadian, Singapore ... dollar; "¥" the yen or the yuan. The document says which."""
    assert money.document_currencies("Facility of AUD 150,000,000. ... not exceeding $5,000,000")["$"] == "AUD"
    assert money.document_currencies("“Dollars” and “$” mean lawful money of Canada. US$ 1 ... USD 2")["$"] == "CAD"
    assert money.document_currencies("Loan of RMB 800 million ... ¥50 million")["¥"] == "CNY"
    plain = money.document_currencies("A loan of $5,000,000 and ¥50 million and Rs. 5 crore")
    assert plain == {"$": "USD", "¥": "JPY", "Rs": "INR", "kr": None, "R": None, "Fr": None}  # names none: defaults
    # a US agreement that also mentions Canadian Dollars as an alternative currency stays in US dollars
    us = ("“Dollars” and “$” mean lawful money of the United States. “Alternative Currency” means Euro, Sterling "
          "or Canadian Dollars. Canadian Dollars ... Canadian Dollars ...")
    assert money.document_currencies(us)["$"] == "USD"
    assert money.currency_before_amount("$ 5,000,000", {"$": "AUD"}) == "AUD"
    assert money.currency_before_amount("US$ 5,000,000", {"$": "AUD"}) == "USD"  # a marked dollar keeps its own


def test_an_australian_covenant_in_bare_dollars_is_in_aud():
    text = ("Facility Agreement for AUD 150,000,000 (Australian Dollars).\nARTICLE VII FINANCIAL COVENANTS\n"
            "7.1 Tangible Net Worth.\nThe Borrower shall maintain a Tangible Net Worth of not less than "
            "$50,000,000.\n")
    tnw = next(t for t in extract_terms([Page(1, text, "TEXT", 1.0)]) if t.category == "FINANCIAL")
    assert tnw.currency == "AUD"


def test_a_currency_not_in_the_list_gives_no_currency_never_a_wrong_one():
    """"NT$" (Taiwan), "B$" (Brunei), "J$" (Jamaica) were read as US dollars; "KZT", "GHS" name no listed currency."""
    for text in ("NT$ 500 million", "B$ 5,000,000", "J$ 5,000,000", "KZT 500 million", "GHS 5,000,000",
                 "5,000,000 Kazakh tenge"):
        assert money.currency_before_amount(text) is None, text
        assert money.currency_after_amount(text.split(" ", 1)[1]) is None, text


def test_short_symbols_count_only_when_the_document_names_their_currency():
    """"kr", "R", "Fr." are too short to be sure of alone: SEK/NOK/DKK, ZAR, CHF only when the document names it."""
    for text in ("kr 5,000,000", "R 5,000,000", "Fr. 5,000,000"):
        assert money.currency_before_amount(text) is None, text
    local = money.document_currencies("Facility of NOK 100,000,000 (Norwegian Kroner). ZAR ... Swiss Francs")
    assert money.currency_before_amount("kr 5,000,000", local) == "NOK"
    assert money.currency_before_amount("R 5,000,000", local) == "ZAR"
    assert money.currency_before_amount("Fr. 5,000,000", local) == "CHF"
    assert money.currency_before_amount("R$ 5,000", local) == "BRL"  # the Brazilian real keeps its own symbol
    assert money.currency_before_amount("for 5 years", local) is None


def test_a_currency_written_after_the_amount():
    assert money.currency_after_amount(" million Sri Lankan Rupees") == "LKR"
    assert money.currency_after_amount(" euros") == "EUR"
    assert money.currency_after_amount(" crore Rupees", "LKR") == "LKR"
    for text in (" years", " Business Days", " to 1.00", " per cent"):
        assert money.currency_after_amount(text) is None, text
    found = baskets("Indebtedness not exceeding 5,000,000 euros in aggregate at any time")
    assert found and found[0]["amount"] == 5_000_000 and found[0]["currency"] == "EUR"


def test_a_sanction_letter_and_a_repayment_with_the_currency_after_the_amount():
    from test_phase3_analyze import model_of

    from dps.skills import load

    pages = model_of("SANCTION LETTER\nSanctioned Limit: 500 million Sri Lankan Rupees\nRepayable in 20 quarterly "
                     "instalments of 25 million Sri Lankan Rupees\n")["pages"]
    amount = next(f for f in load("sanction").rules(pages, {}) if f["name"] == "SANCTIONED_AMOUNT")
    assert amount["value"]["currency"] == "LKR"
    repay = next(f for f in load("schedule_terms").rules(pages, {}) if f["name"] == "REPAYMENT")
    assert repay["value"]["currency"] == "LKR"


def test_a_label_is_not_money():
    """"R", "kr", "Fr." count only as written and only before a real amount: "R1", "Schedule R 5" are labels."""
    local = money.document_currencies("ZAR 10,000,000 ... Swiss Francs ... NOK")
    for text in ("R1", "R 5", "Schedule R 5", "r 5,000,000", "(r) 5,000,000", "fr 5,000,000"):
        assert money.currency_before_amount(text, local) is None, text
    assert money.currency_before_amount("R 50 million", local) == "ZAR"
    assert money.currency_before_amount("Kr 5.000.000", local) == "NOK"
