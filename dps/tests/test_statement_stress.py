"""Gate: every statement label variant in tests/stress_statements.py is read as its line, and no look-alike (other
current assets, current maturities, interest income, cash at the beginning of the year ...) is taken for one."""

from __future__ import annotations

from stress_statements import results


def test_every_label_variant_is_read_and_no_look_alike_is_taken():
    r = results()
    assert (r["unmatched"], r["wrong"], r["lookalike_wrong"]) == (0, 0, 0), r["bad"][:10]
