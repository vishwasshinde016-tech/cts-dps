"""Gate: two covenants in one clause each get their own operator and number, with no false SEVERAL_LEVELS flag
(tests/stress_several_ratios.py; before 9 November 2026, 1,548 of 1,620 were wrong or flagged)."""

from __future__ import annotations

from stress_several_ratios import results


def test_each_ratio_in_a_clause_gets_its_own_number():
    r = results()
    assert not r["bad"], r["bad"][:5]
    assert r["right"] == 1620
