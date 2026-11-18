"""Gate: no covenant wording in the stress grid (tests/stress_covenants.py) is missed or wrong without a flag, and no
distractor (pricing grid, incurrence test, basket, definition ...) becomes a covenant that looks clean.

The full grid has about 12,000 cases (python tests/stress_covenants.py prints it); CI runs every third one.
"""

from __future__ import annotations

import pytest
from stress_covenants import DISTRACTORS, covenant_cases, distractor_outcome, outcome

CASES = covenant_cases()[::3]


def test_every_covenant_wording_reaches_a_person():
    failures = [(outcome(c), c.label) for c in CASES]
    failures = [f for f in failures if f[0] in ("missed", "silent_wrong")]
    assert failures == [], f"{len(failures)} of {len(CASES)}: {failures[:10]}"


@pytest.mark.parametrize("text", DISTRACTORS, ids=[d[:40] for d in DISTRACTORS])
def test_a_distractor_is_never_a_clean_covenant(text):
    assert distractor_outcome(text) == "ok"


def test_every_wording_of_a_known_covenant_is_read_exactly():
    # Since 9 November 2026 (ratios in words, a bare first level in a step-down table, minimum EBITDA outside an
    # "EBITDA" clause). Covenants DPS has no metric for (family None) are shown to a person instead.
    wrong = [(outcome(c), c.label) for c in CASES if c.family is not None]
    wrong = [w for w in wrong if w[0] != "found"]
    assert wrong == [], f"{len(wrong)}: {wrong[:10]}"
