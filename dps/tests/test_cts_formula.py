"""CTS integration, P1 (23 Nov 2026): the covenant as the bank app's formula, built on the document's own threshold.

The bank app (covenant-service) applies value.expression as a CP's condition. Its language is MEASURE OP NUMBER with its
own measure codes, '==' for equality, plain decimals, amounts in INR crore and percentages as points."""

from __future__ import annotations

from datetime import date

from test_phase3_analyze import model_of
from test_phase4_skills import Tools, fake_job

from dps.extraction.agreement import extract_terms
from dps.parse.engine import Page
from dps.skills import app_formula, load
from dps.skills import runner as skills

LIBRARY = [
    {"id": "c-dscr", "name": "Minimum DSCR", "aliases": ["DSCR", "FIN_DSCR"], "metricKeys": ["DSCR"],
     "expression": "DSCR >= 1.50", "active": True},
    {"id": "c-icr", "name": "Minimum Interest Coverage", "aliases": ["ICR", "FIN_INTEREST_COVERAGE"],
     "metricKeys": ["INTEREST_COVER"], "expression": "INTEREST_COVER >= 4.00", "active": True},
    {"id": "c-tnw", "name": "Minimum Tangible Net Worth", "aliases": ["MIN_TANGIBLE_NET_WORTH"],
     "metricKeys": ["TANGIBLE_NET_WORTH"], "expression": "TANGIBLE_NET_WORTH >= 45000", "active": True},
]


def cov(metric, op, threshold, unit=None, name=None, **value):
    return {"id": f"f-{metric}", "group": "FINANCIAL_COVENANT", "name": name or metric, "issues": [],
            "status": "NEEDS_REVIEW", "source": {"page": 1, "quote": "q"},
            "value": {"metric": metric, "operator": op, "threshold": threshold, "unit": unit, "expression": None,
                      **value}}


# ---------- numbers and operators

def test_numbers_are_plain_decimals_never_exponent():
    assert app_formula.plain_number(5_000_000.0) == "5000000"
    assert app_formula.plain_number(1.25) == "1.25"
    assert app_formula.plain_number(3.0) == "3"
    assert app_formula.plain_number(1234567.891) == "1234567.891"
    assert app_formula.plain_number(-0.5) == "-0.5"
    assert app_formula.plain_number(12) == "12"


def test_operators_are_normalised_and_equality_is_double_equals():
    n = app_formula.normalise_operator
    assert n("≥") == ">=" and n("minimum") == ">=" and n("At Least") == ">=" and n("≤") == "<=" and n("max") == "<="
    assert n("in excess of") == ">" and n("=") == "=" and n("==") == "=" and n("roughly") is None and n(None) is None
    f = cov("DSCR", "=", 2.0)
    app_formula.build(f, None)
    assert f["value"]["expression"] == "DSCR == 2"


# ---------- fix 1: the document's threshold, never the library master's

def test_the_formula_carries_the_documents_threshold_not_the_library_master():
    f = cov("DSCR", ">=", 1.25, name="Debt Service Coverage Ratio", code="FIN_DSCR")
    app_formula.apply([f], LIBRARY)
    assert f["libraryMatch"]["covenantMasterId"] == "c-dscr"
    assert f["libraryMatch"]["expression"] == "DSCR >= 1.50"  # the master, for comparison only
    assert f["value"]["expression"] == "DSCR >= 1.25"  # what the agreement says
    assert f["value"]["mapping"]["mappable"] is True


# ---------- fix 2: the app's measure code; two masters on one measure

def test_the_formula_uses_the_apps_measure_code_from_the_library():
    f = cov("ICR", ">=", 2.5, name="Interest Coverage Ratio", code="FIN_INTEREST_COVERAGE")
    app_formula.apply([f], LIBRARY)
    assert f["libraryMatch"]["covenantMasterId"] == "c-icr"
    assert f["value"]["expression"] == "INTEREST_COVER >= 2.5"
    assert f["value"]["mapping"]["measure"] == "INTEREST_COVER"


def test_two_library_covenants_on_one_measure_are_resolved_by_name_or_left_to_a_person():
    library = LIBRARY + [{"id": "c-dsr3", "name": "DSR3", "aliases": None, "metricKeys": ["DSCR"],
                          "expression": "DSCR > 1.9", "active": True}]
    named = cov("DSCR", ">=", 1.25, name="Minimum DSCR")
    app_formula.apply([named], library)
    assert named["libraryMatch"]["covenantMasterId"] == "c-dscr"  # shares the words of "Minimum DSCR"
    dsr3 = cov("DSCR", ">=", 1.25, name="Ratio")  # "DSCR" is a word of "Minimum DSCR", none of "DSR3"
    app_formula.apply([dsr3], library)
    assert dsr3["libraryMatch"]["covenantMasterId"] == "c-dscr"
    twins = [{"id": "c-a", "name": "Covenant A", "metricKeys": ["DSCR"]},
             {"id": "c-b", "name": "Covenant B", "metricKeys": ["DSCR"]}]
    tie = cov("DSCR", ">=", 1.25, name="Ratio")
    app_formula.apply([tie], twins)
    assert tie["libraryMatch"]["covenantMasterId"] is None
    assert {c["covenantMasterId"] for c in tie["libraryMatch"]["candidates"]} == {"c-a", "c-b"}
    assert "LIBRARY_AMBIGUOUS" in {i["code"] for i in tie["issues"]}


def test_a_library_with_empty_lists_or_no_id_does_not_crash():
    index = app_formula.library_index([{"id": None, "name": "x"}, {"name": "no id"},
                                       {"id": "a", "name": "A", "aliases": None, "metricKeys": None}, "junk"])
    assert list(index) == ["a"]


# ---------- fixes 5 and 6: units

def test_amounts_are_written_in_inr_crore():
    crore = cov("MIN_TANGIBLE_NET_WORTH", ">=", 500, "crore", name="Minimum Tangible Net Worth")
    lakh = cov("MIN_NET_WORTH", ">=", 50, "lakh")
    rupees = cov("MIN_NET_WORTH", ">=", 5_000_000_000, None, currency="INR")
    usd = cov("MIN_NET_WORTH", ">=", 10, "million", currency="USD")
    app_formula.apply([crore, lakh, rupees, usd], LIBRARY)
    assert crore["value"]["expression"] == "TANGIBLE_NET_WORTH >= 500" and crore["value"]["mapping"]["unit"] == "INR_CR"
    assert lakh["value"]["expression"] == "MIN_NET_WORTH >= 0.5"
    assert rupees["value"]["expression"] == "MIN_NET_WORTH >= 500"
    assert usd["value"]["expression"] is None and usd["value"]["mapping"]["mappable"] is False
    assert any(r.startswith("AMOUNT_CURRENCY") for r in usd["value"]["mapping"]["reasons"])


def test_percentages_are_points_not_the_percent_sign():
    f = cov("LOAN_TO_VALUE", "<=", 75.0, "%")
    app_formula.build(f, None)
    assert f["value"]["expression"] == "LOAN_TO_VALUE <= 75" and "%" not in f["value"]["expression"]


# ---------- fix 8: no junk formulas

def test_no_formula_for_an_unknown_metric_or_a_missing_part():
    for f in (cov("OTHER", ">=", 2), cov("FIN_OTHER", ">=", 2), cov("Debt Service Coverage", ">=", 2),
              cov("DSCR", None, 2), cov("DSCR", ">=", None)):
        app_formula.build(f, None)
        assert f["value"]["expression"] is None and f["value"]["mapping"]["mappable"] is False


# ---------- fixes 11 and 12: step-downs, conditions, scope

def test_a_step_down_uses_the_level_in_force_and_is_not_mappable_alone():
    schedule = [{"from": "March 31, 2025", "to": None, "threshold": 4.75},
                {"from": "March 31, 2026", "to": None, "threshold": 4.25},
                {"from": "March 31, 2027", "to": None, "threshold": 3.75}]
    f = cov("NET_LEVERAGE_RATIO", "<=", 4.75, schedule=schedule)
    app_formula.build(f, None, as_of=date(2026, 6, 30))
    assert f["value"]["expression"] is None  # not mappable alone: no formula until a person confirms the level
    assert any("STEP_DOWN" in r and "2026-03-31" in r for r in f["value"]["mapping"]["reasons"])
    assert app_formula.level_in_force(schedule, date(2026, 6, 30)) == (4.25, "2026-03-31")
    assert app_formula.level_in_force(schedule, date(2024, 1, 1)) == (4.75, None)


def test_conditions_qualifiers_and_another_obligor_make_the_formula_not_mappable():
    cond = cov("DSCR", ">=", 1.1, conditions=[{"text": "when Excess Availability is below USD 10 million"}])
    qual = cov("DSCR", ">=", 1.1, qualifiers=["subject to Clause 21.3"])
    guar = cov("DSCR", ">=", 1.1, scope={"obligor": "Guarantor", "basis": "CONSOLIDATED"})
    plain = cov("DSCR", ">=", 1.1, scope={"obligor": "Borrower", "basis": "CONSOLIDATED"})
    for f in (cond, qual, guar, plain):
        app_formula.build(f, None)
    assert [f["value"]["mapping"]["mappable"] for f in (cond, qual, guar, plain)] == [False, False, False, True]
    assert any(r.startswith("SCOPE") for r in guar["value"]["mapping"]["reasons"])
    assert any(n.startswith("BASIS") for n in plain["value"]["mapping"]["notes"])


# ---------- fixes 3, 4, 7 end to end (rules, stamp, enrich, then the formula and Java's check)

def test_a_real_run_builds_the_formula_after_the_final_metric_and_checks_it():
    page = ("21. FINANCIAL COVENANTS\n21.1 The Borrower shall maintain a Debt Service Coverage Ratio of not less "
            "than 1.25 times.\n21.2 The Borrower shall maintain an Interest Coverage Ratio of at least 2.50 times.")
    tools = Tools()
    tools.covenant_library = lambda: (LIBRARY, True)
    result = skills.run(load("covenants"), fake_job("EXTRACT_AGREEMENT"), model_of(page), None, tools)
    by_metric = {f["value"]["metric"]: f for f in result["fields"] if f["group"] == "FINANCIAL_COVENANT"}
    assert by_metric["DSCR"]["value"]["expression"] == "DSCR >= 1.25"
    assert by_metric["ICR"]["value"]["expression"] == "INTEREST_COVER >= 2.5"
    assert ("f", "DSCR >= 1.25") not in tools.validated and any(e == "DSCR >= 1.25" for _, e in tools.validated)
    assert all("e+" not in (e or "") for _, e in tools.validated)


def test_an_invalid_formula_is_not_mappable():
    tools = Tools(valid=False)
    tools.covenant_library = lambda: (LIBRARY, True)
    page = "The Borrower shall maintain a Debt Service Coverage Ratio of not less than 1.25 times."
    result = skills.run(load("covenants"), fake_job("EXTRACT_AGREEMENT"), model_of(page), None, tools)
    dscr = next(f for f in result["fields"] if f["value"].get("metric") == "DSCR")
    assert dscr["value"]["mapping"]["mappable"] is False and "FORMULA_INVALID" in {i["code"] for i in dscr["issues"]}


# ---------- fix 9: the agent's operator

def test_the_agents_operator_is_normalised_or_sent_to_a_person():
    pack = load("covenants")
    pages = [{"no": 1, "text": "The Borrower shall keep the leverage at or under 3.5x."}]
    props = [{"name": "Leverage", "page": 1, "quote": "keep the leverage at or under 3.5x",
              "value": {"metric": "NET_LEVERAGE_RATIO", "operator": "≤", "threshold": "3.5x",
                        "expression": "whatever the model wrote"}},
             {"name": "Other", "page": 1, "quote": "keep the leverage at or under 3.5x",
              "value": {"metric": "DSCR", "operator": "roughly", "threshold": 1}}]
    out = skills._agent_only(pack, pages, [], props)
    assert out[0]["value"]["operator"] == "<=" and out[0]["value"]["threshold"] == 3.5
    assert out[0]["value"]["expression"] is None  # DPS builds it
    assert out[1]["value"]["operator"] is None and "OPERATOR_UNREAD" in {i["code"] for i in out[1]["issues"]}


# ---------- fix 13: a second covenant in one sentence is kept

def test_a_second_covenant_in_the_same_sentence_is_not_merged_away():
    quote = "maintain a DSCR of at least 1.25 and a Leverage Ratio of at most 3.5"
    rules = {"id": "r", "group": "FINANCIAL_COVENANT", "name": "DSCR", "method": "REGEX", "issues": [],
             "source": {"page": 1, "quote": quote}, "value": {"metric": "DSCR", "code": "FIN_DSCR", "threshold": 1.25}}
    ai_other = {"id": "a", "group": "FINANCIAL_COVENANT", "name": "Leverage", "method": "AI", "issues": [],
                "source": {"page": 1, "quote": quote},
                "value": {"metric": "NET_LEVERAGE_RATIO", "code": "FIN_NET_LEVERAGE", "threshold": 3.5}}
    ai_same = {"id": "b", "group": "FINANCIAL_COVENANT", "name": "DSCR", "method": "AI", "issues": [],
               "source": {"page": 1, "quote": quote}, "value": {"metric": "FIN_OTHER", "code": "FIN_OTHER"}}
    kept = skills.merge_same_sentence([rules, ai_other, ai_same])
    assert [f["id"] for f in kept] == ["r", "a"]


# ---------- fix 10: wording

def _terms(text):
    return extract_terms([Page(1, text, "TEXT", 1.0)])


def test_in_excess_of_and_negative_numbers_and_breach_wording():
    above = _terms("The Borrower shall maintain a Tangible Net Worth in excess of INR 500 crore at all times.")
    assert [(t.operator, t.threshold) for t in above if t.metric == "MIN_TANGIBLE_NET_WORTH"] == [(">", 500.0)]
    capped = _terms("The Borrower shall ensure that the Leverage Ratio shall not be in excess of 3.50:1.")
    assert [(t.operator, t.threshold) for t in capped if t.category == "FINANCIAL"] == [("<=", 3.5)]
    from dps.extraction.agreement import _number
    assert _number("−0.50") == -0.5
    from dps.skills.covenants.rules import breach_to_covenant
    f = {"value": {"operator": "<", "threshold": 1.2}, "issues": [], "status": "NEEDS_REVIEW",
         "source": {"quote": "If the Debt Service Coverage Ratio is less than 1.20, the Borrower shall cure it."}}
    breach_to_covenant(f)
    assert f["value"]["operator"] == ">=" and "BREACH_WORDING" in {i["code"] for i in f["issues"]}
