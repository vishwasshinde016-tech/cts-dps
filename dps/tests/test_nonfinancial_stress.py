"""Gate: every non-financial covenant wording in tests/stress_nonfinancial.py is proposed, in a numbered clause, a
list item or a plain paragraph (before 8 November 2026 only 10 of 72 were)."""

from __future__ import annotations

from stress_nonfinancial import results

from dps.extraction.agreement import extract_terms
from dps.parsing import Page


def test_every_non_financial_wording_is_proposed():
    r = results()
    assert r["missed"] == 0, r["bad"][:10]


def test_a_type_found_outside_a_numbered_clause_is_confirmed_by_a_person():
    text = "The Borrower shall not create, incur, assume or permit to exist any Lien on any of its property or assets."
    [term] = [t for t in extract_terms([Page(7, text, "TEXT", 1.0)]) if t.metric == "NEGATIVE_PLEDGE"]
    assert term.confidence < 0.8 and "Lien" in term.quote
