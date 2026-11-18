"""The covenant taxonomy (18 Nov 2026): loader checks, overlays, alias rules, the four focused covenant parts, taxonomy
codes and defaults on result fields, events of default as their own fields, coverage families."""

from __future__ import annotations

import pytest
from test_jobapi import check
from test_phase3_analyze import model_of
from test_phase4_skills import Tools, fake_job
from test_recall import Salvage

from dps.skills import load
from dps.skills import runner as skills
from dps.skills.covenants import codes, coverage, names, parts
from dps.skills.covenants import taxonomy as tx

CATS = {"version": "t1", "categories": [{"code": "financial", "name": "Financial"},
                                        {"code": "negative", "name": "Negative"},
                                        {"code": "event_of_default", "name": "Events of default"}]}


def doc(*types, **extra):
    return {**CATS, "types": list(types), **extra}


# ---------- loader ----------

def test_the_shipped_taxonomy_loads_with_the_overlay_and_maps_every_old_dps_name():
    t = tx.get()
    assert t.version == "2026.11.0+dps.1" and len(t.types) == 98
    for new in ("FIN_EBITDA_TO_EMI", "FIN_MIN_TANGIBLE_NET_WORTH", "FIN_MIN_COLLATERAL_VALUE", "NEG_DISPOSAL_CAP",
                "AFF_SUBORDINATION", "NEG_MATERIAL_CONTRACTS"):
        assert t.get(new) is not None, new
    assert {k for k in names.LEGACY_KNOWN if k != names.OTHER} <= set(t.by_dps_key)
    assert t.code_for("DSCR") == "FIN_DSCR" and t.dps_key("FIN_DSCR") == "DSCR"
    assert t.code_for("DEBT_TO_NET_WORTH") == "FIN_DEBT_TO_EQUITY"
    assert t.code_for("INR 50 Cr") is None


@pytest.mark.parametrize("bad, problem", [
    ({"code": "FIN_X", "category": "nope", "name": "x"}, "unknown category"),
    ({"code": "FIN_X", "category": "financial", "name": "x", "default_operator": "=>"}, "default_operator"),
    ({"code": "FIN_X", "category": "financial", "name": "x", "unit": "parsecs"}, "unit"),
    ({"code": "fin_x", "category": "financial", "name": "x"}, "UPPER_SNAKE"),
])
def test_a_bad_type_stops_the_load(bad, problem):
    with pytest.raises(tx.TaxonomyError, match=problem):
        tx.build([("t", doc(bad))])


def test_an_alias_on_two_types_of_one_category_or_a_dps_key_on_two_codes_stops_the_load():
    a = {"code": "FIN_A", "category": "financial", "name": "A", "aliases": ["Cover Ratio"]}
    b = {"code": "FIN_B", "category": "financial", "name": "B", "aliases": ["cover ratio"]}
    with pytest.raises(tx.TaxonomyError, match="same category"):
        tx.build([("t", doc(a, b))])
    with pytest.raises(tx.TaxonomyError, match="DPS key"):
        tx.build([("t", doc(dict(a, dps_keys=["K"], aliases=[]), dict(b, dps_keys=["K"], aliases=[])))])
    with pytest.raises(tx.TaxonomyError, match="without a code"):
        tx.build([("t", doc({"category": "financial"}))])


def test_an_alias_shared_across_categories_is_a_note_and_the_covenant_keeps_it():
    neg = {"code": "NEG_COC", "category": "negative", "name": "No change of control", "aliases": ["Change of Control"]}
    eod = {"code": "EOD_COC", "category": "event_of_default", "name": "Control lost", "aliases": ["Change of Control"]}
    t = tx.build([("t", doc(eod, neg))])
    assert t.notes and "CHANGE OF CONTROL" in t.notes[0]
    assert t.code_for("Change of Control") == "NEG_COC"


def test_an_overlay_patches_adds_and_disables_types():
    base = doc({"code": "FIN_DSCR", "category": "financial", "name": "DSCR", "aliases": ["Debt Service Cover"]},
               {"code": "NEG_LIENS", "category": "negative", "name": "Liens", "aliases": ["Negative Pledge"]})
    overlay = {"version": "o1", "types": [
        {"code": "FIN_DSCR", "aliases": ["Debt Service Coverage Ratio"], "dps_keys": ["DSCR"]},
        {"code": "NEG_LIENS", "aliases": ["Encumbrance restriction"], "replace_aliases": True},
        {"code": "FIN_NEW", "category": "financial", "name": "New ratio"},
        {"code": "FIN_NEW", "enabled": False}]}
    t = tx.build([("base", base), ("overlay", overlay)])
    assert t.version == "t1+o1" and t.get("FIN_NEW") is None
    assert t.get("FIN_DSCR").aliases == ("Debt Service Cover", "Debt Service Coverage Ratio")
    assert t.get("NEG_LIENS").aliases == ("Encumbrance restriction",)
    assert t.code_for("Negative Pledge") is None and t.code_for("DSCR") == "FIN_DSCR"


def test_alias_matching_is_careful_with_short_and_one_word_names():
    t = tx.build([("t", doc(
        {"code": "FIN_CR", "category": "financial", "name": "Current ratio", "aliases": ["CR"]},
        {"code": "FIN_DSCR", "category": "financial", "name": "Debt service cover", "aliases": ["DSCR"]},
        {"code": "NEG_INS", "category": "negative", "name": "Insurance"}))])
    assert t.code_for("CR") is None  # two letters: never matched
    assert t.code_for("the DSCR test") == "FIN_DSCR" and t.code_for("the dscr test") is None  # whole upper word
    assert t.code_for("Insurance") == "NEG_INS" and t.code_for("insurance policy copies") is None  # one word: exact
    assert t.code_for("minimum current ratio") == "FIN_CR"  # a phrase inside a name


def test_an_overlay_in_the_environment_is_loaded(tmp_path, monkeypatch):
    extra = tmp_path / "bank.yaml"
    extra.write_text("version: bank.1\ntypes:\n  - {code: FIN_BANK_RATIO, category: financial, name: Bank ratio}\n",
                     encoding="utf-8")
    monkeypatch.setenv(tx.ENV, str(tmp_path))
    tx.get.cache_clear()
    try:
        t = tx.get()
        assert t.get("FIN_BANK_RATIO") is not None and t.version.endswith("+bank.1")
    finally:
        monkeypatch.delenv(tx.ENV)
        tx.get.cache_clear()
    monkeypatch.setenv(tx.ENV, str(tmp_path / "missing"))
    try:
        with pytest.raises(tx.TaxonomyError):
            tx._extra_files()
    finally:
        monkeypatch.delenv(tx.ENV)


# ---------- the four parts ----------

PAGES = [
    {"no": 1, "text": "FINANCIAL COVENANTS\nThe Borrower shall maintain a Debt Service Coverage Ratio of not less "
                      "than 1.20 times."},
    {"no": 2, "text": "The Borrower shall deliver its audited financial statements within 180 days."},
    {"no": 3, "text": "EVENTS OF DEFAULT\nIf the Borrower fails to pay any amount when due, it is an Event of "
                      "Default."},
    {"no": 4, "text": "Schedule of the parties' addresses and signatures."},
]


class PerPart(Salvage):
    """Answers by the part the prompt is for, and records which pages each part was sent."""

    def __init__(self, answers):
        super().__init__([])
        self.answers, self.seen = answers, {}

    def call_json(self, route, messages, schema, check):
        prompt = messages[0]["content"]
        part = next((p.name for p in parts.PARTS.values() if f"Find {p.scope} in" in prompt), "other")
        self.seen[part] = [n for n in range(1, 10) if f"<<<page {n}>>>" in prompt]
        answer = {"items": self.answers.get(part, [])}
        problem = check(answer)
        assert not problem, problem
        return answer


def test_each_part_reads_only_the_pages_with_its_words_and_events_come_back_on_their_own():
    pack = load("covenants")
    assert [p.name for p in parts.parts_of(pack)] == ["financial", "undertakings", "reporting", "events_of_default"]
    dscr = {"name": "DSCR", "page": 1, "quote": "Debt Service Coverage Ratio of not less than 1.20",
            "value": {"metric": "FIN_DSCR", "operator": ">=", "threshold": 1.2, "unit": "TIMES"}}
    eod = {"name": "Non-payment", "page": 3, "quote": "fails to pay any amount when due",
           "value": {"metric": "EOD_NON_PAYMENT"}}
    gw = PerPart({"financial": [dscr], "events_of_default": [eod]})
    covenants, events = parts.read(pack, gw, [PAGES], all_pages=PAGES)
    assert 3 in gw.seen["events_of_default"] and 4 not in gw.seen["events_of_default"]
    assert 2 in gw.seen["reporting"] and 4 not in gw.seen["financial"]
    assert [c["value"]["metric"] for c in covenants] == ["FIN_DSCR"] and covenants[0]["_part"] == "financial"
    assert [e["value"]["metric"] for e in events] == ["EOD_NON_PAYMENT"]


def test_with_no_part_words_on_any_page_one_read_covers_every_covenant_category():
    pack = load("covenants")
    gw = PerPart({})
    plain = [{"no": 1, "text": "Schedule of the parties' addresses."}]
    covenants, events = parts.read(pack, gw, [plain], all_pages=plain)
    assert covenants == [] and events is None and list(gw.seen) == ["other"]


# ---------- codes and defaults on fields ----------

def field(group, value, name="x"):
    return {"group": group, "name": name, "value": value, "issues": [], "status": "AUTO_ACCEPTED", "test": None}


def test_stamp_sets_code_and_category_and_marks_taxonomy_defaults_for_review():
    full = field("FINANCIAL_COVENANT", {"metric": "DSCR", "operator": ">=", "threshold": 1.2, "unit": "TIMES"})
    full["test"] = {"basis": None, "firstTestDate": None, "frequency": "YEARLY"}
    bare = field("FINANCIAL_COVENANT", {"metric": "DSCR", "threshold": 1.2})
    none = field("NON_FINANCIAL_COVENANT", {"metric": "NEGATIVE_PLEDGE"})
    other = field("POSSIBLE_COVENANT", {"metric": "OTHER"}, name="Hotel star classification")
    line = field("STATEMENT_LINE", {"amount": 1})
    codes.stamp([full, bare, none, other, line])
    assert (full["value"]["code"], full["value"]["category"]) == ("FIN_DSCR", "financial")
    assert "defaults" not in full["value"] and full["status"] == "AUTO_ACCEPTED"
    assert bare["value"]["operator"] == ">=" and bare["value"]["defaults"]
    assert bare["status"] == "NEEDS_REVIEW" and bare["issues"][-1]["code"] == "TAXONOMY_DEFAULT"
    assert (none["value"]["code"], none["value"]["category"]) == ("NEG_LIENS", "negative") and not none["issues"]
    assert other["value"]["code"] is None and other["value"]["category"] is None
    assert "code" not in line["value"]


def test_the_proven_flows_get_codes_only():
    bare = field("FINANCIAL_COVENANT", {"metric": "DSCR", "threshold": 1.2})
    codes.stamp([bare], defaults=False)
    assert bare["value"]["code"] == "FIN_DSCR" and "operator" not in bare["value"] and not bare["issues"]


def test_events_of_default_are_their_own_fields_for_a_person_one_per_code_and_page():
    pages = [{"no": 3, "text": PAGES[2]["text"]}]
    got = codes.event_fields("covenants", pages, [
        {"name": "Non-payment", "page": 3, "quote": "fails to pay any amount when due",
         "value": {"metric": "EOD_NON_PAYMENT"}},
        {"name": "Non-payment again", "page": 3, "quote": "fails to pay any amount when due",
         "value": {"metric": "EOD_NON_PAYMENT"}},
        {"name": "Something odd", "page": 3, "quote": "it is an Event of Default", "value": {"metric": "FIN_DSCR"}},
        {"name": "No page", "quote": "q"}])
    assert [f["value"]["code"] for f in got] == ["EOD_NON_PAYMENT", "EOD_OTHER"]
    assert all(f["group"] == "EVENT_OF_DEFAULT" and f["status"] == "NEEDS_REVIEW" for f in got)
    assert all(f["value"]["category"] == "event_of_default" for f in got)
    assert codes.event_fields("covenants", pages, None) == []


# ---------- coverage families ----------

@pytest.mark.parametrize("key, fam", [
    ("REPORTING_COVENANT", "REPORTING"), ("REP_OTHER_INFO", "REPORTING"), ("COMPLIANCE_CERTIFICATE", "REPORTING"),
    ("NEG_GUARANTEES", "NEG_INDEBTEDNESS"), ("NEG_DISPOSAL_CAP", "NEG_DISPOSALS"), ("DSCR", "FIN_DSCR"),
    ("OTHER", None), ("NEG_OTHER", None), (None, None), ("NOT_A_THING", None),
])
def test_coverage_families(key, fam):
    assert coverage.family(key) == fam


def test_a_sentence_of_no_older_kind_gets_its_kind_from_the_taxonomy_words():
    assert coverage.sentence_kind("The Borrower shall submit monthly stock statements within 15 days.") == "REPORTING"
    assert coverage.sentence_kind("The Borrower shall not declare or pay any dividend without the prior consent "
                                  "of the Lender.") == "NEG_RESTRICTED_PAYMENTS"
    assert coverage.sentence_kind("The Borrower shall keep the lights on.") is None


# ---------- end to end: the result is valid against the schema ----------

def test_an_agreement_job_returns_codes_events_and_the_taxonomy_version_in_a_valid_result():
    page = "\n".join(p["text"] for p in PAGES[:3])
    dscr = {"name": "DSCR", "page": 1, "quote": "Debt Service Coverage Ratio of not less than 1.20",
            "value": {"metric": "FIN_DSCR", "operator": ">=", "threshold": 1.2, "unit": "TIMES"}}
    eod = {"name": "Non-payment", "page": 1, "quote": "fails to pay any amount when due",
           "value": {"metric": "EOD_NON_PAYMENT"}}
    gw = PerPart({"financial": [dscr], "events_of_default": [eod]})
    result = skills.run(load("covenants"), fake_job("ONBOARD"), model_of(page), gw, Tools())
    check("result", result)
    assert result["pipeline"]["taxonomyVersion"] == tx.get().version
    by_group = {}
    for f in result["fields"]:
        by_group.setdefault(f["group"], []).append(f)
    dscr_field = next(f for f in by_group["FINANCIAL_COVENANT"] if f["value"]["metric"] == "DSCR")
    assert (dscr_field["value"]["code"], dscr_field["value"]["category"]) == ("FIN_DSCR", "financial")
    events = by_group["EVENT_OF_DEFAULT"]
    assert [e["value"]["code"] for e in events] == ["EOD_NON_PAYMENT"] and events[0]["status"] == "NEEDS_REVIEW"
