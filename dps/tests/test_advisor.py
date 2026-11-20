from __future__ import annotations

import json

from dps.advisory.advisor import ADVISORY, Advisor, deterministic

COVS = [
    {"appliedCovenantId": "a1", "latestResult": "COMPLIANT"},
    {"appliedCovenantId": "a2", "latestResult": "BREACHED", "conditionExpression": "DSCR >= 1.25",
     "computedValue": "1.1"},
    {"appliedCovenantId": "a3", "latestResult": "BREACHED"},
    {"appliedCovenantId": "a4", "latestResult": "ERROR"},
]


def test_deterministic_mirrors_results():
    out = {r["appliedCovenantId"]: r for r in deterministic(COVS)}
    assert out["a1"]["recommendedStatus"] == "COMPLIED"
    assert "actual 1.1" in out["a2"]["rationale"] and ADVISORY in out["a2"]["rationale"]
    assert out["a3"]["rationale"].startswith("Latest assessment breached.")
    assert "a4" not in out


def test_llm_output_is_validated_and_bad_output_falls_back():
    reply = json.dumps([
        {"appliedCovenantId": "a2", "recommendedStatus": "waiver", "rationale": "small", "confidence": 140},
        {"appliedCovenantId": "zz", "recommendedStatus": "WAIVER"},
        {"appliedCovenantId": "a1", "recommendedStatus": "MAYBE"},
        {"appliedCovenantId": "a3", "recommendedStatus": "BREACHED", "confidence": "high"},
        "junk",
    ])
    seen = []
    advisor = Advisor(lambda messages: seen.append(messages) or f"Here you go: {reply}")
    assert advisor.ai_enabled
    out = advisor.suggest("CP-1", COVS + [{"no": "id"}])
    assert [(r["appliedCovenantId"], r["recommendedStatus"], r["confidence"]) for r in out] == [
        ("a2", "WAIVER", 100.0), ("a3", "BREACHED", None)]
    assert json.loads(seen[0][1]["content"])["cpId"] == "CP-1"

    assert Advisor(lambda m: "no json here").suggest("CP-1", COVS) == deterministic(COVS)

    def boom(messages):
        raise TimeoutError

    assert Advisor(boom).suggest("CP-1", COVS) == deterministic(COVS)
    assert Advisor().suggest("CP-1", []) == []


def test_narrative_rules_and_ai():
    values = [{"key": "EBITDA", "value": 50}, {"key": "INTEREST_PAID", "value": 10},
              {"key": "TOTAL_DEBT", "value": 150}, {"key": "NET_WORTH", "value": 300},
              {"key": "CURRENT_ASSETS", "value": 60}, {"key": "CURRENT_LIABILITIES", "value": 40}]
    rules = Advisor().narrative(values)
    assert rules["method"] == "RULES"
    assert "5.00 times" in rules["text"] and "Current ratio is 1.50" in rules["text"]
    assert Advisor().narrative([])["text"] == "Not enough line items to comment on."
    assert Advisor(lambda m: " Strong year. ").narrative(values) == {
        "text": "Strong year.", "method": "AI", "advisory": ADVISORY}

    def boom(messages):
        raise RuntimeError

    assert Advisor(boom).narrative(values)["method"] == "RULES"


def test_the_narrative_names_the_figures_own_currency():
    """Money for any country: an LKR statement is not summarised as rupees crore."""
    from dps.advisory.advisor import describe_unit

    seen = []
    figures = [{"key": "EBITDA", "value": 800, "unit": "LKR_MN"}]
    Advisor(lambda m: seen.append(m[0]["content"]) or "ok").narrative(figures)
    assert "(LKR million)" in seen[0]
    assert (describe_unit("INR_CR"), describe_unit("x"), describe_unit(None)) == ("INR crore", "x", "")

