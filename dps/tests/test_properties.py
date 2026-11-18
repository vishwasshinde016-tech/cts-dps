"""Phase 6 (12 November 2026): property tests. Thousands of seeded random cases for the arithmetic every figure goes
through: money units, number styles and scales, the grounding checks and the personal-data mask. A failure prints its
seed and case, so it can be run again exactly."""

# ruff: noqa: S311 - seeded test data, not secrets

from __future__ import annotations

import random

import pytest

from dps.ai_guard import mask_pii
from dps.extraction.statement import detect_number_style, number_style, parse_number
from dps.extraction.verify import numbers_in, threshold_in
from dps.money import SCALE_UNITS, currency_of, money_unit, to_money_unit, unit_size

CURRENCIES = ["INR", "LKR", "USD", "AED", "EUR", "GBP", "KES", "SGD"]
SCALES = ["unit", "hundred", "thousand", "lakh", "million", "crore", "billion"]
N = 2000


def _western(value: float, decimals: int) -> str:
    return f"{value:,.{decimals}f}"


def _european(value: float, decimals: int) -> str:
    return _western(value, decimals).translate(str.maketrans(",.", ".,"))


def _indian(value: float) -> str:
    whole, _, frac = f"{abs(value):.2f}".partition(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ("-" if value < 0 else "") + ",".join(groups + [tail]) + "." + frac if groups else f"{value:.2f}"


@pytest.mark.parametrize("seed", range(3))
def test_money_units_round_trip(seed):
    rng = random.Random(seed)
    for _ in range(N):
        ccy, scale = rng.choice(CURRENCIES), rng.choice(SCALES)
        printed = round(rng.uniform(-1e6, 1e6), 2)
        stored = to_money_unit(printed, scale, ccy)
        back = stored * unit_size(ccy) / SCALE_UNITS[scale]
        assert back == pytest.approx(printed, rel=1e-12, abs=1e-9), (seed, ccy, scale, printed)
        assert currency_of(money_unit(ccy)) == ccy
        rate = rng.uniform(0.001, 500)
        assert to_money_unit(printed, scale, ccy, rate) == pytest.approx(stored * rate, rel=1e-12, abs=1e-9)


@pytest.mark.parametrize("seed", range(3))
def test_every_number_style_reads_back_the_same_value(seed):
    rng = random.Random(seed)
    for _ in range(N):
        value = round(rng.uniform(-9e7, 9e7), 2)
        assert parse_number(_western(value, 2)) == pytest.approx(value), (seed, value)
        assert parse_number(f"({_western(abs(value), 2)})") == pytest.approx(-abs(value)), (seed, value)
        assert parse_number(_indian(value)) == pytest.approx(value), (seed, value, _indian(value))
        with number_style("EUROPEAN"):
            assert parse_number(_european(value, 2)) == pytest.approx(value), (seed, value)
        assert value in [pytest.approx(n) for n in numbers_in(f"Revenue {_western(value, 2)}")] or value < 0


@pytest.mark.parametrize("seed", range(3))
def test_a_document_written_in_one_style_is_told_apart(seed):
    rng = random.Random(seed)
    for _ in range(200):
        amounts = [round(rng.uniform(1000, 9e6), 2) for _ in range(rng.randint(3, 15))]
        assert detect_number_style(" ".join(_western(a, 2) for a in amounts)) == "WESTERN", (seed, amounts)
        assert detect_number_style(" ".join(_european(a, 2) for a in amounts)) == "EUROPEAN", (seed, amounts)


@pytest.mark.parametrize("seed", range(3))
def test_a_threshold_is_found_in_its_quote_in_any_common_form(seed):
    rng = random.Random(seed)
    for _ in range(N):
        t = round(rng.uniform(0.5, 9.5), 2)
        quote = rng.choice([f"not less than {t:.2f}:1.00", f"not exceed {t:.2f} to 1.00", f"at least {t:.2f}x",
                            f"au moins égal à {t:.2f}".replace(".", ","), f"of {t:.2f} times"])
        assert threshold_in(quote, t), (seed, quote, t)
        assert not threshold_in(quote, round(t + 0.37, 2)), (seed, quote, t)


@pytest.mark.parametrize("seed", range(3))
def test_the_mask_never_touches_figures_ratios_years_or_ids(seed):
    rng = random.Random(seed)
    for _ in range(N):
        parts = [_western(round(rng.uniform(0, 9e9), 2), 2), _indian(round(rng.uniform(0, 9e9), 2)),
                 f"{rng.uniform(0.5, 9):.2f}x", f"{rng.randint(1990, 2040)} {rng.randint(1990, 2040)}",
                 f"{rng.uniform(-50, 50):+.1f}%", f"breach:{rng.getrandbits(32):08x}-{rng.randint(0, 9999):04d}-"
                 f"{rng.randint(0, 9999):04d}-{rng.randint(0, 9999):04d}-{rng.getrandbits(48):012x}"]
        text = " ; ".join(parts)
        assert mask_pii(text) == text, (seed, text)
