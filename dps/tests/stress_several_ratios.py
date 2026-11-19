"""Several-ratios wording stress test: two financial covenants in one clause ("maintain a DSCR of not less than
1.25:1 and a Leverage Ratio of not more than 3.50:1"), each pair of six ratios in both orders, three floor and three
cap wordings, three joiners and two clause leads. Outcome: right (each ratio with its own operator and number and
no false SEVERAL_LEVELS flag) or bad. Before the fix (9 November 2026), 1,548 of 1,620 were bad.

  python tests/stress_several_ratios.py
"""

from __future__ import annotations

import itertools

from dps.extraction.agreement import extract_terms
from dps.parse.engine import Page

RATIOS = [("DSCR", "Debt Service Coverage Ratio", ">=", "1.25"), ("ICR", "Interest Coverage Ratio", ">=", "2.00"),
          ("DEBT_TO_EBITDA", "Total Debt to EBITDA", "<=", "3.50"),
          ("DEBT_TO_EQUITY", "Debt to Equity Ratio", "<=", "2.00"),
          ("CURRENT_RATIO", "Current Ratio", ">=", "1.33"), ("DEBT_TO_NET_WORTH", "TOL/TNW", "<=", "3.00")]
FLOOR = ["a {n} of not less than {v}:1", "a {n} of at least {v} times", "a minimum {n} of {v}x"]
CAP = ["a {n} of not more than {v}:1", "a {n} not exceeding {v} times", "a maximum {n} of {v}x"]
JOINERS = [" and ", "; and (b) ", ", "]
LEADS = ["12.1 Financial Covenants. The Borrower shall maintain ", "12.1 The Borrower shall maintain (a) "]


def _part(ratio: tuple[str, str, str, str], form: int) -> str:
    return (FLOOR if ratio[2] == ">=" else CAP)[form].format(n=ratio[1], v=ratio[3])


def results() -> dict:
    out: dict = {"right": 0, "bad": []}
    for a, b in itertools.permutations(RATIOS, 2):
        for fa, fb, joiner, lead in itertools.product(range(3), range(3), JOINERS, LEADS):
            text = lead + _part(a, fa) + joiner + _part(b, fb) + "."
            terms = extract_terms([Page(1, text, "TEXT", 0.9)])
            want = {(a[0], a[2], float(a[3])), (b[0], b[2], float(b[3]))}
            got = {(t.metric, t.operator, t.threshold) for t in terms}
            if got == want and not any(t.issues for t in terms):
                out["right"] += 1
            else:
                out["bad"].append((text, sorted(got ^ want), [i for t in terms for i in t.issues]))
    return out


if __name__ == "__main__":
    r = results()
    print({"right": r["right"], "bad": len(r["bad"])})
    for b in r["bad"][:20]:
        print("  ", b)
