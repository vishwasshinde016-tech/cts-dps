"""Phase 1 review (22 Nov 2026): the parts the first review did not cover (API security, worker, statements, packs)."""

from __future__ import annotations

from conftest import h

from dps.models import Batch

SUPPORT = h("ops-1", "SUPPORT")


def _other_tenants_batch(app) -> str:
    with app.state.db.session_factory() as s:
        batch = Batch(tenant_id="another-bank", batch_ref="b-x", idempotency_key="k-other-tenant-1",
                      request_hash="0" * 64, item_count=1, created_by="someone", status="COMPLETED")
        s.add(batch)
        s.commit()
        return batch.id


def test_another_tenants_batch_does_not_exist_here(client, app):
    """GET /batches/{id} answered 404 for another tenant's batch, but errors.csv listed its jobs and rerun re-queued
    them (full review G2: another tenant's data does not exist here)."""
    batch_id = _other_tenants_batch(app)
    assert client.get(f"/dps/v1/batches/{batch_id}", headers=SUPPORT).status_code == 404
    assert client.get(f"/dps/v1/batches/{batch_id}/errors.csv", headers=SUPPORT).status_code == 404
    assert client.post(f"/dps/v1/batches/{batch_id}/rerun", headers=SUPPORT).status_code == 404


# ---------- a negative-covenant list item ("Permit the ... to be greater than") is a maximum ----------
from dps.extraction.agreement import extract_terms  # noqa: E402
from dps.parse.engine import Page  # noqa: E402

LIENS = ("Create, incur or suffer to exist any Lien upon any of its property, other than Permitted Liens, "
         "including liens for taxes not yet due and liens of landlords and carriers. ") * 6
LIST_FORM = ("ARTICLE VII NEGATIVE COVENANTS\nThe Borrower shall not, nor shall it permit any Subsidiary to, "
             "directly or indirectly:\n7.1 Liens. " + LIENS
             + "\n7.11 Financial Covenants.\n(a) Consolidated Total Leverage Ratio. Permit the Consolidated "
             "Total Leverage Ratio as of the end of any Measurement Period to be greater than 2.25 to 1.0.\n"
             "(b) Consolidated Interest Coverage Ratio. Permit the Consolidated Interest Coverage Ratio as of the end "
             "of any fiscal quarter to be less than 2.50:1.00.\n")


def test_a_permit_list_item_under_shall_not_is_read_the_right_way():
    """Meet Group and Holly (real agreements): read as "> 2.25" and "< 2.50"; the covenants are <= 2.25 and >= 2.50."""
    terms = {t.metric: t for t in extract_terms([Page(1, LIST_FORM, "TEXT", 1.0)]) if t.category == "FINANCIAL"}
    assert (terms["DEBT_TO_EBITDA"].operator, terms["DEBT_TO_EBITDA"].threshold) == ("<=", 2.25)
    assert (terms["ICR"].operator, terms["ICR"].threshold) == (">=", 2.5)


UK_STYLE = ("26.2 Financial condition\nThe Parent shall ensure that:\n(a) Adjusted Net Leverage: Adjusted Net "
            "Leverage in respect of any Relevant Period specified in column 1 below shall not exceed the ratio set out "
            "in column 2 below opposite that Relevant Period.\nColumn 1 Relevant Period ending on Column 2 Ratio\n"
            "31 July 2026 4.75:1\n31 October 2026 4.50:1\n31 January 2027 4.25:1\n(b) Interest Cover: Interest Cover "
            "in respect of any Relevant Period shall not be less than 3.00:1.\n")


def test_lma_style_financial_covenants_are_found():
    """A UK facility agreement (LMA wording, 396 pages): "Adjusted Net Leverage ... shall not exceed the ratio set out
    in column 2 below" and "Interest Cover ... shall not be less than" were missed."""
    terms = {t.metric: t for t in extract_terms([Page(1, UK_STYLE, "TEXT", 1.0)]) if t.category == "FINANCIAL"}
    assert terms["NET_LEVERAGE_RATIO"].operator == "<=" and terms["NET_LEVERAGE_RATIO"].threshold in (4.75, 4.25)
    assert (terms["ICR"].operator, terms["ICR"].threshold) == (">=", 3.0)


# ---------- numbers that are not covenants (pricing grids, definitions, periods, lists of other parties) ----------
NOT_COVENANTS = [
    ("pricing grid", "1.01 Applicable Rate.\nthe following percentages per annum, based upon the Consolidated Total "
                     "Leverage Ratio: Pricing Tier Consolidated Total Leverage Ratio Commitment Fee Letter of Credit "
                     "Fee "
                     "I > 2.00 to 1.0 0.40% 3.00% II < 2.00 to 1.0 but > 1.50 to 1.0 0.35% 2.50%\n"),
    ("definition", "1.1 Definitions\n“Maximum Indebtedness Amount” means Adjusted Net Leverage of 3.10:1 for "
                   "the Relevant Period ending on the most recent Quarter Date.\n"),
    ("definition far above", "1.01 Defined Terms.\n“Designated Restricted Payment” means any Restricted Payment "
                             "made in cash. It shall not include a payment in any Measurement Period for which the "
                             "Company certifies that the Consolidated Total Leverage Ratio is less than "
                             "1.00 to 1.0.\n"),
    ("list of other parties", "ii. The following users shall be eligible to be classified as non-retail users: a. All "
                              "regulated entities b. Exim Bank c. Companies with a minimum net worth of Rs 500 crores "
                              "d. Persons resident outside India\n"),
    ("definition outside the definitions clause", "28.6 Insolvency\nThe Majority Lenders may waive it. "
                                                  "“Maximum Indebtedness Amount ” means Adjusted Net Leverage "
                                                  "of 3.10:1 for the last Relevant Period.\n"),
    ("an incurrence condition", "7.06 Restricted Payments.\nthe Company may make Restricted Payments if, on the date "
                                "of determination, for the Measurement Period most recently ended on or prior to such "
                                "date, the Consolidated Total Leverage Ratio is less than 1.00 to 1.0.\n"),
    ("an EBITDA in a definition", "“Permitted Acquisition” means an acquisition of a target whose EBITDA "
                                  "(for the purpose of this definition, “LTM EBITDA”) is positive or "
                                  "negative (and, if negative, in an amount not exceeding -£5,000,000).\n"),
    ("a number of days", "18.2 Tax gross-up.\nA Treaty Lender that has included its Interest Cover confirmation at "
                         "least 15 Business Days prior to the date of such payment.\n"),
    ("a number of days split by the PDF text", "18.2 Tax gross-up.\nA Treaty Lender that has included its Interest "
                                               "Cover confirmation at least 15 Business D ays prior to the payment.\n"),
]


def test_numbers_that_are_not_covenants_are_not_read_as_covenants():
    """False findings of the bulk run on real agreements (22 Nov 2026): Meet Group's pricing grid and a definition, the
    UK agreement's "Maximum Indebtedness Amount" definition and "15 Business Days", an Indian agreement's list of
    who may be a non-retail user."""
    for label, text in NOT_COVENANTS:
        found = [(t.metric, t.operator, t.threshold) for t in extract_terms([Page(1, text, "TEXT", 1.0)])
                 if t.category == "FINANCIAL"]
        assert found == [], (label, found)
