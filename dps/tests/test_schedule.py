"""Phase 3b-1 (11 November 2026): step-down schedules, test dates, cure rights, the agreement's own definitions and
the figures each covenant is tested on, read from the agreement and carried to Core through C1."""

from __future__ import annotations

import json
from datetime import date

import pytest
from test_flows import FakeCore, add_document, intake

from dps import services
from dps.db import Database
from dps.extraction.agreement import extract_terms
from dps.extraction.mapping import normalize
from dps.extraction.schedule import (
    adjustments,
    basis,
    cron_for,
    cure,
    definitions,
    frequency_for,
    step_down,
)
from dps.extraction.schedule import test_months as months_tested
from dps.models import Base, ExtractedTerm
from dps.parse.engine import Page
from dps.profiles import LoanProfile

SENTENCE = ("The Borrower shall not permit the Consolidated Leverage Ratio to exceed 4.00 to 1.00 for any Test Period "
            "ending on or before 31 March 2027, 3.75 to 1.00 for any Test Period ending after 31 March 2027 and on or "
            "before 31 March 2028 and 3.50 to 1.00 thereafter.")
TABLE = """to exceed the ratio set forth below opposite such Fiscal Quarter:
Fiscal Quarter Ending    Maximum Leverage Ratio
December 31, 2026        4.50:1.00
March 31, 2027           4.25:1.00
June 30, 2027            4.25:1.00
September 30, 2027 and thereafter  3.75:1.00
"""
AGREEMENT = """1. DEFINITIONS
"EBITDA" means the operating profit of the Group before interest, tax, depreciation and amortisation, adding back
any exceptional or non-cash items, provided that such add-backs shall not exceed 10% of EBITDA.
21. FINANCIAL COVENANTS
21.1 Leverage. The Borrower shall not permit the ratio of Total Debt to EBITDA, on a consolidated basis, to exceed
4.00 to 1.00 for any Test Period ending on or before 31 March 2027 and 3.50 to 1.00 thereafter.
21.2 DSCR. The Borrower shall maintain a Debt Service Coverage Ratio of not less than 1.25:1.
21.3 Testing. The financial covenants shall be tested on 31 March and 30 September in each financial year.
21.4 Equity cure. If the Borrower fails to comply with Clause 21.1, the Borrower may cure such breach by an equity
contribution within fifteen (15) Business Days, provided that such right may not be exercised more than two times."""


@pytest.fixture
def database(tmp_path) -> Database:
    db = Database(f"sqlite:///{tmp_path / 'schedule.db'}")
    Base.metadata.create_all(db.engine)
    return db


# ---------------------------------------------------------------- step-downs

def test_a_step_down_in_a_sentence_gives_each_level_its_period():
    levels = [lv.as_dict() for lv in step_down(SENTENCE + " 21.2 The DSCR shall be at least 1.25:1.", 4.0)]
    assert levels == [{"threshold": 4.0, "from": None, "to": "2027-03-31"},
                      {"threshold": 3.75, "from": "2027-04-01", "to": "2028-03-31"},
                      {"threshold": 3.5, "from": "2028-04-01", "to": None}]


def test_a_step_down_table_merges_equal_rows_and_runs_on_after_the_last_date():
    levels = [lv.as_dict() for lv in step_down(TABLE, 4.5)]
    assert levels == [{"threshold": 4.5, "from": None, "to": "2026-12-31"},
                      {"threshold": 4.25, "from": "2027-01-01", "to": "2027-06-30"},
                      {"threshold": 3.75, "from": "2027-07-01", "to": None}]


@pytest.mark.parametrize("text, first", [
    ("DSCR shall not be less than 1.25x at all times.", 1.25),  # one level
    (SENTENCE, 9.0),  # the rules read another number: not this schedule
    ("March 31, 2027 4.00:1.00\nMarch 31, 2026 3.50:1.00\n", 4.0),  # dates out of order
])
def test_no_schedule_is_made_up(text, first):
    assert step_down(text, first) == []


@pytest.mark.parametrize("raw, iso", [
    ("on or before 31st March, 2027", "2027-03-31"), ("on or before March 31, 2027", "2027-03-31"),
    ("on or before 31.03.2027", "2027-03-31"), ("on or before 2027-03-31", "2027-03-31"),
    ("on or before 30 Sept 2027", "2027-09-30"),
])
def test_dates_in_every_common_form(raw, iso):
    levels = step_down(f"3.00x for any period ending {raw} and 2.50x thereafter.", 3.0)
    assert levels[0].to_date == iso


# ---------------------------------------------------------------- test dates and cure

def test_test_dates_give_months_and_a_schedule():
    months = months_tested("The covenants shall be tested on 31 March and 30 September in each financial year.")
    assert months == [3, 9] and frequency_for(months) == "CUSTOM" and cron_for(months) == "0 0 2 1 4,10 ?"
    assert frequency_for([3, 6, 9, 12]) == "QUARTERLY" and frequency_for([3]) == "YEARLY"
    assert frequency_for([]) is None and cron_for([]) is None
    assert months_tested("tested quarterly") == [] and months_tested("") == []
    assert months_tested("tested on 31 March 2026") == []  # a single dated test is not a recurring schedule


def test_a_cure_right_with_its_days_and_count():
    right = cure("21.4 Equity cure. " + AGREEMENT.split("21.4 Equity cure. ")[1])
    assert (right.kind, right.days, right.business_days, right.max_uses) == ("EQUITY", 15, True, 2)
    remedy = cure("Any breach, if capable of remedy, shall be remedied within 30 days. The Borrower may cure it once.")
    assert (remedy.kind, remedy.days, remedy.business_days) == ("REMEDY", 30, False)
    assert cure("The Borrower shall maintain a DSCR of 1.25x.") is None


# ---------------------------------------------------------------- definitions and basis

def test_the_agreements_own_definitions_and_their_adjustments():
    found = definitions(AGREEMENT)
    assert set(found) == {"EBITDA"}
    assert adjustments(found["EBITDA"])[0].startswith("depreciation and amortisation, adding back")
    assert adjustments("\"EBITDA\" means profit before interest, tax, depreciation and amortisation.") == []


@pytest.mark.parametrize("quote, entity", [
    ("tested on a consolidated basis", "CONSOLIDATED"), ("of the Group", "CONSOLIDATED"),
    ("on a standalone basis", "STANDALONE"), ("on a company basis", "STANDALONE"), ("the DSCR", None),
])
def test_basis(quote, entity):
    assert basis(quote) == entity


# ---------------------------------------------------------------- read with the covenants and sent to Core

def test_an_agreement_carries_its_schedule_cure_definitions_and_basis():
    terms = {t.metric: t for t in extract_terms([Page(1, AGREEMENT, "TEXT", 1.0)]) if t.category == "FINANCIAL"}
    leverage, dscr = terms["DEBT_TO_EBITDA"], terms["DSCR"]
    assert [lv["threshold"] for lv in leverage.levels] == [4.0, 3.5]
    assert leverage.basis == "CONSOLIDATED" and dscr.basis is None
    assert any(i.startswith("STEP_DOWN: 2 levels read") for i in leverage.issues)
    assert not any(i.startswith("SEVERAL_LEVELS") for i in leverage.issues)
    assert any(i.startswith("DEFINITION_DIFFERS: the agreement's EBITDA is adjusted") for i in leverage.issues)
    assert dscr.test_months == [3, 9] and dscr.frequency == "CUSTOM" and dscr.cure["max_uses"] == 2

    [p] = [p for p in normalize(list(terms.values()), []) if p.metric_raw == "DEBT_TO_EBITDA"]
    assert [lv["expression"] for lv in p.schedule["levels"]] == ["DPS_DEBT_TO_EBITDA_TTM <= 4",
                                                                 "DPS_DEBT_TO_EBITDA_TTM <= 3.5"] or all(
        lv["expression"].endswith(f"<= {lv['threshold']:g}") for lv in p.schedule["levels"])
    assert p.schedule["cron"] == "0 0 2 1 4,10 ?" and p.schedule["cure"]["days"] == 15


class ProfileCore(FakeCore):
    def __init__(self, entity: str) -> None:
        super().__init__()
        self.entity = entity

    def cp_profile(self, cp_id):
        return LoanProfile("INR", True, 3, self.entity, "INDIAN")


def test_intake_flags_a_covenant_tested_on_other_figures_and_sends_the_schedule_through_c1(database):
    doc_id = add_document(database)
    flow = intake(database, [Page(1, AGREEMENT, "TEXT", 1.0)], core=ProfileCore("STANDALONE"))
    flow.kickoff(inputs={"job_id": "j1", "document_id": doc_id})
    leverage = next(p for p in flow.state.proposals if p["metric_raw"] == "DEBT_TO_EBITDA")
    assert any(i.startswith("ENTITY_DIFFERS: the agreement tests this on group figures") for i in leverage["issues"])

    with database.session_factory() as s:
        for t in s.query(ExtractedTerm).filter(ExtractedTerm.document_id == doc_id):
            t.status = "PUBLISHED"  # the reviewer approved them
        s.commit()
        recs = services.recommendations(s, "CP-1")
    stepped = next(r for r in recs if r["levels"])
    assert len(stepped["levels"]) == 2 and stepped["cron"] == "0 0 2 1 4,10 ?"
    assert stepped["cure"]["kind"] == "EQUITY"
    with database.session_factory() as s:
        row = s.query(ExtractedTerm).filter(ExtractedTerm.metric_raw == "DEBT_TO_EBITDA").one()
        assert json.loads(row.schedule_json)["testMonths"] == [3, 9]


def test_a_borrower_on_group_figures_is_not_flagged(database):
    doc_id = add_document(database, as_of=date(2026, 3, 31))
    flow = intake(database, [Page(1, AGREEMENT, "TEXT", 1.0)], core=ProfileCore("CONSOLIDATED"))
    flow.kickoff(inputs={"job_id": "j1", "document_id": doc_id})
    assert not any(i.startswith("ENTITY_DIFFERS") for p in flow.state.proposals for i in p["issues"])
