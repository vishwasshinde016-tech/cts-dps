"""Phase 3b-2 (12 November 2026): documents in any language. The rules read English; the AI reader reads any language
and every number it gives must be on the page (in that page's own script and digits). Without the AI reader a document
in another language waits for a person, never published empty."""

from __future__ import annotations

import json
from datetime import date

import pytest
from test_flows import FakeParser, add_document, doc_status, intake, values

from dps.db import Database
from dps.extraction import agreement_ai, statement_ai
from dps.extraction.verify import numbers_in, quote_on_page, threshold_in
from dps.flows.statement import StatementDeps, StatementProcessingFlow
from dps.language import detect, name, plain_digits
from dps.models import Base
from dps.parsing import Page

FRENCH_AGREEMENT = ("Article 12. Engagements financiers. L'Emprunteur s'engage à maintenir un ratio de couverture "
                    "du service de la dette au moins égal à 1,20 pour chaque période de test et un ratio "
                    "d'endettement net "
                    "inférieur ou égal à 3,50 pendant toute la durée du crédit. Les états financiers de la société "
                    "sont "
                    "fournis dans les délais prévus par la présente convention et par la loi applicable.")
FRENCH_STATEMENT = """Comptes annuels de la société (en millions d'euros)
Chiffre d'affaires 1 200,0
Résultat avant impôt 150,0
Capitaux propres 900,0
Dettes financières à long terme 400,0
Trésorerie et équivalents de trésorerie 80,0
Le présent document est établi par la société pour les besoins de la banque et de ses conseils."""


@pytest.fixture
def database(tmp_path) -> Database:
    db = Database(f"sqlite:///{tmp_path / 'lang.db'}")
    Base.metadata.create_all(db.engine)
    return db


@pytest.mark.parametrize("text, code", [
    (FRENCH_AGREEMENT, "fr"), ("The Borrower shall maintain a DSCR of not less than 1.25x at all times.", "en"),
    ("يجب على المقترض الحفاظ على نسبة تغطية خدمة الدين", "ar"), ("ණය ගැනුම්කරු විසින් පවත්වා ගත යුතුය", "si"),
    ("कंपनी का शुद्ध लाभ", "hi"), ("", "en"), ("12345 67.8", "en"),
    ("El prestatario deberá mantener una razón de cobertura de la deuda de al menos 1,25 para el periodo de prueba y "
     "la sociedad presentará los estados financieros de la sociedad en el plazo previsto por la ley y por el contrato",
     "es"),
])
def test_language(text, code):
    assert detect(text) == code


def test_digits_of_every_script_and_comma_decimals():
    assert plain_digits("١٫٢٥ و ٣٬٥٠٠") == "1.25 و 3,500"
    assert plain_digits("१२३ ൦") == "123 ൦"
    assert numbers_in("ratio ≥ 1,25 ; 1,250 ; (3.5)") == [1.25, 1250.0, 3.5]
    assert threshold_in("au moins égal à 1,20", 1.2)
    assert threshold_in("نسبة لا تقل عن ١٫٢٥", 1.25)
    page = "يجب على المقترض الحفاظ على نسبة تغطية خدمة الدين لا تقل عن ١٫٢٥ في جميع الأوقات"
    assert quote_on_page({3: page}, 3, "يجب على المقترض الحفاظ على نسبة تغطية خدمة الدين لا تقل عن 1.25") == 3
    assert name("fr") == "French" and name("xx") == "xx"


def test_a_french_agreement_is_read_by_the_ai_with_its_quotes_checked_in_french():
    sent = []

    def model(messages):
        sent.append(messages[0]["content"])
        return json.dumps({"covenants": [
            {"name": "Debt Service Coverage Ratio", "family": "DSCR", "dir": "min", "threshold": 1.2, "page": 1,
             "quote": "maintenir un ratio de couverture du service de la dette au moins égal à 1,20"},
            {"name": "Net Leverage", "family": "LEVERAGE", "dir": "max", "threshold": 3.5, "page": 1,
             "quote": "un ratio d'endettement net inférieur ou égal à 3,50"},
            {"name": "Invented", "family": "ICR", "dir": "min", "threshold": 2.0, "page": 1,
             "quote": "un ratio de couverture des intérêts au moins égal à 2,00"},  # not in the agreement
        ]})

    terms, dropped = agreement_ai.ai_covenants([Page(1, FRENCH_AGREEMENT, "TEXT", 1.0)], [], model)
    assert "any language" in sent[0]  # the prompt asks for English names and quotes as printed
    assert [(t.metric, t.operator, t.threshold) for t in terms] == [("DSCR", ">=", 1.2),
                                                                    ("NET_LEVERAGE_RATIO", "<=", 3.5)]
    assert dropped == 1 and all("AI_ONLY" in t.issues[0] for t in terms)


def test_without_the_ai_reader_an_agreement_in_another_language_waits(database):
    doc_id = add_document(database)
    flow = intake(database, [Page(1, FRENCH_AGREEMENT, "TEXT", 1.0)])
    flow.kickoff(inputs={"job_id": "j1", "document_id": doc_id})
    assert flow.state.outcome == "NEEDS_REVIEW" and doc_status(database, doc_id) == "NEEDS_REVIEW"
    assert any(i.startswith("LANGUAGE_NOT_READ: the agreement is in French")
               for p in flow.state.proposals for i in p["issues"])


def statement(database, reader=None):
    doc_id = add_document(database, "FINANCIAL_STATEMENT", "fr.pdf", date(2026, 12, 31), ("CP-1",))
    deps = StatementDeps(database.session_factory, lambda d: b"%PDF", FakeParser([Page(1, FRENCH_STATEMENT, "TEXT",
                                                                                         1.0)]), "ds-dps", True,
                         second_reader=reader)
    flow = StatementProcessingFlow(deps)
    flow.kickoff(inputs={"job_id": "j1", "document_id": doc_id})
    return flow, doc_id


def test_a_french_statement_is_read_by_the_ai_and_waits_for_a_person(database):
    def model(messages):
        return json.dumps({"unit": "million", "values": {
            "REVENUE": {"value": 1200.0, "page": 1, "line": "Chiffre d'affaires 1 200,0"},
            "PBT": {"value": 150.0, "page": 1, "line": "Résultat avant impôt 150,0"},
            "NET_WORTH": {"value": 900.0, "page": 1, "line": "Capitaux propres 900,0"},
            "LT_DEBT": {"value": 400.0, "page": 1, "line": "Dettes financières à long terme 400,0"},
            "CASH": {"value": 81.0, "page": 1, "line": "Trésorerie 81,0"},  # not printed: dropped
        }})

    def reader(text, values, scale=1.0, entity=None):
        return statement_ai.check(text, values, model, scale=scale)

    flow, doc_id = statement(database, reader)
    assert flow.state.language == "fr"
    assert flow.state.outcome == "NEEDS_REVIEW" and values(database) == {}
    got = {v["key"]: v["value"] for v in flow.state.values}
    # Printed in euros with no EUR rate: kept in EUR million and held (CURRENCY_DIFFERS), never sent as rupees.
    assert got["NET_WORTH"] == pytest.approx(900.0) and got["REVENUE"] == pytest.approx(1200.0)
    assert {v["unit"] for v in flow.state.values} == {"EUR_MN"}
    assert any(i.startswith("CURRENCY_DIFFERS") for i in flow.state.issues)
    assert "CASH" not in got and "TOTAL_DEBT" in got  # the totals are worked out from the lines read
    assert any(i.startswith("READ_BY_AI: 4 figures of this French statement") for i in flow.state.issues)


def test_without_the_ai_reader_a_statement_in_another_language_waits(database):
    flow, _ = statement(database)
    assert flow.state.outcome == "NEEDS_REVIEW"
    assert any(i.startswith("LANGUAGE_NOT_READ: the statement is in French") for i in flow.state.issues)
